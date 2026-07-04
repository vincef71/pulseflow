from typing import Dict, Any, Optional
from pulseflow.config.settings import INTERPRETER_THRESHOLDS as TH

# Priority rank for comparison (higher = more important)
PRIORITY_RANK = {"INFO": 0, "WARNING": 1, "HIGH": 2, "CRITICAL": 3}

SIGNAL_META = {
    "SHORT_SQUEEZE": {
        "label": "SHORT SQUEEZE",
        "direction": "BULLISH",
        "priority": "CRITICAL",
        "interpretation": [
            "Short positions forced to close.",
            "Explosive move possible.",
            "Watch for exhaustion near resistance.",
        ],
    },
    "LONG_LIQUIDATION_CASCADE": {
        "label": "LONG LIQUIDATION CASCADE",
        "direction": "BEARISH",
        "priority": "CRITICAL",
        "interpretation": [
            "Forced long selling detected.",
            "Panic liquidation in progress.",
            "Potential acceleration lower.",
        ],
    },
    "AGGRESSIVE_BUYING": {
        "label": "AGGRESSIVE BUYING DETECTED",
        "direction": "BULLISH",
        "priority": "HIGH",
        "interpretation": [
            "Aggressive buyers entering with strong participation.",
            "New long positioning detected.",
            "Potential breakout continuation.",
        ],
    },
    "AGGRESSIVE_SELLING": {
        "label": "AGGRESSIVE SELLING DETECTED",
        "direction": "BEARISH",
        "priority": "HIGH",
        "interpretation": [
            "Aggressive sellers entering with strong participation.",
            "New short positioning detected.",
            "Potential breakdown continuation.",
        ],
    },
    "ABSORPTION": {
        "label": "ABSORPTION DETECTED",
        "direction": "NEUTRAL",
        "priority": "WARNING",
        "interpretation": [
            "Large passive liquidity absorbing aggressive orders.",
            "Potential reversal zone ahead.",
            "Watch for direction resolution.",
        ],
    },
    "EXHAUSTION_WARNING": {
        "label": "EXHAUSTION WARNING",
        "direction": "NEUTRAL",
        "priority": "WARNING",
        "interpretation": [
            "Momentum weakening despite aggressive activity.",
            "Potential reversal risk increasing.",
            "Consider reducing position size.",
        ],
    },
    "VELOCITY_SPIKE": {
        "label": "VELOCITY SPIKE",
        "direction": "NEUTRAL",
        "priority": "INFO",
        "interpretation": [
            "Significant volume surge detected.",
            "Monitor for follow-through and direction.",
        ],
    },
}


def _confidence(score: float) -> str:
    if score < 3.0:
        return "LOW"
    if score < 6.0:
        return "MEDIUM"
    if score < 10.0:
        return "HIGH"
    return "EXTREME"


class MarketInterpreter:
    """
    Interprets multi-dimensional velocity + context metrics into an
    actionable market intelligence signal with direction, confidence,
    and human-readable interpretation lines.
    """

    def interpret(self, metrics: Dict[str, Any], state: str = "IDLE") -> Dict[str, Any]:
        # Filtered 5m Z-scores (institutional-grade signal — ignores bot noise).
        # Fall back to raw 5m Z-scores when the filtered layer hasn't warmed up.
        filt_5m = metrics.get("filtered_z_scores", {}).get("5m", {})
        raw_5m  = metrics.get("z_scores", {}).get("5m", {})
        ext = metrics.get("extended", {})

        # vol/delta: prefer filtered layer — cleaner institutional signal
        # trade/price: always raw — not affected by notional filtering
        vol_z   = filt_5m.get("volume_velocity_z", 0.0) or raw_5m.get("volume_velocity_z", 0.0)
        delta_z = filt_5m.get("delta_velocity_z",  0.0) or raw_5m.get("delta_velocity_z",  0.0)
        trade_z = raw_5m.get("trade_velocity_z",  0.0)
        price_z = raw_5m.get("price_velocity_z",  0.0)

        oi_pct = ext.get("oi_pct_change_5s", 0.0)
        short_liq = ext.get("short_liq_usd_5s", 0.0)
        long_liq = ext.get("long_liq_usd_5s", 0.0)
        delta = ext.get("delta_5s", 0.0)
        absorption_eff = ext.get("absorption_efficiency", 0.0)
        spread_exp = ext.get("spread_expansion", 1.0)

        signal_type: Optional[str] = None
        confidence_score = 0.0

        # ── 1. SHORT SQUEEZE ──────────────────────────────────────────────
        # price up + vol spike + OI dropping + short liquidations present
        if (
            price_z > TH["squeeze_price_z"]
            and vol_z > TH["squeeze_vol_z"]
            and oi_pct < TH["squeeze_oi_pct"]
            and short_liq > 0
        ):
            signal_type = "SHORT_SQUEEZE"
            confidence_score = abs(price_z) + vol_z + abs(oi_pct) * 3 + (short_liq / 50000)

        # ── 2. LONG LIQUIDATION CASCADE ──────────────────────────────────
        # price moving (velocity up) + delta very negative + OI dropping + long liqs
        elif (
            price_z > TH["squeeze_price_z"]
            and delta_z < -TH["squeeze_vol_z"]
            and oi_pct < TH["squeeze_oi_pct"]
            and long_liq > 0
        ):
            signal_type = "LONG_LIQUIDATION_CASCADE"
            confidence_score = price_z + abs(delta_z) + abs(oi_pct) * 3 + (long_liq / 50000)

        # ── 3. AGGRESSIVE BUYING ─────────────────────────────────────────
        # vol up + positive delta + price up. OI rising adds confidence but is not required
        # (position flips can show aggressive buying without net OI change).
        elif (
            vol_z > TH["aggressive_vol_z"]
            and delta_z > TH["aggressive_delta_z"]
            and price_z > TH["aggressive_price_z"]
        ):
            signal_type = "AGGRESSIVE_BUYING"
            oi_bonus = max(oi_pct, 0.0) * 0.5
            confidence_score = vol_z + delta_z + price_z + oi_bonus

        # ── 4. AGGRESSIVE SELLING ────────────────────────────────────────
        elif (
            vol_z > TH["aggressive_vol_z"]
            and delta_z < -TH["aggressive_delta_z"]
            and price_z > TH["aggressive_price_z"]
        ):
            signal_type = "AGGRESSIVE_SELLING"
            oi_bonus = max(oi_pct, 0.0) * 0.5
            confidence_score = vol_z + abs(delta_z) + price_z + oi_bonus

        # ── 5. ABSORPTION ────────────────────────────────────────────────
        # vol spike + trade spike + minimal price movement → hidden liquidity
        elif (
            vol_z > TH["absorption_vol_z"]
            and price_z < TH["absorption_price_z_max"]
            and absorption_eff > 0
        ):
            signal_type = "ABSORPTION"
            confidence_score = vol_z * 1.5

        # ── 6. EXHAUSTION ────────────────────────────────────────────────
        # extreme vol but weak price follow-through + delta divergence
        elif (
            vol_z > TH["exhaustion_vol_z"]
            and price_z < TH["exhaustion_price_z_max"]
        ):
            signal_type = "EXHAUSTION_WARNING"
            confidence_score = vol_z

        # ── 7. GENERIC VELOCITY SPIKE ────────────────────────────────────
        elif vol_z > TH["velocity_spike_min_z"]:
            signal_type = "VELOCITY_SPIKE"
            # Override direction based on net delta
            confidence_score = vol_z

        if signal_type is None:
            return {"signal_type": "NORMAL_ACTIVITY", "priority": None}

        meta = SIGNAL_META[signal_type]

        # Override VELOCITY_SPIKE direction based on delta
        direction = meta["direction"]
        if signal_type == "VELOCITY_SPIKE":
            if delta > 0:
                direction = "BULLISH"
            elif delta < 0:
                direction = "BEARISH"

        # Enrich interpretation with live numbers for SHORT_SQUEEZE / LONG_LIQ
        interpretation = list(meta["interpretation"])
        if signal_type == "SHORT_SQUEEZE" and short_liq > 0:
            interpretation.insert(1, f"${short_liq / 1000:.0f}K short positions liquidated.")
        elif signal_type == "LONG_LIQUIDATION_CASCADE" and long_liq > 0:
            interpretation.insert(1, f"${long_liq / 1000:.0f}K long positions liquidated.")

        return {
            "signal_type": signal_type,
            "label": meta["label"],
            "direction": direction,
            "priority": meta["priority"],
            "confidence": _confidence(confidence_score),
            "interpretation": interpretation,
        }
