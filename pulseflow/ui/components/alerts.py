import time
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QListWidget, QListWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from pulseflow.ui.styles import COLORS

# ── Visual constants ──────────────────────────────────────────────────────────

SIGNAL_ICONS = {
    "SHORT_SQUEEZE":           "🚀",
    "LONG_LIQUIDATION_CASCADE": "💥",
    "AGGRESSIVE_BUYING":       "📈",
    "AGGRESSIVE_SELLING":      "📉",
    "ABSORPTION":              "🛡",
    "EXHAUSTION_WARNING":      "⚠",
    "VELOCITY_SPIKE":          "⚡",
}

PRIORITY_COLORS = {
    "CRITICAL": "#FF4444",
    "HIGH":     "#FF8C00",
    "WARNING":  "#FFD700",
    "INFO":     "#00CFFF",
}

DIRECTION_ARROWS = {
    "BULLISH": "▲",
    "BEARISH": "▼",
    "NEUTRAL": "◆",
}

CONFIDENCE_BARS = {
    "EXTREME": "████████",
    "HIGH":    "██████░░",
    "MEDIUM":  "████░░░░",
    "LOW":     "██░░░░░░",
}


def _fmt_z(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}σ"


def _fmt_usd(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


class AlertPanel(QFrame):
    """
    Smart Market Intelligence console — shows rich structured alerts
    with multi-dimensional velocity, context, and interpretation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        self.title = QLabel("SMART MARKET INTELLIGENCE", self)
        self.title.setObjectName("TitleLabel")
        layout.addWidget(self.title)

        self.log_widget = QListWidget(self)
        self.log_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 16px;
            }}
            QListWidget::item {{
                padding: 3px 6px;
                border-bottom: 1px solid #1a1a24;
            }}
            QListWidget::item:hover {{
                background-color: {COLORS['bg_hover']};
            }}
        """)
        layout.addWidget(self.log_widget)

    def add_signal(self, symbol: str, signal: dict):
        """Render a rich multi-line intelligence alert."""
        ts = time.strftime("%H:%M:%S")
        sig_type   = signal.get("type", "UNKNOWN")
        label      = signal.get("label", sig_type)
        priority   = signal.get("priority", "INFO")
        direction  = signal.get("direction", "NEUTRAL")
        confidence = signal.get("confidence", "LOW")
        regime_lbl = signal.get("regime_label", signal.get("regime", ""))
        state      = signal.get("state", "")
        agg_score  = signal.get("agg_score", 0.0)

        # Velocities
        vol_z   = signal.get("volume_velocity_z", 0.0)
        delta_z = signal.get("delta_velocity_z", 0.0)
        trade_z = signal.get("trade_velocity_z", 0.0)
        price_z = signal.get("price_velocity_z", 0.0)
        oi_z    = signal.get("oi_velocity_z", 0.0)
        liq_z   = signal.get("liq_velocity_z", 0.0)

        # Context
        oi_pct     = signal.get("oi_pct_change", 0.0)
        short_liq  = signal.get("short_liq_usd", 0.0)
        long_liq   = signal.get("long_liq_usd", 0.0)
        spread_exp = signal.get("spread_expansion", 1.0)

        # Whale / flow intelligence
        whale_vol_z    = signal.get("whale_vol_z", 0.0)
        whale_delta_z  = signal.get("whale_delta_z", 0.0)
        whale_delta_usd = signal.get("whale_delta_usd", 0.0)
        whale_pct      = signal.get("whale_pct", 0.0)
        noise_ratio    = signal.get("noise_ratio", 100.0)
        p70_threshold  = signal.get("p70_threshold", 0.0)

        # Interpretation
        interp_lines = signal.get("interpretation", [])
        interp_text  = " ".join(interp_lines) if interp_lines else signal.get("message", "")

        icon    = SIGNAL_ICONS.get(sig_type, "🔔")
        arrow   = DIRECTION_ARROWS.get(direction, "◆")
        conf_bar = CONFIDENCE_BARS.get(confidence, "░░░░░░░░")

        # Liquidation display
        liq_parts = []
        if short_liq > 1000:
            liq_parts.append(f"Shorts {_fmt_usd(short_liq)}")
        if long_liq > 1000:
            liq_parts.append(f"Longs {_fmt_usd(long_liq)}")
        liq_str = " | ".join(liq_parts) if liq_parts else "—"

        sep = "─" * 52

        # Whale delta display
        wdelta_arrow = "▲" if whale_delta_usd >= 0 else "▼"
        wdelta_str   = _fmt_usd(abs(whale_delta_usd)) if abs(whale_delta_usd) >= 100 else "—"
        wdelta_side  = "buy" if whale_delta_usd >= 0 else "sell"

        # P70 display
        p70_str = (f"${p70_threshold / 1_000:.1f}K" if p70_threshold >= 1_000
                   else (f"${p70_threshold:.0f}" if p70_threshold > 0 else "warming"))

        lines = [
            f"[{ts}] {symbol:<8}  {icon} {label}  {arrow}",
            f"  {sep}",
            f"  Vol:{_fmt_z(vol_z)}  Delta:{_fmt_z(delta_z)}  Trade:{_fmt_z(trade_z)}",
            f"  Price:{_fmt_z(price_z)}  OI:{_fmt_z(oi_z)}  Liq:{_fmt_z(liq_z)}",
            f"  OI Change: {oi_pct:+.2f}%   Liquidations: {liq_str}",
            f"  Spread Exp: {spread_exp:.2f}x",
            f"  {sep}",
            f"  Whale Vol:{_fmt_z(whale_vol_z)}  Whale Delta:{_fmt_z(whale_delta_z)}",
            f"  Whale Flow: {whale_pct:.1f}%   Whale Delta: {wdelta_arrow} {wdelta_str} {wdelta_side}",
            f"  Signal Quality: {noise_ratio:.1f}% filtered   P70: {p70_str}",
            f"  {sep}",
            f"  Regime: {regime_lbl}   State: {state}   Score: {agg_score:.0f}/100",
            f"  Confidence: {confidence} {conf_bar}",
            f"  {sep}",
            f"  ▸ {interp_text}",
            "",
        ]

        full_text = "\n".join(lines)

        item = QListWidgetItem(full_text)
        color = QColor(PRIORITY_COLORS.get(priority, "#00CFFF"))
        item.setForeground(color)

        if priority == "CRITICAL":
            f = item.font()
            f.setBold(True)
            item.setFont(f)

        self.log_widget.insertItem(0, item)

        # Keep max 80 alerts to preserve render performance
        while self.log_widget.count() > 80:
            self.log_widget.takeItem(self.log_widget.count() - 1)
