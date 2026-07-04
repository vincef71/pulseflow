from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import Qt
from pulseflow.ui.styles import COLORS


def _dot(color: str) -> str:
    return f"<span style='color:{color}; font-size:18px;'>●</span>"


class MarketStateCard(QFrame):
    """
    The glanceable verdict panel. Replaces a wall of raw numbers
    (whale vol, delta, flow, spread, signal quality …) with the four things
    a trader actually wants in one second:

        🟢 BUYERS IN CONTROL
        Pressure       78%
        Whale Support  YES
        Momentum       RISING
        Breakout Prob  71%

    Fed by `battle` (Battle State Engine output).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # ── Headline ─────────────────────────────────────────────────────
        self.headline = QLabel("● MARKET QUIET", self)
        self.headline.setStyleSheet(
            "font-size: 19px; font-weight: 900; color: #7d7d8e; letter-spacing: 0.5px;"
        )
        root.addWidget(self.headline)

        self.substate = QLabel("Waiting for flow…", self)
        self.substate.setStyleSheet("font-size: 11px; color: #7d7d8e;")
        root.addWidget(self.substate)

        root.addSpacing(2)

        # ── Key/value rows ───────────────────────────────────────────────
        self.rows = {}
        for key, name in (("pressure", "PRESSURE"),
                          ("whale", "WHALE SUPPORT"),
                          ("momentum", "MOMENTUM")):
            row = QHBoxLayout()
            lbl = QLabel(name, self)
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: bold; letter-spacing: 0.5px;")
            val = QLabel("—", self)
            val.setStyleSheet("font-size: 15px; font-weight: 900; color: #e3e3e7;")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(val)
            root.addLayout(row)
            self.rows[key] = val

        root.addSpacing(6)

        # ── Conviction (breakout / reversal probability) ─────────────────
        self.conv_label = QLabel("BREAKOUT PROBABILITY", self)
        self.conv_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px; font-weight: bold; letter-spacing: 0.5px;")
        root.addWidget(self.conv_label)

        conv_row = QHBoxLayout()
        conv_row.setSpacing(10)
        self.conv_bar = QProgressBar(self)
        self.conv_bar.setRange(0, 100)
        self.conv_bar.setValue(0)
        self.conv_bar.setTextVisible(False)
        self.conv_bar.setFixedHeight(14)
        self._set_bar_color("#7d7d8e")
        conv_row.addWidget(self.conv_bar, 1)

        self.conv_pct = QLabel("0%", self)
        self.conv_pct.setStyleSheet("font-size: 18px; font-weight: 900; color: #7d7d8e;")
        self.conv_pct.setFixedWidth(54)
        self.conv_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        conv_row.addWidget(self.conv_pct)
        root.addLayout(conv_row)
        root.addStretch()

    def _set_bar_color(self, color: str):
        self.conv_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #171720;
                border: 1px solid #282835;
                border-radius: 7px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 7px;
            }}
        """)

    # ── Update ───────────────────────────────────────────────────────────

    def update_state(self, battle: dict | None):
        if not battle:
            return
        narr = battle.get("narrative", {})
        color = battle.get("state_color", COLORS["text_muted"])
        dominant = battle.get("dominant", "NEUTRAL")

        # Headline: who's in control + the battlefield verb
        if dominant == "BUYERS":
            control = "BUYERS IN CONTROL"
        elif dominant == "SELLERS":
            control = "SELLERS IN CONTROL"
        else:
            control = "BALANCED"
        self.headline.setText(f"{_dot(color)}  {control}")
        self.headline.setStyleSheet(
            f"font-size: 19px; font-weight: 900; color: {color}; letter-spacing: 0.5px;"
        )
        self.substate.setText(narr.get("headline", ""))
        self.substate.setStyleSheet(f"font-size: 11px; color: {color};")

        # Rows
        pressure_pct = narr.get("pressure_pct", int(round(battle.get("aggression", 0.0))))
        self.rows["pressure"].setText(f"{pressure_pct}%")
        self.rows["pressure"].setStyleSheet(f"font-size: 15px; font-weight: 900; color: {color};")

        whale = narr.get("whale_support", "—")
        whale_col = COLORS["green_glow"] if whale == "YES" else COLORS["text_muted"]
        self.rows["whale"].setText(whale)
        self.rows["whale"].setStyleSheet(f"font-size: 15px; font-weight: 900; color: {whale_col};")

        momentum = narr.get("momentum", "—")
        mom_col = {
            "RISING": COLORS["green_glow"],
            "FADING": COLORS["red_glow"],
            "STEADY": COLORS["text_muted"],
        }.get(momentum, COLORS["text_muted"])
        self.rows["momentum"].setText(momentum)
        self.rows["momentum"].setStyleSheet(f"font-size: 15px; font-weight: 900; color: {mom_col};")

        # Conviction
        mode = narr.get("conviction_mode", "BREAKOUT")
        prob = int(narr.get("conviction_prob", 0))
        if mode == "REVERSAL":
            conv_col = COLORS["orange_alert"]
            self.conv_label.setText("REVERSAL PROBABILITY")
        else:
            conv_col = color if dominant != "NEUTRAL" else COLORS["text_muted"]
            self.conv_label.setText("BREAKOUT PROBABILITY")
        self.conv_bar.setValue(prob)
        self._set_bar_color(conv_col)
        self.conv_pct.setText(f"{prob}%")
        self.conv_pct.setStyleSheet(f"font-size: 18px; font-weight: 900; color: {conv_col};")
