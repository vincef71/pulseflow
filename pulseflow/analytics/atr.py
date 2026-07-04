import time
from collections import deque
from typing import Dict, Any, Optional


class ATRCalculator:
    """
    Average True Range over time-aggregated OHLC bars built from the live
    price stream.

    The engine has no candle feed — it only sees a price each 100 ms tick.
    This class folds those snapshots into `bar_seconds` OHLC bars (open =
    first price, high/low = extremes seen, close = last price), then applies
    Wilder's smoothing to the True Range of completed bars.

    True Range = max(high - low,
                     |high - prev_close|,
                     |low  - prev_close|)

    ATR (Wilder) = (prev_ATR * (period - 1) + TR) / period
    During the seed phase (first `period` bars) it is a simple mean of TR.

    A rolling window of ATR% (ATR as a fraction of price) is kept so the
    current reading can be ranked into a volatility regime.
    """

    def __init__(self, bar_seconds: int = 5, period: int = 14,
                 regime_window: int = 240):
        self.bar_seconds = bar_seconds
        self.period = period

        # Active (forming) bar
        self._open:  Optional[float] = None
        self._high:  Optional[float] = None
        self._low:   Optional[float] = None
        self._close: Optional[float] = None
        self._bar_start: Optional[float] = None

        # ATR state
        self._prev_close: Optional[float] = None
        self.atr: float = 0.0
        self.bars_completed: int = 0
        self._tr_seed: list = []

        # Regime ranking — rolling ATR% samples
        self._atr_pct_window = deque(maxlen=regime_window)
        self.last_price: float = 0.0

    # ── Ingestion ─────────────────────────────────────────────────────

    def update(self, price: float, now: Optional[float] = None) -> None:
        """Feed one price snapshot; rolls the bar over when its window ends."""
        if price is None or price <= 0.0:
            return
        self.last_price = price
        now = now if now is not None else time.time()

        if self._bar_start is None:
            self._start_bar(price, now)
            return

        if now - self._bar_start < self.bar_seconds:
            self._high = max(self._high, price)
            self._low  = min(self._low, price)
            self._close = price
        else:
            self._close_bar()
            self._start_bar(price, now)

    def _start_bar(self, price: float, now: float) -> None:
        self._open = self._high = self._low = self._close = price
        self._bar_start = now

    def _close_bar(self) -> None:
        high, low, close = self._high, self._low, self._close
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low,
                     abs(high - self._prev_close),
                     abs(low - self._prev_close))

        if self.bars_completed < self.period:
            self._tr_seed.append(tr)
            self.atr = sum(self._tr_seed) / len(self._tr_seed)
        else:
            self.atr = (self.atr * (self.period - 1) + tr) / self.period

        self._prev_close = close
        self.bars_completed += 1

        if close > 0.0:
            self._atr_pct_window.append(self.atr / close * 100.0)

    # ── Output ────────────────────────────────────────────────────────

    def _regime(self, atr_pct: float) -> str:
        """Percentile-rank the current ATR% against recent history."""
        samples = self._atr_pct_window
        if len(samples) < 10:
            return "warming"
        rank = sum(1 for v in samples if v <= atr_pct) / len(samples)
        if rank < 0.30:
            return "calm"
        if rank < 0.70:
            return "normal"
        if rank < 0.90:
            return "elevated"
        return "explosive"

    def snapshot(self) -> Dict[str, Any]:
        price = self.last_price
        atr_pct = (self.atr / price * 100.0) if price > 0.0 else 0.0
        return {
            "value":          self.atr,
            "pct":            atr_pct,
            "regime":         self._regime(atr_pct),
            "bars_completed": self.bars_completed,
            "ready":          self.bars_completed >= self.period,
            "bar_seconds":    self.bar_seconds,
            "period":         self.period,
        }
