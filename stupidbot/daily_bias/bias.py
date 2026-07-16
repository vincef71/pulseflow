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

    def structure_score(self) -> float:
        """Skor 0-110 kualitas struktur Daily searah bias — untuk portfolio mode
        memilih aset dengan struktur terbaik. 0 bila bias netral."""
        direction, _ = self.bias()
        if direction == Direction.NEUTRAL:
            return 0.0
        good = ("HH", "HL") if direction == Direction.LONG else ("LH", "LL")
        swings = self.tracker.swings[-6:]
        if not swings:
            return 0.0
        aligned = sum(1 for s in swings if s.label in good) / len(swings)
        score = aligned * 100.0
        # bonus bila momentum terakhir mengonfirmasi (BOS searah bias)
        ev = self.tracker.events[-1] if self.tracker.events else None
        if ev and ev.type == ("BOS_UP" if direction == Direction.LONG else "BOS_DOWN"):
            score += 10.0
        return score

    # target struktural untuk perhitungan RR
    def nearest_high_above(self, price: float) -> float | None:
        return self.tracker.nearest_high_above(price)

    def nearest_low_below(self, price: float) -> float | None:
        return self.tracker.nearest_low_below(price)
