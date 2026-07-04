from pulseflow.config.settings import WHALE_THRESHOLDS_USD

TIERS = ("SMALL", "MEDIUM", "LARGE", "BLOCK")
TIER_INDEX = {"SMALL": 0, "MEDIUM": 1, "LARGE": 2, "BLOCK": 3}


class WhaleClassifier:
    """Assigns a size tier to each trade based on its USD notional."""

    def __init__(self, symbol: str):
        self._thresholds = {
            tier: WHALE_THRESHOLDS_USD[tier].get(symbol, WHALE_THRESHOLDS_USD[tier]["__default__"])
            for tier in TIERS
        }

    def classify(self, notional: float) -> str:
        if notional >= self._thresholds["BLOCK"]:
            return "BLOCK"
        if notional >= self._thresholds["LARGE"]:
            return "LARGE"
        if notional >= self._thresholds["MEDIUM"]:
            return "MEDIUM"
        return "SMALL"

    def is_whale(self, notional: float) -> bool:
        """True for LARGE or BLOCK trades."""
        return notional >= self._thresholds["LARGE"]

    @property
    def thresholds(self) -> dict:
        return dict(self._thresholds)
