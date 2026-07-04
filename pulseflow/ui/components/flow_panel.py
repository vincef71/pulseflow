from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from pulseflow.ui.styles import COLORS

# Tier colour palette
TIER_COLORS = {
    "BLOCK":  "#00ffd2",   # accent cyan  — largest orders
    "LARGE":  "#10b981",   # emerald green
    "MEDIUM": "#f59e0b",   # amber
    "SMALL":  "#7d7d8e",   # muted grey
}

BAR_STYLE = """
QProgressBar {{
    background-color: #0f0f14;
    border: none;
    height: 8px;
    border-radius: 4px;
}}
QProgressBar::chunk {{
    background-color: {color};
    border-radius: 4px;
}}
"""


class FlowPanel(QFrame):
    """
    Displays the real-time order-flow composition and whale intelligence:

    ┌─ ORDER FLOW COMPOSITION ────────────────────────────────────────┐
    │ BLOCK   ████████████████████████   42.3%                        │
    │ LARGE   ████████████              28.1%                         │
    │ MEDIUM  ██████                    16.4%                         │
    │ SMALL   ████                      13.2%                         │
    │ ─────────────────────────────────────────────────────────────── │
    │ SIGNAL QUALITY    84.2% filtered    ● ● ● ● ● ●                │
    │ WHALE DELTA       ▲ +$1.4M  buy pressure                       │
    │ WHALE VOL%        70.4%   of 5s notional                        │
    │ P70 THRESHOLD     $4,521  (adaptive)                            │
    └─────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        title = QLabel("ORDER FLOW COMPOSITION", self)
        title.setObjectName("TitleLabel")
        layout.addWidget(title)

        # ── Tier bars ─────────────────────────────────────────────────
        self._tier_pcts:  dict = {}
        self._tier_bars:  dict = {}
        self._tier_labels: dict = {}

        tiers = [("BLOCK", "BLOCK"), ("LARGE", "LARGE"),
                 ("MEDIUM", "MEDIUM"), ("SMALL", "SMALL")]

        for key, label in tiers:
            row = QHBoxLayout()
            row.setSpacing(6)

            lbl_name = QLabel(label, self)
            lbl_name.setFixedWidth(52)
            lbl_name.setStyleSheet(
                f"color: {TIER_COLORS[key]}; font-family: 'Consolas', monospace; "
                f"font-size: 11px; font-weight: bold;"
            )

            bar = QProgressBar(self)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setStyleSheet(BAR_STYLE.format(color=TIER_COLORS[key]))

            lbl_pct = QLabel("0.0%", self)
            lbl_pct.setFixedWidth(42)
            lbl_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl_pct.setStyleSheet(
                f"color: {TIER_COLORS[key]}; font-family: 'Consolas', monospace; "
                f"font-size: 11px; font-weight: bold;"
            )

            row.addWidget(lbl_name)
            row.addWidget(bar, stretch=1)
            row.addWidget(lbl_pct)

            self._tier_bars[key]   = bar
            self._tier_labels[key] = lbl_pct

            layout.addLayout(row)

        # ── Divider ────────────────────────────────────────────────────
        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #23232a;")
        layout.addWidget(sep)

        # ── Stats rows ─────────────────────────────────────────────────
        self._lbl_quality  = self._stat_row(layout, "SIGNAL QUALITY", "—")
        self._lbl_wdelta   = self._stat_row(layout, "WHALE DELTA",    "—")
        self._lbl_wpct     = self._stat_row(layout, "WHALE VOL %",    "—")
        self._lbl_p70      = self._stat_row(layout, "P70 THRESHOLD",  "—")

        layout.addStretch()

    def _stat_row(self, layout: QVBoxLayout, name: str, init_val: str) -> QLabel:
        row = QHBoxLayout()
        row.setSpacing(4)

        lbl_name = QLabel(name, self)
        lbl_name.setFixedWidth(110)
        lbl_name.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-family: 'Consolas', monospace; "
            f"font-size: 10px; font-weight: bold; letter-spacing: 0.5px;"
        )

        lbl_val = QLabel(init_val, self)
        lbl_val.setStyleSheet(
            f"color: {COLORS['text_main']}; font-family: 'Consolas', monospace; "
            f"font-size: 11px;"
        )
        lbl_val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        row.addWidget(lbl_name)
        row.addWidget(lbl_val, stretch=1)
        layout.addLayout(row)

        return lbl_val

    # ── Public API ─────────────────────────────────────────────────────

    def update_metrics(self, metrics: dict):
        flow = metrics.get("flow_composition", {})
        if not flow:
            return

        # Tier bars
        for key, pct_key in [("BLOCK", "block_pct"), ("LARGE", "large_pct"),
                              ("MEDIUM", "medium_pct"), ("SMALL", "small_pct")]:
            pct = float(flow.get(pct_key, 0.0))
            self._tier_bars[key].setValue(int(pct))
            self._tier_labels[key].setText(f"{pct:.1f}%")

        # Signal quality (noise ratio = % of trades that passed the filter)
        noise = float(flow.get("noise_ratio", 0.0))
        self._lbl_quality.setText(f"{noise:.1f}% filtered")
        quality_color = (
            COLORS["green_glow"]   if noise > 50 else
            COLORS["orange_alert"] if noise > 20 else
            COLORS["red_glow"]
        )
        self._lbl_quality.setStyleSheet(
            f"color: {quality_color}; font-family: 'Consolas', monospace; font-size: 11px;"
        )

        # Whale delta
        wdelta = float(flow.get("whale_delta_usd_5s", 0.0))
        if abs(wdelta) >= 1_000_000:
            wdelta_str = f"${wdelta / 1_000_000:.2f}M"
        elif abs(wdelta) >= 1_000:
            wdelta_str = f"${wdelta / 1_000:.1f}K"
        else:
            wdelta_str = f"${wdelta:.0f}"
        direction = "buy" if wdelta >= 0 else "sell"
        arrow = "▲" if wdelta >= 0 else "▼"
        wdelta_color = COLORS["green_glow"] if wdelta >= 0 else COLORS["red_glow"]
        self._lbl_wdelta.setText(f"{arrow} {wdelta_str}  {direction} pressure")
        self._lbl_wdelta.setStyleSheet(
            f"color: {wdelta_color}; font-family: 'Consolas', monospace; font-size: 11px;"
        )

        # Whale volume %
        wpct = float(flow.get("whale_pct", 0.0))
        self._lbl_wpct.setText(f"{wpct:.1f}% of 5s notional")
        wpct_color = (
            COLORS["accent"]       if wpct > 50 else
            COLORS["green_glow"]   if wpct > 25 else
            COLORS["text_main"]
        )
        self._lbl_wpct.setStyleSheet(
            f"color: {wpct_color}; font-family: 'Consolas', monospace; font-size: 11px;"
        )

        # P70 adaptive threshold
        p70 = float(flow.get("p70_threshold", 0.0))
        if p70 >= 1_000:
            p70_str = f"${p70 / 1_000:.1f}K  (adaptive)"
        else:
            p70_str = f"${p70:.0f}  (warming up)" if p70 == 0 else f"${p70:.0f}  (adaptive)"
        self._lbl_p70.setText(p70_str)
