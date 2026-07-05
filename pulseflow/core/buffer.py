import numpy as np
import time
from typing import Dict, Any, Tuple
from pulseflow.filters.notional_filter import NotionalFilter
from pulseflow.filters.whale_classifier import WhaleClassifier, TIER_INDEX
from pulseflow.filters.percentile_filter import PercentileFilter


class RingBuffer:
    """
    Fixed-size pre-allocated NumPy ring buffer for numerical series.
    Provides fast sliding window aggregations (sum, mean, std) without resizing.
    """
    def __init__(self, size: int, num_features: int = 1):
        self.size = size
        self.num_features = num_features
        self.data = np.zeros((size, num_features), dtype=np.float64)
        self.head = 0
        self.filled = False

    def append(self, values: np.ndarray):
        self.data[self.head] = values
        self.head = (self.head + 1) % self.size
        if self.head == 0:
            self.filled = True

    def get_last_n(self, n: int) -> np.ndarray:
        if n > self.size:
            n = self.size
        if not self.filled and self.head < n:
            return self.data[:self.head]
        indices = (np.arange(self.head - n, self.head)) % self.size
        return self.data[indices]

    def get_stats(self, n: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        subset = self.get_last_n(n)
        if len(subset) == 0:
            return (np.zeros(self.num_features),
                    np.zeros(self.num_features),
                    np.zeros(self.num_features))
        sums  = np.sum(subset, axis=0)
        means = np.mean(subset, axis=0)
        stds  = np.std(subset, axis=0)
        stds[stds == 0.0] = 1e-8
        return sums, means, stds


class MarketTicker:
    """
    Aggregates incoming trades into 100 ms buckets, then commits them to
    four parallel ring-buffer layers:

    buffer          — raw (every trade)
    filtered_buffer — trades above min-notional floor AND rolling P70
    whale_buffer    — LARGE/BLOCK tier trades only
    flow_buffer     — USD notional by size tier [small, medium, large, block]

    Liquidation and OI data is tracked in dedicated buffers as before.
    """

    def __init__(self, symbol: str = "__default__", max_seconds: int = 300):
        self.symbol = symbol
        self.tick_rate = 10
        self.max_ticks = max_seconds * self.tick_rate

        # ── Raw buffer: [trade_count, volume, delta, price_diff] ──────
        self.buffer = RingBuffer(self.max_ticks, num_features=4)

        # ── Filtered buffer: [trade_count, volume, delta] ─────────────
        # Trades that pass both the notional floor and the P70 adaptive gate.
        self.filtered_buffer = RingBuffer(self.max_ticks, num_features=3)

        # ── Whale buffer: [trade_count, volume, delta] ────────────────
        # LARGE and BLOCK tier trades only.
        self.whale_buffer = RingBuffer(self.max_ticks, num_features=3)

        # ── Flow buffer: [small_usd, medium_usd, large_usd, block_usd] ─
        # USD notional accumulated per tier each tick, for % composition.
        self.flow_buffer = RingBuffer(self.max_ticks, num_features=4)

        # ── Liquidation buffer: [count, short_liq_usd, long_liq_usd] ──
        self.liq_buffer = RingBuffer(self.max_ticks, num_features=3)

        # ── OI buffer: [value, change] ────────────────────────────────
        self.oi_buffer = RingBuffer(self.max_ticks, num_features=2)

        # Raw accumulators
        self.current_trades  = 0
        self.current_volume  = 0.0
        self.current_delta   = 0.0
        self.price_diff      = 0.0
        self.last_price      = 0.0
        self.last_tick_time  = time.time()

        # Filtered accumulators
        self.current_filtered_trades = 0
        self.current_filtered_volume = 0.0
        self.current_filtered_delta  = 0.0

        # Whale accumulators
        self.current_whale_trades = 0
        self.current_whale_volume = 0.0
        self.current_whale_delta  = 0.0

        # Flow accumulators [small_usd, medium_usd, large_usd, block_usd]
        self.current_flow_usd = [0.0, 0.0, 0.0, 0.0]

        # Liquidation accumulators
        self.current_liq_count       = 0
        self.current_short_liq_usd   = 0.0
        self.current_long_liq_usd    = 0.0

        # OI state
        self.current_oi       = 0.0
        self.current_oi_change = 0.0

        # Filters
        self._notional_filter   = NotionalFilter(symbol)
        self._whale_classifier  = WhaleClassifier(symbol)
        self._percentile_filter = PercentileFilter()

    @property
    def whale_large_threshold(self) -> float:
        """Ambang LARGE efektif (adaptif per-symbol bila sudah warm)."""
        return self._whale_classifier.large

    @property
    def whale_adaptive(self) -> bool:
        return self._whale_classifier.is_adaptive

    # ── Trade ingestion ───────────────────────────────────────────────

    def add_trade(self, price: float, volume: float, is_buyer_maker: bool):
        notional = price * volume
        delta    = -volume if is_buyer_maker else volume

        # Raw layer — always
        self.current_trades += 1
        self.current_volume += volume
        self.current_delta  += delta
        if self.last_price == 0.0:
            self.last_price = price
        else:
            self.price_diff += abs(price - self.last_price)
            self.last_price  = price

        # Tier classification for flow composition
        tier = self._whale_classifier.classify(notional)
        self.current_flow_usd[TIER_INDEX[tier]] += notional

        # Feed the notional-floor-passing trades into the percentile window
        passes_floor = self._notional_filter.passes(notional)
        if passes_floor:
            self._percentile_filter.update(notional)

        # Filtered layer: must pass both the floor and the adaptive P70
        if passes_floor and self._percentile_filter.passes(notional):
            self.current_filtered_trades += 1
            self.current_filtered_volume += volume
            self.current_filtered_delta  += delta

        # Whale layer: LARGE or BLOCK
        if tier in ("LARGE", "BLOCK"):
            self.current_whale_trades += 1
            self.current_whale_volume += volume
            self.current_whale_delta  += delta

    def add_liquidation(self, usd_value: float, side: str):
        self.current_liq_count += 1
        if side.upper() in ("SHORT", "SELL"):
            self.current_short_liq_usd += usd_value
        else:
            self.current_long_liq_usd += usd_value

    def update_oi(self, oi_value: float):
        if self.current_oi != 0.0:
            self.current_oi_change = oi_value - self.current_oi
        self.current_oi = oi_value

    # ── Tick commit ───────────────────────────────────────────────────

    def roll_tick(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Close the active 100 ms bucket, commit all layers to their ring
        buffers, and reset accumulators.  Returns (raw, liq, oi) arrays.
        """
        raw     = np.array([self.current_trades, self.current_volume,
                             self.current_delta, self.price_diff], dtype=np.float64)
        filt    = np.array([self.current_filtered_trades, self.current_filtered_volume,
                             self.current_filtered_delta], dtype=np.float64)
        whale   = np.array([self.current_whale_trades, self.current_whale_volume,
                             self.current_whale_delta], dtype=np.float64)
        flow    = np.array(self.current_flow_usd, dtype=np.float64)
        liq     = np.array([self.current_liq_count, self.current_short_liq_usd,
                             self.current_long_liq_usd], dtype=np.float64)
        oi      = np.array([self.current_oi, self.current_oi_change], dtype=np.float64)

        self.buffer.append(raw)
        self.filtered_buffer.append(filt)
        self.whale_buffer.append(whale)
        self.flow_buffer.append(flow)
        self.liq_buffer.append(liq)
        self.oi_buffer.append(oi)

        # Reset raw
        self.current_trades = 0
        self.current_volume = 0.0
        self.current_delta  = 0.0
        self.price_diff     = 0.0

        # Reset filtered
        self.current_filtered_trades = 0
        self.current_filtered_volume = 0.0
        self.current_filtered_delta  = 0.0

        # Reset whale
        self.current_whale_trades = 0
        self.current_whale_volume = 0.0
        self.current_whale_delta  = 0.0

        # Reset flow
        self.current_flow_usd = [0.0, 0.0, 0.0, 0.0]

        # Reset liq
        self.current_liq_count     = 0
        self.current_short_liq_usd = 0.0
        self.current_long_liq_usd  = 0.0
        self.current_oi_change     = 0.0

        return raw, liq, oi
