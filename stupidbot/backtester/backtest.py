"""Backtester event-driven, satu candle closed pada satu waktu.

Anti-lookahead:
- Candle Daily hanya diumpankan ke DailyBiasEngine SETELAH hari itu close penuh
  (open_time + 1 hari <= open_time candle entry yang sedang diproses).
- Swing TF entry terkonfirmasi dengan lag k candle (di StructureTracker).
- Entry dieksekusi di harga close candle sinyal; manajemen posisi baru mulai
  candle berikutnya.
- Bila SL dan TP tersentuh di candle yang sama, SL dianggap kena dulu (pesimis).
"""
from datetime import datetime, timezone

from config.settings import Settings
from core.models import Candle, Direction, Trade
from daily_bias.bias import DailyBiasEngine
from entry_engine.engine import EntryEngine
from position_manager.manager import Position, PositionManager
from risk_manager.risk import position_size

DAY_MS = 86_400_000


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class Backtester:
    def __init__(self, cfg: Settings, symbol: str, entry_tf: str, start_balance: float):
        self.cfg = cfg
        self.symbol = symbol
        self.entry_tf = entry_tf
        self.start_balance = start_balance

    def run(self, daily: list[Candle], entry: list[Candle], trade_from_ts: int) -> dict:
        cfg = self.cfg
        bias_engine = DailyBiasEngine(cfg)
        entry_engine = EntryEngine(cfg)
        pos_manager = PositionManager(cfg)

        balance = self.start_balance
        trades: list[Trade] = []
        equity: list[tuple[int, float]] = [(trade_from_ts, balance)]
        pos: Position | None = None
        di = 0

        for candle in entry:
            # umpankan candle Daily yang sudah tuntas sebelum candle entry ini
            while di < len(daily) and daily[di].ts + DAY_MS <= candle.ts:
                bias_engine.update(daily[di])
                di += 1

            entry_engine.update(candle)

            if pos is not None:
                if candle.ts > pos.signal.ts:  # manajemen mulai candle berikutnya
                    closed = pos_manager.on_candle(pos, candle, entry_engine.atr.value)
                    if closed:
                        trade, pnl = self._close_trade(pos, candle.ts)
                        balance += pnl
                        trades.append(trade)
                        equity.append((candle.ts, balance))
                        pos = None
                continue

            if candle.ts < trade_from_ts:
                continue  # periode warmup: bangun struktur, jangan trading

            bias, reason = bias_engine.bias()
            if bias == Direction.NEUTRAL:
                continue

            signal = entry_engine.check(bias, reason, bias_engine)
            if signal is None:
                continue

            qty, risk_amount = position_size(balance, cfg.risk_per_trade_pct,
                                             signal.entry, signal.sl)
            if qty <= 0:
                continue
            pos = Position(signal=signal, qty=qty, init_qty=qty, risk_amount=risk_amount)

        # posisi masih terbuka di akhir data → tutup di close terakhir
        if pos is not None and entry:
            last = entry[-1]
            from position_manager.manager import Fill
            pos.fills.append(Fill(last.close, pos.qty, "END_OF_DATA", last.ts))
            pos.qty = 0.0
            trade, pnl = self._close_trade(pos, last.ts)
            balance += pnl
            trades.append(trade)
            equity.append((last.ts, balance))

        stats = summarize(trades, equity, self.start_balance, balance)
        return {"trades": trades, "equity": equity, "stats": stats,
                "final_balance": balance}

    # ------------------------------------------------------------------ #
    def _close_trade(self, pos: Position, exit_ts: int) -> tuple[Trade, float]:
        sig = pos.signal
        d = 1 if sig.direction == Direction.LONG else -1
        gross = sum(d * (f.price - sig.entry) * f.qty for f in pos.fills)
        notional = sig.entry * pos.init_qty + sum(f.price * f.qty for f in pos.fills)
        fees = notional * self.cfg.fee_pct / 100.0
        pnl = gross - fees

        exit_qty = sum(f.qty for f in pos.fills)
        exit_price = sum(f.price * f.qty for f in pos.fills) / exit_qty
        exit_reason = pos.fills[-1].reason
        if any(f.reason == "PARTIAL_TP" for f in pos.fills) and exit_reason != "PARTIAL_TP":
            exit_reason = f"PARTIAL_TP+{exit_reason}"

        trade = Trade(
            entry_ts=sig.ts,
            entry_date=_iso(sig.ts),
            exit_date=_iso(exit_ts),
            symbol=self.symbol,
            daily_bias=sig.direction.value,
            entry_reason=sig.reason,
            pattern=sig.pattern,
            atr=round(sig.atr, 8),
            entry=sig.entry,
            stop_loss=sig.sl,
            take_profit=sig.tp,
            risk_amount=round(pos.risk_amount, 2),
            rr_planned=round(sig.rr, 2),
            exit_reason=exit_reason,
            exit_price=round(exit_price, 8),
            pnl=round(pnl, 2),
            r_multiple=round(pnl / pos.risk_amount, 2) if pos.risk_amount else 0.0,
            mfe_r=round(pos.mfe_r, 2),
            mae_r=round(pos.mae_r, 2),
        )
        return trade, pnl


# ---------------------------------------------------------------------- #
def summarize(trades: list[Trade], equity: list[tuple[int, float]],
              start_balance: float, final_balance: float) -> dict:
    if not trades:
        return {"trades": 0, "final_balance": final_balance}

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    peak = equity[0][1]
    max_dd = 0.0
    for _, bal in equity:
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak * 100.0)

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100.0,
        "expectancy_r": sum(t.r_multiple for t in trades) / len(trades),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "avg_win_r": sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0,
        "total_pnl": final_balance - start_balance,
        "return_pct": (final_balance / start_balance - 1.0) * 100.0,
        "max_dd_pct": max_dd,
        "final_balance": final_balance,
    }


def walkforward_folds(trades: list[Trade], start_ms: int, end_ms: int,
                      folds: int, start_balance: float) -> list[dict]:
    """Bagi periode jadi beberapa fold dan nilai stabilitas performa per fold.

    Bukan optimasi parameter — hanya uji generalisasi lintas periode.
    """
    span = (end_ms - start_ms) // folds
    out = []
    for i in range(folds):
        s = start_ms + i * span
        e = end_ms if i == folds - 1 else s + span
        sub = [t for t in trades if s <= t.entry_ts < e]
        bal = start_balance
        eq = [(s, bal)]
        for t in sub:
            bal += t.pnl
            eq.append((t.entry_ts, bal))
        out.append({"fold": i + 1, "start": _iso(s), "end": _iso(e),
                    "stats": summarize(sub, eq, start_balance, bal)})
    return out
