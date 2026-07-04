import time
from typing import Dict, Any, List
from pulseflow.core.buffer import MarketTicker
from pulseflow.velocity.calculator import VelocityCalculator
from pulseflow.signals.interpreter import MarketInterpreter, PRIORITY_RANK
from pulseflow.signals.state_machine import SymbolStateMachine
from pulseflow.config.settings import THRESHOLDS, SIGNAL_COOLDOWNS


class SignalDetector:
    """
    Evaluates real-time market microstructure metrics to trigger
    structured intelligence alerts using the interpretation matrix
    and symbol state machine.
    """

    def __init__(self, ticker: MarketTicker, calculator: VelocityCalculator):
        self.ticker = ticker
        self.calculator = calculator
        self.interpreter = MarketInterpreter()
        self.state_machine = SymbolStateMachine()

        # Per signal-type: last fire time and last priority
        self._last_time: Dict[str, float] = {}
        self._last_priority: Dict[str, str] = {}

    def _cooldown_ok(self, signal_type: str, priority: str, now: float) -> bool:
        if signal_type not in self._last_time:
            return True

        last_pri = self._last_priority.get(signal_type, "INFO")

        # Always allow if incoming priority is strictly higher than last
        if PRIORITY_RANK.get(priority, 0) > PRIORITY_RANK.get(last_pri, 0):
            return True

        cooldown = SIGNAL_COOLDOWNS.get(priority, 15.0)
        return (now - self._last_time[signal_type]) >= cooldown

    def evaluate_signals(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals = []
        now = time.time()

        # Update symbol state machine
        current_state = self.state_machine.update(metrics)

        agg_score = metrics.get("aggression_score", 0.0)
        regime = metrics.get("regime", "normal")

        # Gate: below minimum aggression, nothing meaningful to report
        if agg_score < THRESHOLDS["min_aggression_score"]:
            return signals

        # Run interpretation matrix
        interp = self.interpreter.interpret(metrics, current_state.value)
        signal_type = interp.get("signal_type", "NORMAL_ACTIVITY")
        priority = interp.get("priority")

        if priority is None or signal_type == "NORMAL_ACTIVITY":
            return signals

        if not self._cooldown_ok(signal_type, priority, now):
            return signals

        # Display Z-scores from the 5m window (vs 5-minute baseline — most readable).
        z_5m = metrics.get("z_scores", {}).get("5m", {})
        ext = metrics.get("extended", {})

        vol_z = z_5m.get("volume_velocity_z", 0.0)
        delta_z = z_5m.get("delta_velocity_z", 0.0)
        trade_z = z_5m.get("trade_velocity_z", 0.0)
        price_z = z_5m.get("price_velocity_z", 0.0)
        oi_z = ext.get("oi_velocity_z", 0.0)
        liq_z = ext.get("liq_velocity_z", 0.0)

        oi_pct         = ext.get("oi_pct_change_5s", 0.0)
        short_liq      = ext.get("short_liq_usd_5s", 0.0)
        long_liq       = ext.get("long_liq_usd_5s", 0.0)
        spread_exp     = ext.get("spread_expansion", 1.0)
        absorption_eff = ext.get("absorption_efficiency", 0.0)
        delta_5s       = ext.get("delta_5s", 0.0)

        flow = metrics.get("flow_composition", {})
        whale_delta_usd = flow.get("whale_delta_usd_5s", 0.0)
        whale_volume    = flow.get("whale_volume_5s", 0.0)
        whale_pct       = flow.get("whale_pct", 0.0)
        noise_ratio     = flow.get("noise_ratio", 100.0)
        p70_threshold   = flow.get("p70_threshold", 0.0)

        whale_z = metrics.get("whale_z_scores", {}).get("5m", {})
        whale_vol_z   = whale_z.get("volume_velocity_z", 0.0)
        whale_delta_z = whale_z.get("delta_velocity_z", 0.0)

        signal = {
            # Core identity
            "type": signal_type,
            "label": interp.get("label", signal_type),
            "timestamp": now,
            "priority": priority,
            "direction": interp.get("direction", "NEUTRAL"),
            "confidence": interp.get("confidence", "LOW"),

            # Regime and phase
            "regime": regime.upper(),
            "regime_label": _regime_label(regime),
            "state": current_state.value,
            "state_description": self.state_machine.description,
            "agg_score": float(agg_score),

            # Multi-dimensional velocity Z-scores
            "volume_velocity_z": float(vol_z),
            "delta_velocity_z": float(delta_z),
            "trade_velocity_z": float(trade_z),
            "price_velocity_z": float(price_z),
            "oi_velocity_z": float(oi_z),
            "liq_velocity_z": float(liq_z),

            # Market context
            "oi_pct_change": float(oi_pct),
            "short_liq_usd": float(short_liq),
            "long_liq_usd": float(long_liq),
            "spread_expansion": float(spread_exp),
            "absorption_efficiency": float(absorption_eff),
            "delta_5s": float(delta_5s),

            # Whale / flow intelligence
            "whale_delta_usd":  float(whale_delta_usd),
            "whale_volume_usd": float(whale_volume),
            "whale_pct":        float(whale_pct),
            "whale_vol_z":      float(whale_vol_z),
            "whale_delta_z":    float(whale_delta_z),
            "noise_ratio":      float(noise_ratio),
            "p70_threshold":    float(p70_threshold),

            # Human interpretation
            "interpretation": interp.get("interpretation", []),
            "message": " ".join(interp.get("interpretation", [])),
        }

        signals.append(signal)
        self._last_time[signal_type] = now
        self._last_priority[signal_type] = priority

        return signals


def _regime_label(regime: str) -> str:
    labels = {
        "dead": "DEAD MARKET",
        "normal": "NORMAL ACTIVITY",
        "active": "ACTIVE MOMENTUM",
        "aggressive": "AGGRESSIVE EXPANSION",
        "extreme": "EXTREME / CLIMAX",
    }
    return labels.get(regime, regime.upper())
