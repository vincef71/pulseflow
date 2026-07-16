"""ATR (Wilder) — satu-satunya indikator yang diizinkan sistem ini.

Dipakai hanya untuk: jarak stop loss, position sizing, filter volatilitas,
dan trailing stop.
"""
from core.models import Candle


class ATRCalculator:
    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._trs: list[float] = []
        self._prev_close: float | None = None

    def update(self, c: Candle) -> float | None:
        if self._prev_close is None:
            tr = c.high - c.low
        else:
            tr = max(
                c.high - c.low,
                abs(c.high - self._prev_close),
                abs(c.low - self._prev_close),
            )
        self._prev_close = c.close

        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self.value = sum(self._trs) / self.period
        else:
            # smoothing Wilder
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None
