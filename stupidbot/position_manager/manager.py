"""Manajemen posisi aktif: partial TP, breakeven, dan ATR trailing stop.

Aturan candle yang sama menyentuh SL dan TP diselesaikan secara PESIMIS:
SL dianggap kena lebih dulu.
"""
from dataclasses import dataclass, field

from config.settings import Settings
from core.models import Candle, Direction, Signal


@dataclass
class Fill:
    price: float
    qty: float
    reason: str  # SL | BE | TRAIL | PARTIAL_TP | TP
    ts: int


@dataclass
class Position:
    signal: Signal
    qty: float
    init_qty: float
    risk_amount: float
    risk_pct: float = 0.0
    sl: float = 0.0
    partial_done: bool = False
    mfe_r: float = 0.0
    mae_r: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self):
        self.sl = self.signal.sl

    @property
    def direction(self) -> Direction:
        return self.signal.direction

    @property
    def entry(self) -> float:
        return self.signal.entry

    @property
    def stop_dist(self) -> float:
        return abs(self.signal.entry - self.signal.sl)


class PositionManager:
    def __init__(self, cfg: Settings):
        self.cfg = cfg

    def on_candle(self, pos: Position, candle: Candle, atr: float | None) -> bool:
        """Proses satu candle closed. Kembalikan True bila posisi tertutup penuh."""
        d = 1 if pos.direction == Direction.LONG else -1
        stop = pos.stop_dist
        adverse = candle.low if d == 1 else candle.high
        favorable = candle.high if d == 1 else candle.low

        pos.mae_r = max(pos.mae_r, d * (pos.entry - adverse) / stop)
        pos.mfe_r = max(pos.mfe_r, d * (favorable - pos.entry) / stop)

        # 1. cek SL dulu (pesimis)
        if d * (adverse - pos.sl) <= 0:
            # gap melewati SL → isi di open yang lebih buruk
            px = candle.open if d * (candle.open - pos.sl) < 0 else pos.sl
            if pos.sl == pos.entry:
                reason = "BE"
            elif d * (pos.sl - pos.entry) > 0:
                reason = "TRAIL"
            else:
                reason = "SL"
            pos.fills.append(Fill(px, pos.qty, reason, candle.ts))
            pos.qty = 0.0
            return True

        # 2. partial take profit
        partial_px = pos.entry + d * self.cfg.partial_tp_r * stop
        if (not pos.partial_done and self.cfg.partial_fraction > 0
                and d * (favorable - partial_px) >= 0):
            q = pos.qty * self.cfg.partial_fraction
            pos.fills.append(Fill(partial_px, q, "PARTIAL_TP", candle.ts))
            pos.qty -= q
            pos.partial_done = True
            if self.cfg.be_after_partial and d * (pos.entry - pos.sl) > 0:
                pos.sl = pos.entry  # amankan: breakeven

        # 3. take profit final
        if d * (favorable - pos.signal.tp) >= 0:
            pos.fills.append(Fill(pos.signal.tp, pos.qty, "TP", candle.ts))
            pos.qty = 0.0
            return True

        # 4. trailing stop — hanya setelah profit cukup, jangan terlalu dini
        r_close = d * (candle.close - pos.entry) / stop
        if r_close >= self.cfg.trail_start_r and atr:
            new_sl = candle.close - d * self.cfg.trail_atr_mult * atr
            if d * (new_sl - pos.sl) > 0:
                pos.sl = new_sl

        return False
