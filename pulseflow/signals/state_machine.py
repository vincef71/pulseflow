from enum import Enum
from typing import Dict, Any


class SymbolState(Enum):
    IDLE = "IDLE"
    BUILDUP = "BUILDUP"
    BREAKOUT = "BREAKOUT"
    EXPANSION = "EXPANSION"
    EXHAUSTION = "EXHAUSTION"
    REVERSAL = "REVERSAL"


# Human-readable description for each state shown in alerts
STATE_DESCRIPTIONS = {
    SymbolState.IDLE: "Low participation. Avoid breakout trades.",
    SymbolState.BUILDUP: "Momentum building. Watch for trigger.",
    SymbolState.BREAKOUT: "High velocity spike detected. Active impulse.",
    SymbolState.EXPANSION: "Price accepted. OI expanding. Continuation likely.",
    SymbolState.EXHAUSTION: "Momentum fading. Potential reversal approaching.",
    SymbolState.REVERSAL: "Opposite directional signals detected.",
}


class SymbolStateMachine:
    """
    Tracks the current market microstructure phase for a single symbol.

    Transitions:
        IDLE      → BUILDUP    : aggression rising above 40
        BUILDUP   → BREAKOUT   : vol_z > 2.5 and aggression > 60
        BUILDUP   → IDLE       : aggression drops below 20
        BREAKOUT  → EXPANSION  : OI rising after breakout
        BREAKOUT  → EXHAUSTION : velocity fades quickly
        EXPANSION → EXHAUSTION : aggression drops to < 50% of peak
        EXPANSION → REVERSAL   : strong opposing delta
        EXHAUSTION→ REVERSAL   : fresh velocity spike in opposite direction
        EXHAUSTION→ IDLE       : aggression fully collapses below 20
        REVERSAL  → BREAKOUT   : new velocity spike
        REVERSAL  → IDLE       : aggression collapses
    """

    def __init__(self):
        self.state = SymbolState.IDLE
        self.state_ticks = 0          # ticks spent in current state
        self.peak_agg_score = 0.0
        self.last_delta_z = 0.0

    def update(self, metrics: Dict[str, Any]) -> SymbolState:
        agg = metrics.get("aggression_score", 0.0)
        z_5s = metrics.get("z_scores", {}).get("5s", {})
        vol_z = z_5s.get("volume_velocity_z", 0.0)
        delta_z = z_5s.get("delta_velocity_z", 0.0)
        ext = metrics.get("extended", {})
        oi_pct = ext.get("oi_pct_change_5s", 0.0)

        prev = self.state
        self.state_ticks += 1

        if self.state == SymbolState.IDLE:
            if agg > 40.0:
                self.state = SymbolState.BUILDUP
                self.peak_agg_score = agg

        elif self.state == SymbolState.BUILDUP:
            if agg > self.peak_agg_score:
                self.peak_agg_score = agg
            if vol_z > 2.5 and agg > 60.0:
                self.state = SymbolState.BREAKOUT
            elif agg < 20.0:
                self.state = SymbolState.IDLE
                self.peak_agg_score = 0.0

        elif self.state == SymbolState.BREAKOUT:
            if agg > self.peak_agg_score:
                self.peak_agg_score = agg
            # Price accepted + OI growing → expansion
            if oi_pct > 0.4 and agg > 55.0:
                self.state = SymbolState.EXPANSION
            # Velocity faded → exhaustion
            elif self.state_ticks > 30 and agg < 45.0:
                self.state = SymbolState.EXHAUSTION

        elif self.state == SymbolState.EXPANSION:
            if agg > self.peak_agg_score:
                self.peak_agg_score = agg
            # Reversal: strong opposing delta surge
            if abs(delta_z) > 2.5 and (delta_z * self.last_delta_z < 0):
                self.state = SymbolState.REVERSAL
            # Exhaustion: aggression dropped to < 50% of peak
            elif agg < self.peak_agg_score * 0.5 and agg < 40.0:
                self.state = SymbolState.EXHAUSTION

        elif self.state == SymbolState.EXHAUSTION:
            if agg < 20.0:
                self.state = SymbolState.IDLE
                self.peak_agg_score = 0.0
            elif vol_z > 2.0 and abs(delta_z) > 1.5:
                self.state = SymbolState.REVERSAL

        elif self.state == SymbolState.REVERSAL:
            if agg < 20.0:
                self.state = SymbolState.IDLE
                self.peak_agg_score = 0.0
            elif vol_z > 2.5 and agg > 60.0:
                self.state = SymbolState.BREAKOUT
                self.peak_agg_score = agg

        if self.state != prev:
            self.state_ticks = 0

        self.last_delta_z = delta_z
        return self.state

    @property
    def description(self) -> str:
        return STATE_DESCRIPTIONS.get(self.state, "")
