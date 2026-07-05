"""
Trade Executor — eksekusi plan EntrySignalEngine ke Binance USDS-M Futures.

Mode:
    PAPER (default, PAPER_MODE=true di .env) — order disimulasikan dan
        dicatat ke `paper_trades.json`; tidak ada satu pun request order
        ke exchange.
    LIVE  (PAPER_MODE=false) — order sungguhan via python-binance:
        market entry + STOP_MARKET (SL, closePosition) +
        TAKE_PROFIT_MARKET (TP1, closePosition).

Aturan keselamatan (lihat skill trading-executor):
- API key hanya dari .env — tidak pernah hardcode.
- Position size SELALU dihitung dari risk % balance terhadap jarak stop.
- Order tanpa SL tidak akan pernah ditempatkan; bila SL GAGAL terpasang
  setelah entry live terisi, posisi langsung ditutup paksa (fail-safe).
- Tidak ada retry otomatis pada error API — error dikembalikan ke caller.
- Konfirmasi eksplisit sebelum eksekusi live adalah tanggung jawab UI
  (dialog ringkasan order) — modul ini tidak pernah dipanggil tanpa itu.

Semua method network-blocking — panggil dari worker thread, bukan thread GUI.
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger("PulseFlow.Executor")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_LOG_FILE = _PROJECT_ROOT / "paper_trades.json"
LIVE_LOG_FILE = _PROJECT_ROOT / "live_trades.json"
PAPER_START_BALANCE = 10_000.0


def _round_step(value: float, step: float) -> float:
    """Bulatkan ke bawah ke kelipatan step (LOT_SIZE / PRICE_FILTER)."""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step

def _step_decimals(step: float) -> int:
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0

def _fmt(value: float, step: float) -> str:
    return f"{value:.{_step_decimals(step)}f}"


class TradeExecutor:
    """Eksekusi trade plan {side, entry, stop, tp1, tp2, ...} per-symbol."""

    def __init__(self, paper_mode: Optional[bool] = None):
        load_dotenv(_PROJECT_ROOT / ".env")
        env_paper = os.getenv("PAPER_MODE", "true").strip().lower() == "true"
        self.paper_mode = env_paper if paper_mode is None else bool(paper_mode)
        self.risk_pct = float(os.getenv("RISK_PCT", "1.0"))
        self.leverage = int(os.getenv("LEVERAGE", "5"))
        # Taker fee per sisi (% dari notional) — dipakai simulasi paper &
        # estimasi di ringkasan order. Default VIP0 USDT-M: 0.05%.
        self.taker_fee_pct = float(os.getenv("TAKER_FEE_PCT", "0.05"))
        self._api_key = os.getenv("BINANCE_API_KEY", "")
        self._api_secret = os.getenv("BINANCE_API_SECRET", "")

        self._client = None
        self._client_lock = threading.Lock()
        self._filters_cache: Dict[str, Dict[str, float]] = {}
        # Symbol yang posisinya dibuka LIVE oleh sesi PulseFlow ini — hanya
        # posisi ini yang boleh ditutup otomatis saat setup FADED/FLIP.
        # (Restart app = tracking kosong → posisi lama tidak disentuh.)
        self._live_tracked: set = set()

        logger.info("TradeExecutor mode: %s (risk %.2f%%, leverage %dx)",
                    "PAPER" if self.paper_mode else "🔴 LIVE",
                    self.risk_pct, self.leverage)

    # ── Client & util ─────────────────────────────────────────────────

    def client(self):
        """Lazy init python-binance Client (konstruktornya melakukan ping)."""
        with self._client_lock:
            if self._client is None:
                if not self._api_key or not self._api_secret:
                    raise RuntimeError(
                        "BINANCE_API_KEY / BINANCE_API_SECRET belum diisi di .env")
                from binance.client import Client
                self._client = Client(self._api_key, self._api_secret)
            return self._client

    @staticmethod
    def to_binance_symbol(symbol: str) -> str:
        s = symbol.upper()
        return s if s.endswith("USDT") else f"{s}USDT"

    def _filters(self, bsymbol: str) -> Dict[str, float]:
        """stepSize/tickSize/minQty/minNotional dari exchangeInfo (cached)."""
        if bsymbol in self._filters_cache:
            return self._filters_cache[bsymbol]
        info = self.client().futures_exchange_info()
        for s in info.get("symbols", []):
            flt = {"stepSize": 0.0, "tickSize": 0.0,
                   "minQty": 0.0, "minNotional": 0.0}
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    flt["stepSize"] = float(f["stepSize"])
                    flt["minQty"] = float(f["minQty"])
                elif f["filterType"] == "PRICE_FILTER":
                    flt["tickSize"] = float(f["tickSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    flt["minNotional"] = float(f.get("notional", 0.0))
            self._filters_cache[s["symbol"]] = flt
        if bsymbol not in self._filters_cache:
            raise ValueError(f"Symbol {bsymbol} tidak ada di Binance Futures")
        return self._filters_cache[bsymbol]

    # ── Balance & posisi ──────────────────────────────────────────────

    def get_balance(self, asset: str = "USDT") -> Dict[str, Any]:
        if self.paper_mode:
            bal = PAPER_START_BALANCE + self._paper_realized_pnl()
            return {"asset": asset, "balance": round(bal, 2),
                    "available": round(bal, 2), "paper": True}
        for b in self.client().futures_account_balance():
            if b["asset"] == asset:
                return {"asset": asset, "balance": float(b["balance"]),
                        "available": float(b["availableBalance"]),
                        "paper": False}
        return {"asset": asset, "balance": 0.0, "available": 0.0,
                "paper": False}

    def has_open_position(self, symbol: str) -> bool:
        """Guard auto-trade: satu posisi per symbol."""
        bsymbol = self.to_binance_symbol(symbol)
        if self.paper_mode:
            return any(t.get("symbol") == bsymbol and t.get("status") == "PAPER_OPEN"
                       for t in self._read_paper_log())
        for p in self.client().futures_position_information(symbol=bsymbol):
            if float(p["positionAmt"]) != 0:
                return True
        return False

    def get_open_positions(self) -> list:
        if self.paper_mode:
            return [t for t in self._read_paper_log()
                    if t.get("status") == "PAPER_OPEN"]
        out = []
        for p in self.client().futures_position_information():
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            entry = float(p["entryPrice"])
            pnl = float(p["unRealizedProfit"])
            out.append({
                "symbol": p["symbol"],
                "direction": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry": entry,
                "mark_price": float(p["markPrice"]),
                "pnl_usdt": round(pnl, 2),
                "leverage": p.get("leverage", "?"),
            })
        return out

    # ── Sizing & ringkasan order ──────────────────────────────────────

    def prepare_order(self, symbol: str, plan: Dict[str, Any],
                      risk_pct: Optional[float] = None) -> Dict[str, Any]:
        """Hitung size + ringkasan order untuk dialog konfirmasi.
        Tidak menempatkan order apa pun."""
        risk_pct = self.risk_pct if risk_pct is None else float(risk_pct)
        bsymbol = self.to_binance_symbol(symbol)
        side = plan["side"]
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        tp1 = float(plan["tp1"])
        price_risk = abs(entry - stop)
        if price_risk <= 0 or entry <= 0:
            raise ValueError("Plan tidak valid: jarak stop nol")

        bal = self.get_balance()
        risk_amount = bal["available"] * (risk_pct / 100.0)
        qty = risk_amount / price_risk

        if self.paper_mode:
            step = tick = 0.0
            min_notional = 0.0
        else:
            flt = self._filters(bsymbol)
            step, tick = flt["stepSize"], flt["tickSize"]
            min_notional = flt["minNotional"]
            qty = _round_step(qty, step)
            if qty < flt["minQty"]:
                raise ValueError(
                    f"Qty {qty} < minQty {flt['minQty']} — risk terlalu kecil "
                    f"untuk {bsymbol}")

        # Cap margin: notional tidak boleh melebihi daya beli
        # (leverage × available × 0.9) — kalau lewat, qty dikecilkan.
        margin_capped = False
        max_notional = bal["available"] * max(self.leverage, 1) * 0.9
        if qty * entry > max_notional:
            qty = max_notional / entry
            if step:
                qty = _round_step(qty, step)
            margin_capped = True
            if not self.paper_mode and qty < self._filters(bsymbol)["minQty"]:
                raise ValueError(
                    f"Margin tidak cukup untuk qty minimum {bsymbol} — "
                    f"balance ${bal['available']:,.2f}, leverage {self.leverage}x")

        notional = qty * entry
        if min_notional and notional < min_notional:
            raise ValueError(
                f"Notional ${notional:,.2f} < minimum ${min_notional:,.2f} "
                f"{bsymbol} — naikkan risk % atau balance")

        risk_usdt = qty * price_risk        # risk aktual setelah cap/rounding
        fee_est = notional * self.taker_fee_pct / 100.0 * 2.0  # buka + tutup

        rr1 = abs(tp1 - entry) / price_risk
        return {
            "mode": "PAPER" if self.paper_mode else "LIVE",
            "symbol": bsymbol,
            "side": side,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": float(plan.get("tp2", tp1)),
            "quantity": qty,
            "step": step,
            "tick": tick,
            "notional_usdt": round(notional, 2),
            "margin_usdt": round(notional / max(self.leverage, 1), 2),
            "margin_capped": margin_capped,
            "risk_usdt": round(risk_usdt, 2),
            "risk_pct": risk_pct,
            "fee_est_usdt": round(fee_est, 2),
            "balance_usdt": bal["available"],
            "rr1": round(rr1, 2),
            "leverage": self.leverage,
        }

    # ── Algo Order API (order kondisional) ────────────────────────────
    # Sejak 2025-12-09 Binance USDS-M menolak STOP_MARKET/TAKE_PROFIT_MARKET
    # di /fapi/v1/order (error -4120) — wajib via POST /fapi/v1/algoOrder
    # dengan algoType=CONDITIONAL. python-binance 1.0.29 belum punya
    # wrapper-nya, jadi pakai helper signed internal.

    def _place_conditional(self, bsymbol: str, side: str, order_type: str,
                           trigger_price: str) -> Dict[str, Any]:
        return self.client()._request_futures_api(
            "post", "algoOrder", signed=True, data={
                "algoType": "CONDITIONAL",
                "symbol": bsymbol,
                "side": side,
                "type": order_type,          # STOP_MARKET / TAKE_PROFIT_MARKET
                "triggerPrice": trigger_price,
                "closePosition": "true",
                "workingType": "MARK_PRICE",
            })

    def _cancel_all_conditional(self, bsymbol: str):
        """DELETE /fapi/v1/algoOpenOrders — batalkan semua algo order symbol."""
        return self.client()._request_futures_api(
            "delete", "algoOpenOrders", signed=True,
            data={"symbol": bsymbol})

    # ── Eksekusi ──────────────────────────────────────────────────────

    def execute(self, prepared: Dict[str, Any],
                context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Eksekusi order yang sudah disiapkan `prepare_order` DAN sudah
        dikonfirmasi user (untuk LIVE). Entry market + SL + TP1.

        `context` (opsional): {setup, score, grade} dari entry engine —
        dicatat ke jurnal supaya hasil bisa dianalisa per jenis setup."""
        if self.paper_mode:
            return self._execute_paper(prepared, context)
        return self._execute_live(prepared, context)

    def _execute_paper(self, p: Dict[str, Any],
                       context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fee_open = p["notional_usdt"] * self.taker_fee_pct / 100.0
        trade = {
            "status": "PAPER_OPEN",
            "symbol": p["symbol"],
            "side": p["side"],
            "quantity": p["quantity"],
            "entry": p["entry"],
            "stop": p["stop"],
            "tp1": p["tp1"],
            "tp2": p["tp2"],
            "risk_usdt": p["risk_usdt"],
            "notional_usdt": p["notional_usdt"],
            "fee_usdt": round(fee_open, 2),   # fee buka; fee tutup menyusul
            "opened_at": datetime.now().isoformat(timespec="seconds"),
        }
        if context:
            trade.update({k: context[k] for k in ("setup", "score", "grade")
                          if k in context})
        self._append_paper_log(trade)
        logger.info("PAPER order: %s %s qty %s @ %s (SL %s, TP1 %s)",
                    p["side"], p["symbol"], p["quantity"], p["entry"],
                    p["stop"], p["tp1"])
        return {"ok": True, "mode": "PAPER", "trade": trade,
                "note": "Order disimulasikan & dicatat ke paper_trades.json"}

    def _execute_live(self, p: Dict[str, Any],
                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET

        c = self.client()
        bsymbol = p["symbol"]
        is_long = p["side"] == "LONG"
        side = SIDE_BUY if is_long else SIDE_SELL
        exit_side = SIDE_SELL if is_long else SIDE_BUY
        qty_s = _fmt(p["quantity"], p["step"]) if p["step"] else str(p["quantity"])
        results: Dict[str, Any] = {"ok": False, "mode": "LIVE"}

        try:
            c.futures_change_leverage(symbol=bsymbol, leverage=p["leverage"])
        except Exception as e:
            logger.warning("Set leverage gagal (%s) — lanjut dengan leverage akun", e)

        # 1. Entry market
        results["entry_order"] = c.futures_create_order(
            symbol=bsymbol, side=side,
            type=FUTURE_ORDER_TYPE_MARKET, quantity=qty_s)

        # 1b. Harga fill AKTUAL — market order bisa fill jauh dari harga plan
        #     (slippage). avgPrice kadang 0 di response awal → re-fetch.
        fill = 0.0
        try:
            fill = float(results["entry_order"].get("avgPrice") or 0.0)
        except (TypeError, ValueError):
            pass
        if fill <= 0:
            for _ in range(3):
                time.sleep(0.3)
                try:
                    od = c.futures_get_order(
                        symbol=bsymbol,
                        orderId=results["entry_order"]["orderId"])
                    fill = float(od.get("avgPrice") or 0.0)
                except Exception:
                    fill = 0.0
                if fill > 0:
                    break
        if fill <= 0:
            fill = p["entry"]

        # SL/TP digeser sebesar slippage — jarak risk & RR dari plan
        # dipertahankan relatif terhadap harga fill sebenarnya.
        slip = fill - p["entry"]
        sl_price = p["stop"] + slip
        tp_price = p["tp1"] + slip
        sl_s = _fmt(_round_step(sl_price, p["tick"]), p["tick"]) if p["tick"] else str(sl_price)
        tp_s = _fmt(_round_step(tp_price, p["tick"]), p["tick"]) if p["tick"] else str(tp_price)
        results["fill_price"] = fill
        results["slippage"] = slip
        if abs(slip) > 0:
            logger.info("Slippage %s: plan %s → fill %s (%+.4g) — SL/TP digeser",
                        bsymbol, p["entry"], fill, slip)

        # 2. Stop loss — WAJIB; bila gagal, tutup posisi (fail-safe).
        #    Via Algo Order API (order kondisional tidak lagi diterima
        #    endpoint order biasa — error -4120).
        try:
            results["sl_order"] = self._place_conditional(
                bsymbol, exit_side, "STOP_MARKET", sl_s)
        except Exception as e:
            logger.error("SL GAGAL terpasang: %s — menutup posisi (fail-safe)", e)
            try:
                c.futures_create_order(
                    symbol=bsymbol, side=exit_side,
                    type=FUTURE_ORDER_TYPE_MARKET, quantity=qty_s,
                    reduceOnly=True)
                results["failsafe"] = "Posisi ditutup karena SL gagal terpasang"
            except Exception as e2:
                results["failsafe"] = (f"KRITIS: SL gagal DAN tutup posisi gagal "
                                       f"({e2}) — TUTUP MANUAL SEKARANG")
            results["error"] = f"SL gagal: {e}"
            return results

        # 3. Take profit (TP1, full close). TP2 dikelola manual/terpisah.
        try:
            results["tp_order"] = self._place_conditional(
                bsymbol, exit_side, "TAKE_PROFIT_MARKET", tp_s)
        except Exception as e:
            # Posisi tetap terlindungi SL — laporkan, jangan retry
            results["tp_error"] = str(e)
            logger.warning("TP gagal terpasang (posisi tetap ber-SL): %s", e)

        results["ok"] = True
        self._live_tracked.add(bsymbol)   # posisi milik sesi ini → boleh
                                          # ditutup otomatis saat setup mati

        # Jurnal live (live_trades.json) — konteks setup/skor + timestamp
        # untuk digabung dengan fill Binance oleh live_report.py.
        # PnL & harga exit TIDAK dicatat di sini: sumber kebenarannya API.
        entry_order = results["entry_order"]
        rec = {
            "status": "LIVE_OPEN",
            "symbol": bsymbol,
            "side": p["side"],
            "quantity": p["quantity"],
            "entry_plan": p["entry"],
            "entry_fill": fill,
            "slippage": round(slip, 8),
            "stop": sl_price,          # sudah digeser sebesar slippage
            "tp1": tp_price,
            "tp2": p["tp2"] + slip,
            "risk_usdt": p["risk_usdt"],
            "notional_usdt": p["notional_usdt"],
            "leverage": p["leverage"],
            "order_id": entry_order.get("orderId"),
            "opened_at": datetime.now().isoformat(timespec="seconds"),
            "opened_ts": int(time.time() * 1000),
        }
        if context:
            rec.update({k: context[k] for k in ("setup", "score", "grade")
                        if k in context})
        self._append_live_log(rec)

        logger.info("🔴 LIVE order terisi: %s %s qty %s (SL %s, TP1 %s)",
                    p["side"], bsymbol, qty_s, sl_s, tp_s)
        return results

    def is_tracked_live(self, symbol: str) -> bool:
        return self.to_binance_symbol(symbol) in self._live_tracked

    def partial_close(self, symbol: str, exit_price: float, new_stop: float,
                      fraction: float = 0.5) -> Dict[str, Any]:
        """Event PARTIAL entry engine (profit ≥ 0.5R): tutup `fraction`
        posisi + pindahkan SL ke breakeven (`new_stop` = harga entry).

        Live: reduceOnly market → cancel semua algo order (SL awal + TP1,
        TP1 closePosition konflik dengan trailing) → pasang STOP_MARKET
        closePosition baru di BE. Bila SL baru GAGAL terpasang, sisa posisi
        ditutup paksa (fail-safe — posisi tanpa SL tidak boleh hidup).
        Trailing selanjutnya dikelola engine (exchange SL tetap di BE
        sebagai lantai proteksi bila bot mati)."""
        bsymbol = self.to_binance_symbol(symbol)

        if self.paper_mode:
            logs = self._read_paper_log()
            n = 0
            for t in logs:
                if t.get("symbol") != bsymbol or t.get("status") != "PAPER_OPEN" \
                        or t.get("be_moved"):
                    continue
                qty_p = float(t["quantity"]) * fraction
                sgn = 1.0 if t["side"] == "LONG" else -1.0
                gross = (float(exit_price) - float(t["entry"])) * qty_p * sgn
                fee_p = float(exit_price) * qty_p * self.taker_fee_pct / 100.0
                t["partial_pnl"] = round(t.get("partial_pnl", 0.0) + gross - fee_p, 4)
                t["partial_exit"] = float(exit_price)
                t["partial_at"] = datetime.now().isoformat(timespec="seconds")
                t["quantity"] = round(float(t["quantity"]) - qty_p, 10)
                t["stop"] = float(new_stop)      # BE — proteksi sisa posisi
                t["be_moved"] = True
                n += 1
            if n:
                self._write_paper_log(logs)
                logger.info("📄 PAPER partial %s: %d posisi tutup %.0f%% @ %s, "
                            "SL → BE", bsymbol, n, fraction * 100, exit_price)
            return {"ok": True, "mode": "PAPER", "closed": n}

        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
        c = self.client()
        pos = c.futures_position_information(symbol=bsymbol)
        amt = float(pos[0]["positionAmt"]) if pos else 0.0
        if amt == 0:
            return {"ok": True, "mode": "LIVE", "note": "Tidak ada posisi"}
        flt = self._filters(bsymbol)
        step, tick = flt["stepSize"], flt["tickSize"]
        exit_side = SIDE_SELL if amt > 0 else SIDE_BUY
        result: Dict[str, Any] = {"ok": True, "mode": "LIVE"}

        # 1. Tutup sebagian (skip bila hasil rounding < minQty — posisi
        #    terlalu kecil untuk dibelah; BE + trail tetap jalan)
        qty_p = _round_step(abs(amt) * fraction, step)
        if qty_p >= flt["minQty"] and qty_p < abs(amt):
            result["partial_order"] = c.futures_create_order(
                symbol=bsymbol, side=exit_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=_fmt(qty_p, step), reduceOnly=True)
            result["closed_qty"] = qty_p
        else:
            result["note"] = "qty partial < minQty — hanya SL → BE"

        # 2. SL → BE: cancel algo lama (SL awal + TP1) → pasang SL baru.
        sl_s = _fmt(_round_step(float(new_stop), tick), tick) if tick else str(new_stop)
        try:
            self._cancel_all_conditional(bsymbol)
        except Exception as e:
            logger.warning("Cancel algo lama %s gagal: %s", bsymbol, e)
        try:
            result["sl_order"] = self._place_conditional(
                bsymbol, exit_side, "STOP_MARKET", sl_s)
        except Exception as e:
            logger.error("SL BE GAGAL terpasang %s: %s — menutup sisa posisi "
                         "(fail-safe)", bsymbol, e)
            try:
                pos2 = c.futures_position_information(symbol=bsymbol)
                amt2 = float(pos2[0]["positionAmt"]) if pos2 else 0.0
                if amt2 != 0:
                    c.futures_create_order(
                        symbol=bsymbol,
                        side=SIDE_SELL if amt2 > 0 else SIDE_BUY,
                        type=FUTURE_ORDER_TYPE_MARKET,
                        quantity=_fmt(abs(amt2), step), reduceOnly=True)
                result["failsafe"] = "Sisa posisi ditutup karena SL BE gagal"
                self._live_tracked.discard(bsymbol)
                self._mark_live_closed(bsymbol, "PARTIAL_FAILSAFE")
            except Exception as e2:
                result["failsafe"] = (f"KRITIS: SL BE gagal DAN tutup posisi "
                                      f"gagal ({e2}) — TUTUP MANUAL SEKARANG")
            result["ok"] = False
            result["error"] = f"SL BE gagal: {e}"
            return result

        # 3. Jurnal live: catat partial di record yang masih terbuka
        try:
            logs = self._read_live_log()
            for t in logs:
                if t.get("symbol") == bsymbol and t.get("status") == "LIVE_OPEN":
                    t["partial_at"] = datetime.now().isoformat(timespec="seconds")
                    t["partial_price"] = float(exit_price)
                    t["partial_qty"] = result.get("closed_qty", 0.0)
                    t["stop"] = float(new_stop)
                    t["be_moved"] = True
            self._write_live_log(logs)
        except Exception as e:
            logger.warning("Update jurnal partial %s gagal: %s", bsymbol, e)

        logger.info("🔴 LIVE partial %s: tutup %s @ ~%s, SL → BE %s",
                    bsymbol, result.get("closed_qty", 0), exit_price, sl_s)
        return result

    def close_live_trade(self, symbol: str, reason: str = "") -> Dict[str, Any]:
        """Setup engine berakhir (FADED/FLIP/STOP/TP2) → sinkronkan posisi
        LIVE milik sesi ini: tutup posisi (bila masih ada) + batalkan semua
        order sisa (SL/TP closePosition tidak otomatis hilang)."""
        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
        bsymbol = self.to_binance_symbol(symbol)
        c = self.client()
        pos = c.futures_position_information(symbol=bsymbol)
        amt = float(pos[0]["positionAmt"]) if pos else 0.0
        closed = False
        if amt != 0:
            step = self._filters(bsymbol)["stepSize"]
            c.futures_create_order(
                symbol=bsymbol,
                side=SIDE_SELL if amt > 0 else SIDE_BUY,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=_fmt(abs(amt), step), reduceOnly=True)
            closed = True
        try:
            c.futures_cancel_all_open_orders(symbol=bsymbol)
        except Exception as e:
            logger.warning("Cancel order sisa %s gagal: %s", bsymbol, e)
        # SL/TP kondisional hidup di Algo service — dibatalkan terpisah
        try:
            self._cancel_all_conditional(bsymbol)
        except Exception as e:
            logger.warning("Cancel algo order sisa %s gagal: %s", bsymbol, e)
        self._live_tracked.discard(bsymbol)
        self._mark_live_closed(bsymbol, reason or "SETUP_END")
        logger.info("🔴 LIVE exit %s (%s): posisi %s, order sisa dibatalkan",
                    bsymbol, reason, "ditutup" if closed else "sudah kosong")
        return {"ok": True, "closed_position": closed, "reason": reason,
                "note": ("posisi ditutup + order sisa dibatalkan"
                         if closed else "posisi sudah kosong — order sisa dibatalkan")}

    def close_position(self, symbol: str) -> Dict[str, Any]:
        bsymbol = self.to_binance_symbol(symbol)
        if self.paper_mode:
            closed = 0
            logs = self._read_paper_log()
            for t in logs:
                if t.get("symbol") == bsymbol and t.get("status") == "PAPER_OPEN":
                    t["status"] = "PAPER_CLOSED"
                    t["closed_at"] = datetime.now().isoformat(timespec="seconds")
                    closed += 1
            self._write_paper_log(logs)
            return {"ok": True, "mode": "PAPER", "closed": closed}

        from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
        c = self.client()
        pos = c.futures_position_information(symbol=bsymbol)
        amt = float(pos[0]["positionAmt"]) if pos else 0.0
        if amt == 0:
            return {"ok": True, "mode": "LIVE", "note": "Tidak ada posisi"}
        step = self._filters(bsymbol)["stepSize"]
        order = c.futures_create_order(
            symbol=bsymbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=_fmt(abs(amt), step), reduceOnly=True)
        self._live_tracked.discard(bsymbol)
        self._mark_live_closed(bsymbol, "MANUAL")
        return {"ok": True, "mode": "LIVE", "order": order}

    def close_paper_trades(self, symbol: str, exit_price: float,
                           reason: str) -> int:
        """Tutup semua posisi paper symbol ini saat setup berakhir.
        Exit: STOP → harga stop, TP2 → harga tp2, FLIP/FADED/TRAIL → harga
        pasar. pnl_usdt = NET (termasuk partial_pnl bila sempat partial TP).
        Mengisi pnl_usdt + result (WIN/LOSS) → statistik di paper_trades.json."""
        bsymbol = self.to_binance_symbol(symbol)
        logs = self._read_paper_log()
        n = 0
        for t in logs:
            if t.get("symbol") != bsymbol or t.get("status") != "PAPER_OPEN":
                continue
            if reason == "STOP":
                exit_p = float(t["stop"])
            elif reason == "TP2":
                exit_p = float(t["tp2"])
            else:
                exit_p = float(exit_price) if exit_price > 0 else float(t["entry"])
            qty = float(t["quantity"])
            sgn = 1.0 if t["side"] == "LONG" else -1.0
            gross = (exit_p - float(t["entry"])) * qty * sgn
            fee_open = float(t.get("fee_usdt", 0.0))
            fee_close = exit_p * qty * self.taker_fee_pct / 100.0
            # partial_pnl sudah net fee sisi partial-nya
            pnl = gross - fee_open - fee_close + float(t.get("partial_pnl", 0.0))
            t["status"] = "PAPER_CLOSED"
            t["closed_at"] = datetime.now().isoformat(timespec="seconds")
            t["exit"] = float(exit_p)
            t["close_reason"] = reason
            t["gross_usdt"] = round(gross, 2)
            t["fee_usdt"] = round(fee_open + fee_close, 2)
            t["pnl_usdt"] = round(pnl, 2)     # NET setelah fee taker 2 sisi
            t["result"] = "WIN" if pnl > 0 else "LOSS"
            n += 1
        if n:
            self._write_paper_log(logs)
            logger.info("PAPER close %s: %d posisi (%s)", bsymbol, n, reason)
        return n

    def realized_pnl_today(self) -> float:
        """PnL terealisasi sejak 00:00 waktu lokal — untuk guard max loss
        harian. Paper: dari paper_trades.json. Live: income Binance
        (REALIZED_PNL + COMMISSION + FUNDING_FEE) — panggil dari worker
        thread, ini network call."""
        if self.paper_mode:
            today = datetime.now().strftime("%Y-%m-%d")
            return sum(float(t.get("pnl_usdt", 0.0)) for t in self._read_paper_log()
                       if t.get("status") == "PAPER_CLOSED"
                       and str(t.get("closed_at", "")).startswith(today))
        midnight = datetime.combine(datetime.now().date(), datetime.min.time())
        start_ms = int(midnight.timestamp() * 1000)
        total = 0.0
        batch = self.client().futures_income_history(
            startTime=start_ms, limit=1000)
        for it in batch:
            if it.get("incomeType") in ("REALIZED_PNL", "COMMISSION",
                                        "FUNDING_FEE"):
                total += float(it.get("income", 0.0))
        return total

    def verify_connection(self) -> Dict[str, Any]:
        """Cek read-only: key valid + permission futures + balance USDT.
        Tidak menempatkan order."""
        try:
            bal = [b for b in self.client().futures_account_balance()
                   if b["asset"] == "USDT"]
            usdt = float(bal[0]["balance"]) if bal else 0.0
            return {"ok": True, "usdt_balance": usdt,
                    "note": "Key valid, akses Futures OK"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Paper log ─────────────────────────────────────────────────────

    def _read_paper_log(self) -> list:
        if PAPER_LOG_FILE.exists():
            try:
                return json.loads(PAPER_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _write_paper_log(self, logs: list):
        PAPER_LOG_FILE.write_text(
            json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_paper_log(self, trade: Dict[str, Any]):
        logs = self._read_paper_log()
        logs.append(trade)
        self._write_paper_log(logs)

    def _paper_realized_pnl(self) -> float:
        return sum(float(t.get("pnl_usdt", 0.0)) for t in self._read_paper_log()
                   if t.get("status") == "PAPER_CLOSED")

    # ── Live log ──────────────────────────────────────────────────────
    # Jurnal konteks (setup/skor/alasan exit) — pelengkap data fill Binance,
    # digabung oleh live_report.py. Bukan sumber PnL.

    def _read_live_log(self) -> list:
        if LIVE_LOG_FILE.exists():
            try:
                return json.loads(LIVE_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _write_live_log(self, logs: list):
        LIVE_LOG_FILE.write_text(
            json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_live_log(self, trade: Dict[str, Any]):
        logs = self._read_live_log()
        logs.append(trade)
        self._write_live_log(logs)

    def _mark_live_closed(self, bsymbol: str, reason: str):
        """Tandai semua record LIVE_OPEN symbol ini sebagai LIVE_CLOSED.
        closed_ts = saat exit terdeteksi (SL/TP exchange bisa terisi sedikit
        lebih awal) — live_report.py mencocokkan fill dengan margin waktu."""
        try:
            logs = self._read_live_log()
            n = 0
            for t in logs:
                if t.get("symbol") == bsymbol and t.get("status") == "LIVE_OPEN":
                    t["status"] = "LIVE_CLOSED"
                    t["close_reason"] = reason
                    t["closed_at"] = datetime.now().isoformat(timespec="seconds")
                    t["closed_ts"] = int(time.time() * 1000)
                    n += 1
            if n:
                self._write_live_log(logs)
        except Exception as e:
            logger.warning("Update jurnal live %s gagal: %s", bsymbol, e)
