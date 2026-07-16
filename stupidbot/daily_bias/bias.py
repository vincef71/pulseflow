"""Bias arah dari timeframe Daily — satu-satunya penentu boleh long/short.

Jika Daily netral, tidak ada trade. Titik.
"""
from config.settings import Settings
from core.atr import ATRCalculator
from core.models import Candle, Direction, SwingType, Trend
from market_structure.structure import StructureTracker


class DailyBiasEngine:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.tracker = StructureTracker(cfg.daily_swing_k)
        self.atr = ATRCalculator(cfg.atr_period)
        self.last: Candle | None = None

    def update(self, candle: Candle) -> None:
        """Feed hanya candle Daily yang sudah close penuh."""
        self.tracker.update(candle)
        self.atr.update(candle)
        self.last = candle

    def bias(self) -> tuple[Direction, str]:
        if self.last is None or not self.atr.ready:
            return Direction.NEUTRAL, "data Daily belum cukup"

        atr_pct = 100.0 * self.atr.value / self.last.close
        if atr_pct < self.cfg.min_daily_atr_pct:
            return Direction.NEUTRAL, f"volatilitas Daily rendah (ATR {atr_pct:.2f}%)"

        trend = self.tracker.trend
        if trend == Trend.UP:
            sl = self.tracker.last_swing(SwingType.LOW)
            if sl and self.last.close > sl.price:
                return Direction.LONG, "Daily uptrend (HH/HL), struktur intact"
        elif trend == Trend.DOWN:
            sh = self.tracker.last_swing(SwingType.HIGH)
            if sh and self.last.close < sh.price:
                return Direction.SHORT, "Daily downtrend (LH/LL), struktur intact"

        return Direction.NEUTRAL, "struktur Daily netral/range"

    # target struktural untuk perhitungan RR
    def nearest_high_above(self, price: float) -> float | None:
        return self.tracker.nearest_high_above(price)

    def nearest_low_below(self, price: float) -> float | None:
        return self.tracker.nearest_low_below(price)
