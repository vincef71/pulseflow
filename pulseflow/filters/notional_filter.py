from pulseflow.config.settings import MIN_NOTIONAL_USD


class NotionalFilter:
    """Hard floor: reject trades whose USD notional is below the per-symbol minimum."""

    def __init__(self, symbol: str):
        self._threshold = MIN_NOTIONAL_USD.get(symbol, MIN_NOTIONAL_USD["__default__"])

    def passes(self, notional: float) -> bool:
        return notional >= self._threshold

    @property
    def threshold(self) -> float:
        return self._threshold
