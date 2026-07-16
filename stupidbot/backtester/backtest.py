"""Backtester event-driven, satu candle closed pada satu waktu.

Anti-lookahead:
- Candle Daily hanya diumpankan ke DailyBiasEngine SETELAH hari itu close penuh
  (open_time + 1 hari <= open_time candle entry yang sedang diproses).
- Swing TF entry terkonfirmasi dengan lag k candle (di StructureTracker).
- Entry dieksekusi di harga close candle sinyal; manajemen posisi baru mulai
  candle berikutnya.
- Bila SL dan TP tersentuh di candle yang sama, SL dianggap kena dulu (pesimis).

Lapisan proteksi akun (risk_manager):
- AdaptiveRisk  : tier risiko naik hanya saat equity high baru, turun saat dd.
- EquityGuard   : dd harian / dd total menghentikan entry baru sementara.
- TradeThrottle : batas trade per bulan + jeda antar entry.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import Settings
from core.models import Candle, Direction, Signal, Trade, Trend
from daily_bias.bias import DailyBiasEngine
from entry_engine.engine import EntryEngine
from position_manager.manager import Fill, Position, PositionManager
from risk_manager.risk import AdaptiveRisk, EquityGuard, TradeThrottle, position_size

DAY_MS = 86_400_000

# fill dengan alasan ini = limit order (maker); selain itu market/stop (taker)
MAKER_REASONS = {"TP", "PARTIAL_TP"}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


@dataclass
class PendingEntry:
    """Order limit entry yang belum terisi. Ukuran sudah dihitung saat
    penempatan (balance saat itu)."""
    signal: Signal
    qty: float
    risk_amount: float
    risk_pct: float
    placed_ts: int


def pending_action(pending: PendingEntry, candle: Candle, bias: Direction,
                   entry_trend: Trend) -> str:
    """Nasib order limit pada candle closed ini: FILL / CANCEL / WAIT.

    Urutan pesimis: sentuhan harga dicek dulu (fill terjadi intrabar,
    sebelum kondisi close bisa dievaluasi). Cancel = zona/struktur rusak:
    bias Daily berubah, trend TF entry tidak lagi searah, atau harga
    breakout meninggalkan pullback tanpa mengisi order.
    """
    sig = pending.signal
    if sig.direction == Direction.LONG:
        if candle.low <= sig.entry:
            return "FILL"
        if bias != Direction.LONG or entry_trend != Trend.UP:
            return "CANCEL"
        if candle.close > sig.leg_high:
            return "CANCEL"  # breakout tanpa kita — pullback selesai
    else:
        if candle.high >= sig.entry:
            return "FILL"
        if bias != Direction.SHORT or entry_trend != Trend.DOWN:
            return "CANCEL"
        if candle.close < sig.leg_low:
            return "CANCEL"
    return "WAIT"


def fill_pending(pending: PendingEntry, candle: Candle) -> tuple[Position, bool]:
    """Isi order limit di candle ini. Kembalikan (Position, stopped_same_candle).
    Pesimis: bila candle yang sama juga menyentuh SL, posisi langsung stop out."""
    sig = pending.signal
    pos = Position(signal=sig, qty=pending.qty, init_qty=pending.qty,
                   risk_amount=pending.risk_amount, risk_pct=pending.risk_pct,
                   opened_ts=candle.ts)
    d = 1 if sig.direction == Direction.LONG else -1
    adverse = candle.low if d == 1 else candle.high
    if d * (adverse - sig.sl) <= 0:
        # gap melewati SL → isi di open yang lebih buruk
        px = candle.open if d * (candle.open - sig.sl) < 0 else sig.sl
        pos.fills.append(Fill(px, pos.qty, "SL", candle.ts))
        pos.qty = 0.0
        pos.mae_r = 1.0
        return pos, True
    return pos, False


def close_position(cfg: Settings, symbol: str, pos: Position, exit_ts: int) -> tuple[Trade, float]:
    """Bangun catatan Trade lengkap dari posisi yang seluruh fill-nya selesai."""
    sig = pos.signal
    d = 1 if sig.direction == Direction.LONG else -1
    gross = sum(d * (f.price - sig.entry) * f.qty for f in pos.fills)
    # entry = limit (maker); exit: TP/partial = limit (maker), SL/trail = taker
    fees = sig.entry * pos.init_qty * cfg.maker_fee_pct / 100.0
    for f in pos.fills:
        rate = cfg.maker_fee_pct if f.reason in MAKER_REASONS else cfg.fee_pct
        fees += f.price * f.qty * rate / 100.0
    pnl = gross - fees

    exit_qty = sum(f.qty for f in pos.fills)
    exit_price = sum(f.price * f.qty for f in pos.fills) / exit_qty
    exit_reason = pos.fills[-1].reason
    if any(f.reason == "PARTIAL_TP" for f in pos.fills) and exit_reason != "PARTIAL_TP":
        exit_reason = f"PARTIAL_TP+{exit_reason}"

    entry_ts = pos.opened_ts or sig.ts
    trade = Trade(
        entry_ts=entry_ts,
        entry_date=_iso(entry_ts),
        exit_date=_iso(exit_ts),
        symbol=symbol,
        daily_bias=sig.direction.value,
        entry_reason=sig.reason,
        pattern=sig.pattern,
        atr=round(sig.atr, 8),
        entry=sig.entry,
        stop_loss=sig.sl,
        take_profit=sig.tp,
        risk_amount=round(pos.risk_amount, 2),
        risk_pct=pos.risk_pct,
        rr_planned=round(sig.rr, 2),
        exit_reason=exit_reason,
        exit_price=round(exit_price, 8),
        pnl=round(pnl, 2),
        r_multiple=round(pnl / pos.risk_amount, 2) if pos.risk_amount else 0.0,
        mfe_r=round(pos.mfe_r, 2),
        mae_r=round(pos.mae_r, 2),
    )
    return trade, pnl


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
        adaptive = AdaptiveRisk(cfg, self.start_balance)
        guard = EquityGuard(cfg, self.start_balance)
        throttle = TradeThrottle(cfg)

        balance = self.start_balance
        trades: list[Trade] = []
        equity: list[tuple[int, float]] = [(trade_from_ts, balance)]
        pos: Position | None = None
        pending: PendingEntry | None = None
        di = 0

        def _record_close(p: Position, ts: int) -> None:
            nonlocal balance
            trade, pnl = close_position(cfg, self.symbol, p, ts)
            balance += pnl
            adaptive.on_trade_close(balance)
            guard.on_trade_close(ts, balance)
            trades.append(trade)
            equity.append((ts, balance))

        for candle in entry:
            # umpankan candle Daily yang sudah tuntas sebelum candle entry ini
            while di < len(daily) and daily[di].ts + DAY_MS <= candle.ts:
                bias_engine.update(daily[di])
                di += 1

            entry_engine.update(candle)
            guard.on_candle(candle.ts, balance)

            if pos is not None:
                if candle.ts > pos.opened_ts:  # manajemen mulai candle berikutnya
                    closed = pos_manager.on_candle(pos, candle, entry_engine.atr.value)
                    if closed:
                        _record_close(pos, candle.ts)
                        pos = None
                continue

            if pending is not None:
                if not guard.allowed(candle.ts):
                    throttle.on_cancel()
                    pending = None  # equity protection aktif → tarik order
                    continue
                bias, _ = bias_engine.bias()
                act = pending_action(pending, candle, bias, entry_engine.tracker.trend)
                if act == "CANCEL":
                    throttle.on_cancel()
                    pending = None
                elif act == "FILL":
                    pos, stopped = fill_pending(pending, candle)
                    pending = None
                    if stopped:
                        _record_close(pos, candle.ts)
                        pos = None
                continue

            if candle.ts < trade_from_ts:
                continue  # periode warmup: bangun struktur, jangan trading
            if not guard.allowed(candle.ts):
                continue  # equity protection aktif
            if not throttle.allowed(candle.ts):
                continue  # kuota bulanan / cooldown antar entry

            bias, reason = bias_engine.bias()
            if bias == Direction.NEUTRAL:
                continue

            signal = entry_engine.check(bias, reason, bias_engine)
            if signal is None:
                continue

            risk_pct = adaptive.current_pct
            qty, risk_amount = position_size(balance, risk_pct, signal.entry, signal.sl)
            if qty <= 0:
                continue
            pending = PendingEntry(signal=signal, qty=qty, risk_amount=risk_amount,
                                   risk_pct=risk_pct, placed_ts=candle.ts)
            throttle.on_entry(candle.ts)

        # posisi masih terbuka di akhir data → tutup di close terakhir
        if pos is not None and entry:
            last = entry[-1]
            pos.fills.append(Fill(last.close, pos.qty, "END_OF_DATA", last.ts))
            pos.qty = 0.0
            trade, pnl = close_position(cfg, self.symbol, pos, last.ts)
            balance += pnl
            trades.append(trade)
            equity.append((last.ts, balance))

        stats = summarize(trades, equity, self.start_balance, balance)
        return {"trades": trades, "equity": equity, "stats": stats,
                "final_balance": balance, "halts": guard.halts}


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

    span_months = max((equity[-1][0] - equity[0][0]) / (30.44 * DAY_MS), 1e-9)

    return {
        "trades": len(trades),
        "trades_per_month": len(trades) / span_months,
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
