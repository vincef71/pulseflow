from collections import deque

import numpy as np

from pulseflow.config.settings import WHALE_THRESHOLDS_USD, WHALE_ADAPTIVE

TIERS = ("SMALL", "MEDIUM", "LARGE", "BLOCK")
TIER_INDEX = {"SMALL": 0, "MEDIUM": 1, "LARGE": 2, "BLOCK": 3}
_ADAPTIVE_TIERS = ("MEDIUM", "LARGE", "BLOCK")


class WhaleClassifier:
    """Assigns a size tier to each trade based on its USD notional.

    ADAPTIF: ambang MEDIUM/LARGE/BLOCK dihitung dari persentil rolling
    notional symbol ini sendiri (P95/P99/P99.9, lihat WHALE_ADAPTIVE),
    dengan floor absolut. Sebelum sampel cukup (warm-up) memakai tabel
    statis WHALE_THRESHOLDS_USD — tabel itu juga fallback bila adaptif
    dimatikan. classify() dipanggil per trade dari thread feed; pembaca
    lain (engine loop) hanya membaca dict yang diganti atomik.
    """

    def __init__(self, symbol: str):
        self._static = {
            tier: WHALE_THRESHOLDS_USD[tier].get(symbol, WHALE_THRESHOLDS_USD[tier]["__default__"])
            for tier in TIERS
        }
        cfg = WHALE_ADAPTIVE
        self._enabled = bool(cfg.get("enabled", True))
        self._window: deque = deque(maxlen=int(cfg["window_trades"]))
        self._warmup = int(cfg["warmup_trades"])
        self._every = int(cfg["recompute_every"])
        self._pcts = dict(cfg["percentiles"])
        self._floors = dict(cfg["floors_usd"])
        self._since_recompute = 0
        self._adaptive = None  # dict tier→USD saat sudah warm

    def _recompute(self):
        arr = np.fromiter(self._window, dtype=np.float64)
        pct = np.percentile(arr, [self._pcts[t] for t in _ADAPTIVE_TIERS])
        # Persentil monotonic + floor monotonic → max keduanya tetap monotonic
        self._adaptive = {
            t: max(float(p), self._floors[t])
            for t, p in zip(_ADAPTIVE_TIERS, pct)
        }
        self._since_recompute = 0

    def classify(self, notional: float) -> str:
        if self._enabled:
            self._window.append(notional)
            self._since_recompute += 1
            if len(self._window) >= self._warmup and (
                    self._adaptive is None
                    or self._since_recompute >= self._every):
                self._recompute()
        thr = self._adaptive or self._static
        if notional >= thr["BLOCK"]:
            return "BLOCK"
        if notional >= thr["LARGE"]:
            return "LARGE"
        if notional >= thr["MEDIUM"]:
            return "MEDIUM"
        return "SMALL"

    def is_whale(self, notional: float) -> bool:
        """True for LARGE or BLOCK trades."""
        return notional >= self.large

    @property
    def large(self) -> float:
        """Ambang LARGE efektif saat ini (adaptif bila sudah warm)."""
        return (self._adaptive or self._static)["LARGE"]

    @property
    def is_adaptive(self) -> bool:
        return self._adaptive is not None

    @property
    def thresholds(self) -> dict:
        eff = self._adaptive or self._static
        return {"SMALL": self._static["SMALL"], **{t: eff[t] for t in _ADAPTIVE_TIERS}}
