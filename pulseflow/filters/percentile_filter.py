import numpy as np
from collections import deque
from pulseflow.config.settings import PERCENTILE_FILTER


class PercentileFilter:
    """
    Adaptive filter: keeps a rolling window of trade notionals and only
    passes trades whose notional exceeds the Pth percentile of that window.

    The window is seeded with trades that already passed the notional floor,
    so the percentile tracks the distribution of genuine (non-bot) activity.
    """

    def __init__(self):
        self._window: deque = deque(maxlen=PERCENTILE_FILTER["window_trades"])
        self._percentile: int = PERCENTILE_FILTER["percentile"]
        self._threshold: float = 0.0

    def update(self, notional: float):
        """Add a notional value to the rolling window and recompute threshold."""
        self._window.append(notional)
        if len(self._window) >= 10:
            self._threshold = float(np.percentile(self._window, self._percentile))

    def passes(self, notional: float) -> bool:
        if len(self._window) < 10:
            return True  # warm-up: let everything through
        return notional >= self._threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def window_size(self) -> int:
        return len(self._window)
