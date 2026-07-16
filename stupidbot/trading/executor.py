"""Adapter eksekusi live stupidbot di atas TradeExecutor PulseFlow.

Kenapa reuse: TradeExecutor pulseflow sudah battle-tested di akun yang sama —
kredensial dari ../.env, pembulatan LOT_SIZE/PRICE_FILTER, cap margin,
workaround Algo Order API (sejak 2025-12 Binance USDS-M menolak STOP_MARKET
di endpoint order biasa, error -4120), dan fail-safe: posisi tanpa SL tidak
boleh hidup.

Yang ditambahkan di sini:
- jurnal live TERPISAH dari pulseflow (stupidbot/logs/live_trades.json)
- position_amount / reduce_position / sync_protection / cancel_protection
  untuk manajemen posisi gaya stupidbot (partial TP + BE + ATR trailing).
"""
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # F:\tradingbot\pulseflow-nu
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pulseflow.trading.executor import TradeExecutor, _fmt, _round_step  # noqa: E402

logger = logging.getLogger("stupidbot.executor")

LIVE_LOG = Path(__file__).resolve().parents[1] / "logs" / "live_trades.json"


class StupidbotExecutor(TradeExecutor):
    def __init__(self, live: bool):
        # paper_mode eksplisit — jangan bergantung default .env di sini;
        # keputusan mode ada di runner (double opt-in).
        super().__init__(paper_mode=not live)

    # ── Jurnal live terpisah dari pulseflow ──────────────────────────
    def _read_live_log(self) -> list:
        if LIVE_LOG.exists():
            try:
                return json.loads(LIVE_LOG.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _write_live_log(self, logs: list):
        LIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        LIVE_LOG.write_text(
            json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Entry limit (no market order) ─────────────────────────────────
    def place_limit_entry(self, symbol: str, direction: str, qty: float,
                          price: float, leverage: int | None = None) -> dict:
        """Pasang LIMIT GTC untuk entry (maker). Tidak ada market order."""
        from binance.enums import (SIDE_BUY, SIDE_SELL,
                                   FUTURE_ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC)
        bsymbol = self.to_binance_symbol(symbol)
        flt = self._filters(bsymbol)
        try:
            self.client().futures_change_leverage(
                symbol=bsymbol, leverage=leverage or self.leverage)
        except Exception as e:
            logger.warning("Set leverage gagal (%s) — lanjut leverage akun", e)
        order = self.client().futures_create_order(
            symbol=bsymbol,
            side=SIDE_BUY if direction == "LONG" else SIDE_SELL,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=_fmt(_round_step(qty, flt["stepSize"]), flt["stepSize"]),
            price=_fmt(_round_step(price, flt["tickSize"]), flt["tickSize"]))
        logger.info("🔴 LIVE limit entry %s %s qty %s @ %s (orderId %s)",
                    direction, bsymbol, qty, price, order.get("orderId"))
        return order

    def order_status(self, symbol: str, order_id: int) -> dict:
        """Status order: NEW / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED."""
        od = self.client().futures_get_order(
            symbol=self.to_binance_symbol(symbol), orderId=order_id)
        return {"status": od.get("status"),
                "executed_qty": float(od.get("executedQty") or 0.0),
                "avg_price": float(od.get("avgPrice") or 0.0)}

    def cancel_order(self, symbol: str, order_id: int) -> None:
        try:
            self.client().futures_cancel_order(
                symbol=self.to_binance_symbol(symbol), orderId=order_id)
        except Exception as e:
            logger.warning("Cancel order %s #%s gagal: %s", symbol, order_id, e)

    def place_protective_sl(self, symbol: str, direction: str, sl: float) -> dict:
        """STOP_MARKET closePosition — dipasang BERSAMAAN dengan limit entry
        sehingga posisi terlindungi sejak detik pertama terisi. Bila trigger
        tersentuh tanpa posisi, order hangus tanpa efek."""
        from binance.enums import SIDE_BUY, SIDE_SELL
        bsymbol = self.to_binance_symbol(symbol)
        tick = self._filters(bsymbol)["tickSize"]
        exit_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        sl_s = _fmt(_round_step(sl, tick), tick) if tick else str(sl)
        return self._place_conditional(bsymbol, exit_side, "STOP_MARKET", sl_s)

    def place_reduce_limits(self, symbol: str, direction: str,
                            legs: list[tuple[float, float]]) -> list:
        """TP sebagai LIMIT reduce-only (maker): legs = [(price, qty), ...].
        Butuh posisi sudah terisi. Leg yang gagal dilewati (posisi tetap
        ber-SL stop-market)."""
        from binance.enums import (SIDE_BUY, SIDE_SELL,
                                   FUTURE_ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC)
        bsymbol = self.to_binance_symbol(symbol)
        flt = self._filters(bsymbol)
        exit_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        out = []
        for price, qty in legs:
            q = _round_step(qty, flt["stepSize"])
            if q < flt["minQty"]:
                continue
            try:
                out.append(self.client().futures_create_order(
                    symbol=bsymbol, side=exit_side,
                    type=FUTURE_ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=_fmt(q, flt["stepSize"]),
                    price=_fmt(_round_step(price, flt["tickSize"]), flt["tickSize"]),
                    reduceOnly=True))
            except Exception as e:
                logger.warning("TP limit %s @ %s gagal (posisi tetap ber-SL): %s",
                               bsymbol, price, e)
        return out

    # ── Utilitas posisi ───────────────────────────────────────────────
    def position_amount(self, symbol: str) -> float:
        """Jumlah posisi bertanda (+long / −short); 0 = tidak ada posisi."""
        bsymbol = self.to_binance_symbol(symbol)
        pos = self.client().futures_position_information(symbol=bsymbol)
        return float(pos[0]["positionAmt"]) if pos else 0.0

    def reduce_position(self, symbol: str, direction: str, qty: float) -> float:
        """Tutup sebagian posisi (reduceOnly market). Kembalikan qty yang
        benar-benar ditutup; 0 bila hasil pembulatan < minQty atau akan
        menutup seluruh posisi (itu tugas SL/TP, bukan partial)."""
        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
        bsymbol = self.to_binance_symbol(symbol)
        flt = self._filters(bsymbol)
        q = _round_step(qty, flt["stepSize"])
        amt = abs(self.position_amount(symbol))
        if q < flt["minQty"] or q >= amt:
            return 0.0
        exit_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        self.client().futures_create_order(
            symbol=bsymbol, side=exit_side, type=FUTURE_ORDER_TYPE_MARKET,
            quantity=_fmt(q, flt["stepSize"]), reduceOnly=True)
        logger.info("LIVE partial %s: reduceOnly %s", bsymbol, q)
        return q

    def sync_protection(self, symbol: str, direction: str, sl: float,
                        tp: float | None = None) -> dict:
        """Pasang ulang proteksi exchange: cancel semua algo order symbol ini
        lalu pasang SL (WAJIB) + TP (opsional), keduanya closePosition.

        Fail-safe: bila SL baru GAGAL terpasang, seluruh posisi ditutup
        paksa — posisi tanpa SL tidak boleh hidup."""
        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
        bsymbol = self.to_binance_symbol(symbol)
        tick = self._filters(bsymbol)["tickSize"]
        exit_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        result: dict = {"ok": True}

        try:
            self._cancel_all_conditional(bsymbol)
        except Exception as e:
            logger.warning("Cancel algo lama %s gagal: %s", bsymbol, e)

        sl_s = _fmt(_round_step(sl, tick), tick) if tick else str(sl)
        try:
            result["sl_order"] = self._place_conditional(
                bsymbol, exit_side, "STOP_MARKET", sl_s)
        except Exception as e:
            logger.error("SL GAGAL terpasang %s: %s — menutup posisi (fail-safe)",
                         bsymbol, e)
            try:
                amt = self.position_amount(symbol)
                if amt != 0:
                    step = self._filters(bsymbol)["stepSize"]
                    self.client().futures_create_order(
                        symbol=bsymbol,
                        side=SIDE_SELL if amt > 0 else SIDE_BUY,
                        type=FUTURE_ORDER_TYPE_MARKET,
                        quantity=_fmt(abs(amt), step), reduceOnly=True)
                result["failsafe"] = "Posisi ditutup karena SL gagal terpasang"
            except Exception as e2:
                result["failsafe"] = (f"KRITIS: SL gagal DAN tutup posisi gagal "
                                      f"({e2}) — TUTUP MANUAL SEKARANG")
            result["ok"] = False
            result["error"] = f"SL gagal: {e}"
            return result

        if tp is not None:
            tp_s = _fmt(_round_step(tp, tick), tick) if tick else str(tp)
            try:
                result["tp_order"] = self._place_conditional(
                    bsymbol, exit_side, "TAKE_PROFIT_MARKET", tp_s)
            except Exception as e:
                result["tp_error"] = str(e)  # posisi tetap ber-SL — aman
                logger.warning("TP gagal terpasang %s (posisi tetap ber-SL): %s",
                               bsymbol, e)
        return result

    def cancel_protection(self, symbol: str) -> None:
        """Bersihkan SEMUA order sisa symbol ini setelah posisi tutup:
        algo conditional (SL) + order biasa (limit TP reduce-only yatim)."""
        bsymbol = self.to_binance_symbol(symbol)
        try:
            self._cancel_all_conditional(bsymbol)
        except Exception as e:
            logger.warning("Cancel algo sisa %s gagal: %s", symbol, e)
        try:
            self.client().futures_cancel_all_open_orders(symbol=bsymbol)
        except Exception as e:
            logger.warning("Cancel order sisa %s gagal: %s", symbol, e)

    def mark_closed(self, symbol: str, reason: str) -> None:
        self._mark_live_closed(self.to_binance_symbol(symbol), reason)

    def journal_open(self, rec: dict) -> None:
        """Catat posisi terisi ke jurnal live (konteks analitik; sumber PnL
        resmi tetap data exchange)."""
        rec.setdefault("status", "LIVE_OPEN")
        rec["symbol"] = self.to_binance_symbol(rec.get("symbol", ""))
        self._append_live_log(rec)
