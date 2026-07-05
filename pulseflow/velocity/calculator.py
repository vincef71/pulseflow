import numpy as np
import math
from typing import Dict, Any, Tuple
from pulseflow.core.buffer import MarketTicker, RingBuffer
from pulseflow.analytics.atr import ATRCalculator
from pulseflow.config.settings import ROLLING_WINDOWS, AGGRESSION_WEIGHTS, ATR_CONFIG


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _z_score(current: float, history: np.ndarray) -> float:
    mean = np.mean(history)
    std  = max(np.std(history), 1e-8)
    return float((current - mean) / std)


class VelocityCalculator:
    """
    Computes real-time market velocity metrics across three layers:
      • Raw       — every trade, for reference and price-velocity tracking
      • Filtered  — trades above notional floor + P70 adaptive gate
      • Whale     — LARGE/BLOCK trades only

    Also exputes flow composition (% USD notional by tier) and extended
    context metrics (OI velocity, liquidation velocity, spread expansion).
    """

    def __init__(self, ticker: MarketTicker):
        self.ticker = ticker
        self.atr = ATRCalculator(
            bar_seconds=ATR_CONFIG["bar_seconds"],
            period=ATR_CONFIG["period"],
            regime_window=ATR_CONFIG["regime_window"],
        )

    # ── Raw layer helpers (unchanged from v1) ─────────────────────────

    def calculate_current_velocities(self) -> Dict[str, float]:
        latest = self.ticker.buffer.get_last_n(1)
        if len(latest) == 0:
            return {"trade_velocity": 0.0, "volume_velocity": 0.0,
                    "delta_velocity": 0.0,  "price_velocity": 0.0,
                    "notional_usd": 0.0,    "large_usd": 0.0, "block_usd": 0.0}
        t = latest[0]
        scale = 10.0

        notional_usd = large_usd = block_usd = 0.0
        flow_latest = self.ticker.flow_buffer.get_last_n(1)
        if len(flow_latest) > 0:
            f = flow_latest[0]
            notional_usd = float(f[0] + f[1] + f[2] + f[3])
            large_usd    = float(f[2])
            block_usd    = float(f[3])

        return {
            "trade_velocity":  t[0] * scale,
            "volume_velocity": t[1] * scale,
            "delta_velocity":  t[2] * scale,
            "price_velocity":  t[3] * scale,
            "notional_usd":   notional_usd,
            "large_usd":      large_usd,
            "block_usd":      block_usd,
        }

    def get_rolling_velocity_stats(self, window_sec: int) -> Tuple[np.ndarray, np.ndarray]:
        num_ticks = window_sec * 10
        ticks = self.ticker.buffer.get_last_n(num_ticks)
        if len(ticks) == 0:
            return np.zeros(4), np.ones(4)
        inst  = ticks * 10.0
        means = np.mean(inst, axis=0)
        stds  = np.std(inst, axis=0)
        stds[stds == 0.0] = 1e-8
        return means, stds

    # ── Layer Z-score helper ──────────────────────────────────────────

    def _layer_z_scores(self, ring: RingBuffer, window_sec: int) -> Dict[str, float]:
        """
        Compute Z-scores for a 3-feature ring buffer [trade, volume, delta].
        Returns {trade_velocity_z, volume_velocity_z, delta_velocity_z}.
        """
        num_ticks = window_sec * 10
        ticks = ring.get_last_n(num_ticks)
        if len(ticks) < 5:
            return {"trade_velocity_z": 0.0, "volume_velocity_z": 0.0, "delta_velocity_z": 0.0}
        inst  = ticks * 10.0
        latest = inst[-1]
        means = np.mean(inst, axis=0)
        stds  = np.std(inst, axis=0)
        stds[stds == 0.0] = 1e-8
        z = (latest - means) / stds
        return {
            "trade_velocity_z":  float(z[0]),
            "volume_velocity_z": float(z[1]),
            "delta_velocity_z":  float(z[2]),
        }

    # ── Extended context (OI / Liq / Spread) ─────────────────────────

    def _compute_oi_velocity_z(self) -> float:
        ticks = self.ticker.oi_buffer.get_last_n(3000)
        if len(ticks) < 10:
            return 0.0
        oi_changes = ticks[:, 1] * 10.0
        return _z_score(oi_changes[-1], oi_changes)

    def _compute_liq_velocity_z(self) -> float:
        ticks = self.ticker.liq_buffer.get_last_n(3000)
        if len(ticks) < 10:
            return 0.0
        total_liqs = (ticks[:, 1] + ticks[:, 2]) * 10.0
        return _z_score(total_liqs[-1], total_liqs)

    def _compute_extended_metrics(self) -> Dict[str, float]:
        ticks_5s     = self.ticker.buffer.get_last_n(50)
        liq_ticks_5s = self.ticker.liq_buffer.get_last_n(50)
        oi_ticks_5s  = self.ticker.oi_buffer.get_last_n(50)

        # OI pct change over 5 s
        oi_pct = 0.0
        if len(oi_ticks_5s) >= 2:
            start_oi = oi_ticks_5s[0, 0]
            end_oi   = oi_ticks_5s[-1, 0]
            if start_oi > 1e-8:
                oi_pct = ((end_oi - start_oi) / start_oi) * 100.0

        short_liq_5s = float(np.sum(liq_ticks_5s[:, 1])) if len(liq_ticks_5s) > 0 else 0.0
        long_liq_5s  = float(np.sum(liq_ticks_5s[:, 2])) if len(liq_ticks_5s) > 0 else 0.0

        delta_5s = float(np.sum(ticks_5s[:, 2])) if len(ticks_5s) > 0 else 0.0

        total_vol_5s   = float(np.sum(ticks_5s[:, 1])) if len(ticks_5s) > 0 else 0.0
        total_pdiff_5s = max(float(np.sum(ticks_5s[:, 3])), 1e-8) if len(ticks_5s) > 0 else 1e-8
        absorption_eff = total_vol_5s / total_pdiff_5s

        means_5s, _ = self.get_rolling_velocity_stats(5)
        means_5m, _ = self.get_rolling_velocity_stats(300)
        spread_exp   = float(means_5s[3]) / max(float(means_5m[3]), 1e-8)

        return {
            "oi_velocity_z":       self._compute_oi_velocity_z(),
            "liq_velocity_z":      self._compute_liq_velocity_z(),
            "short_liq_usd_5s":    short_liq_5s,
            "long_liq_usd_5s":     long_liq_5s,
            "oi_pct_change_5s":    float(oi_pct),
            "delta_5s":            delta_5s,
            "absorption_efficiency": absorption_eff,
            "spread_expansion":    spread_exp,
        }

    # ── Flow composition ──────────────────────────────────────────────

    def _compute_flow_composition(self) -> Dict[str, float]:
        """
        Summarise the last 5 s of the flow_buffer into % USD notional by tier.
        Also computes noise ratio (filtered / raw trades) and whale delta.
        """
        flow_5s = self.ticker.flow_buffer.get_last_n(50)
        raw_5s  = self.ticker.buffer.get_last_n(50)
        filt_5s = self.ticker.filtered_buffer.get_last_n(50)
        whale_5s = self.ticker.whale_buffer.get_last_n(50)

        if len(flow_5s) == 0:
            return {
                "small_pct": 0.0, "medium_pct": 0.0,
                "large_pct": 0.0, "block_pct":  0.0,
                "whale_pct": 0.0, "noise_ratio": 0.0,
                "p70_threshold": self.ticker._percentile_filter.threshold,
                "whale_delta_usd_5s": 0.0,
                "whale_volume_5s":    0.0,
                "whale_thr_large":  self.ticker.whale_large_threshold,
                "whale_adaptive":   self.ticker.whale_adaptive,
                "small_usd_5s":   0.0, "medium_usd_5s": 0.0,
                "large_usd_5s":   0.0, "block_usd_5s":  0.0,
            }

        sums = np.sum(flow_5s, axis=0)          # [small_usd, medium_usd, large_usd, block_usd]
        total_usd = max(float(np.sum(sums)), 1e-8)

        small_pct  = float(sums[0]) / total_usd * 100.0
        medium_pct = float(sums[1]) / total_usd * 100.0
        large_pct  = float(sums[2]) / total_usd * 100.0
        block_pct  = float(sums[3]) / total_usd * 100.0

        # Noise ratio: filtered trade count / raw trade count
        raw_count  = float(np.sum(raw_5s[:, 0]))  if len(raw_5s)  > 0 else 0.0
        filt_count = float(np.sum(filt_5s[:, 0])) if len(filt_5s) > 0 else 0.0
        noise_ratio = (filt_count / max(raw_count, 1e-8)) * 100.0

        # Whale delta (approximate: whale_volume * avg last price, direction from delta)
        last_price = self.ticker.last_price
        whale_delta_base = float(np.sum(whale_5s[:, 2])) if len(whale_5s) > 0 else 0.0
        whale_vol_base   = float(np.sum(whale_5s[:, 1])) if len(whale_5s) > 0 else 0.0
        whale_delta_usd  = whale_delta_base * max(last_price, 1.0)
        whale_volume_usd = whale_vol_base   * max(last_price, 1.0)

        return {
            "small_pct":  small_pct,
            "medium_pct": medium_pct,
            "large_pct":  large_pct,
            "block_pct":  block_pct,
            "whale_pct":  large_pct + block_pct,
            "noise_ratio":       noise_ratio,
            "p70_threshold":     self.ticker._percentile_filter.threshold,
            "whale_delta_usd_5s": whale_delta_usd,
            "whale_volume_5s":    whale_volume_usd,
            "whale_thr_large":   self.ticker.whale_large_threshold,
            "whale_adaptive":    self.ticker.whale_adaptive,
            "small_usd_5s":  float(sums[0]),
            "medium_usd_5s": float(sums[1]),
            "large_usd_5s":  float(sums[2]),
            "block_usd_5s":  float(sums[3]),
        }

    # ── Main entry point ──────────────────────────────────────────────

    def compute_metrics(self) -> Dict[str, Any]:
        curr = self.calculate_current_velocities()
        curr_vec = np.array([curr["trade_velocity"], curr["volume_velocity"],
                              curr["delta_velocity"],  curr["price_velocity"]])

        results: Dict[str, Any] = {
            "instantaneous":    curr,
            "z_scores":         {},
            "filtered_z_scores": {},
            "whale_z_scores":   {},
            "relative_velocity": {},
            "extended":         {},
            "flow_composition": {},
            "aggression_score": 30.0,
            "regime":           "normal",
        }

        # ── Raw Z-scores ──────────────────────────────────────────────
        z_dict: Dict[str, Dict[str, float]] = {}
        for name, sec in ROLLING_WINDOWS.items():
            means, stds = self.get_rolling_velocity_stats(sec)
            z = (curr_vec - means) / stds
            z_dict[name] = {
                "trade_velocity_z":  float(z[0]),
                "volume_velocity_z": float(z[1]),
                "delta_velocity_z":  float(z[2]),
                "price_velocity_z":  float(z[3]),
            }
        results["z_scores"] = z_dict

        # ── Filtered Z-scores ─────────────────────────────────────────
        filt_z: Dict[str, Dict[str, float]] = {}
        for name, sec in ROLLING_WINDOWS.items():
            filt_z[name] = self._layer_z_scores(self.ticker.filtered_buffer, sec)
        results["filtered_z_scores"] = filt_z

        # ── Whale Z-scores ────────────────────────────────────────────
        whale_z: Dict[str, Dict[str, float]] = {}
        for name, sec in ROLLING_WINDOWS.items():
            whale_z[name] = self._layer_z_scores(self.ticker.whale_buffer, sec)
        results["whale_z_scores"] = whale_z

        # ── Relative velocities ───────────────────────────────────────
        means_5s,  _ = self.get_rolling_velocity_stats(5)
        means_30s, _ = self.get_rolling_velocity_stats(30)
        means_1m,  _ = self.get_rolling_velocity_stats(60)
        means_5m,  _ = self.get_rolling_velocity_stats(300)

        def safe_div(a, b):
            return float(a / b) if b > 1e-8 else 1.0

        results["relative_velocity"] = {
            "5s_vs_5m":       safe_div(means_5s[1],  means_5m[1]),
            "1m_vs_5m":       safe_div(means_1m[1],  means_5m[1]),
            "5s_vs_30s_price": safe_div(means_5s[3], means_30s[3]),
            "5s_vs_5m_price":  safe_div(means_5s[3], means_5m[3]),
        }

        # ── Extended context ──────────────────────────────────────────
        results["extended"] = self._compute_extended_metrics()

        # ── Flow composition ──────────────────────────────────────────
        results["flow_composition"] = self._compute_flow_composition()

        # ── ATR volatility ────────────────────────────────────────────
        # Fold the latest price into the rolling OHLC bars, then snapshot.
        self.atr.update(self.ticker.last_price)
        results["atr"] = self.atr.snapshot()

        # ── Aggression Score (0-100) ──────────────────────────────────
        # Use filtered Z-scores for volume/delta (cleaner), raw for price/trade
        filt_30s = filt_z.get("30s", {})
        z_30s    = z_dict.get("30s", {})
        oi_z     = results["extended"].get("oi_velocity_z", 0.0)

        vol_z_used   = filt_30s.get("volume_velocity_z", z_30s.get("volume_velocity_z", 0.0))
        delta_z_used = filt_30s.get("delta_velocity_z",  z_30s.get("delta_velocity_z",  0.0))

        unified_z = (
            AGGRESSION_WEIGHTS["volume_velocity"] * abs(vol_z_used) +
            AGGRESSION_WEIGHTS["delta_velocity"]  * abs(delta_z_used) +
            AGGRESSION_WEIGHTS["trade_velocity"]  * abs(z_30s.get("trade_velocity_z",  0.0)) +
            AGGRESSION_WEIGHTS["price_velocity"]  * abs(z_30s.get("price_velocity_z",  0.0)) +
            AGGRESSION_WEIGHTS["oi_velocity"]     * abs(oi_z)
        )

        raw_score = normal_cdf((unified_z - 1.0) / 0.8) * 100
        agg_score = float(np.clip(raw_score, 0.0, 100.0))
        results["aggression_score"] = agg_score

        if agg_score < 20.0:
            regime = "dead"
        elif agg_score < 40.0:
            regime = "normal"
        elif agg_score < 60.0:
            regime = "active"
        elif agg_score < 80.0:
            regime = "aggressive"
        else:
            regime = "extreme"

        results["regime"] = regime
        return results
