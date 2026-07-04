import time
from collections import deque
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QListWidget, QListWidgetItem
from PyQt6.QtGui import QColor
from pulseflow.ui.styles import COLORS


# Battle state → curated headline event (icon, template, colour)
_STATE_EVENTS = {
    "ASSAULT":      ("⚡", "{side} SURGE",         "dir"),
    "ABSORPTION":   ("🛡", "ABSORPTION DETECTED",  "#3b82f6"),
    "BREAKTHROUGH": ("🚀", "{side} BREAKOUT",      "dir"),
    "EXHAUSTION":   ("💤", "MOMENTUM EXHAUSTED",   "#7d7d8e"),
}

_GREEN = COLORS["green_glow"]
_RED   = COLORS["red_glow"]


def _fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


class EventFeed(QFrame):
    """
    Curated 'what just happened' stream — replaces the 500-row liquidation tape
    with only the events a trader reacts to:

        ⚡ BUY SURGE        🐋 WHALE SELL
        ☠ SHORT LIQUIDATION 🛡 ABSORPTION DETECTED
        🚀 BUY BREAKOUT

    Driven by the Battle State Engine output (`battle`), which already gates
    importance (whale > tier threshold, liquidation > cascade threshold,
    battlefield state transitions). De-duplicated so the 10 Hz tick doesn't
    spam the log.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._last_state = None
        self._logged_ts = deque(maxlen=60)   # battle-event ts already shown
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.title = QLabel("LIVE EVENTS", self)
        self.title.setObjectName("TitleLabel")
        layout.addWidget(self.title)

        self.list_widget = QListWidget(self)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 15px;
            }}
            QListWidget::item {{
                padding: 6px 6px;
                border-bottom: 1px solid #1a1a24;
            }}
        """)
        layout.addWidget(self.list_widget)

    # ── Public API ────────────────────────────────────────────────────────

    def reset(self):
        """Called on symbol switch — the feed follows the focused symbol."""
        self._last_state = None
        self._logged_ts.clear()
        self.list_widget.clear()

    def push_alert(self, symbol: str, level: float, direction: str):
        """Surface a triggered price alert in the live event stream."""
        arrow = "▲" if direction == "up" else "▼"
        color = _GREEN if direction == "up" else _RED
        self._add_row(symbol, "🔔", f"ALERT {level:.6g} {arrow}", "", color)

    def consume(self, symbol: str, battle: dict | None):
        if not battle:
            return
        dominant = battle.get("dominant", "NEUTRAL")

        # ── Battlefield state transitions → headline events ──────────────
        state = battle.get("state")
        if state != self._last_state:
            self._last_state = state
            tpl = _STATE_EVENTS.get(state)
            if tpl:
                icon, template, col = tpl
                side = "BUY" if dominant == "BUYERS" else "SELL" if dominant == "SELLERS" else ""
                # Surge/breakout only meaningful with a clear aggressor
                if "{side}" in template and not side:
                    pass
                else:
                    label = template.format(side=side).strip()
                    if col == "dir":
                        col = _GREEN if dominant == "BUYERS" else _RED if dominant == "SELLERS" else "#7d7d8e"
                    self._add_row(symbol, icon, label, "", col)

        # ── Discrete battle events (whale / liquidation) ─────────────────
        for ev in battle.get("events", []):
            ts = ev.get("ts", 0.0)
            if ts in self._logged_ts:
                continue
            self._logged_ts.append(ts)

            kind = ev.get("kind")
            side = ev.get("side", "")
            usd  = ev.get("usd", 0.0)
            mag  = _fmt_usd(usd) if usd else ""

            if kind == "WHALE":
                icon = "🐋"
                label = f"WHALE {side}"
                col = _GREEN if side == "BUY" else _RED
            elif kind == "LIQ":
                icon = "☠"
                label = f"{side} LIQUIDATION"
                # Short liq = shorts forced to buy (bullish); long liq = bearish
                col = _RED if side == "LONG" else _GREEN
            else:
                icon, label, col = "•", str(kind), COLORS["text_muted"]

            self._add_row(symbol, icon, label, mag, col)

    # ── Rendering ─────────────────────────────────────────────────────────

    def _add_row(self, symbol: str, icon: str, label: str, mag: str, color: str):
        ts = time.strftime("%H:%M:%S")
        mag_part = f"  {mag}" if mag else ""
        text = f"{icon}  {label:<20} {symbol:<7}{mag_part}   {ts}"
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        self.list_widget.insertItem(0, item)
        while self.list_widget.count() > 60:
            self.list_widget.takeItem(self.list_widget.count() - 1)
