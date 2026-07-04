import time
import numpy as np
import pyqtgraph as pg
from collections import deque
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal
from pulseflow.ui.styles import COLORS
from pulseflow.config.settings import ATR_CONFIG
from pulseflow.ui.components.frontline_bar import FrontlineBar
from pulseflow.ui.components.alert_line import AlertLine


class TimeAxisItem(pg.AxisItem):
    """
    Renders the x-axis as real wall-clock time instead of raw tick indices.
    Maps each tick-index value to the timestamp captured when that price point
    was recorded (`chart.time_history`), so labels read e.g. 22:15:30 and line
    up with the Live Events feed.
    """
    def __init__(self, chart, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._chart = chart

    def tickStrings(self, values, scale, spacing):
        th = self._chart.time_history
        n = len(th)
        # Sub-minute spans need seconds; longer spans read fine as HH:MM
        fmt = "%H:%M:%S" if (spacing * 0.1) < 90 else "%H:%M"
        labels = []
        for v in values:
            i = int(round(v))
            if 0 <= i < n:
                labels.append(time.strftime(fmt, time.localtime(th[i])))
            else:
                labels.append("")
        return labels


# ATR volatility regime → envelope colour (R, G, B)
ATR_REGIME_COLORS = {
    "warming":   (100, 116, 139),   # slate
    "calm":      (56, 189, 248),    # sky
    "normal":    (16, 185, 129),    # emerald
    "elevated":  (245, 158, 11),    # amber
    "explosive": (244, 63, 94),     # crimson
}

# Daily-ATR session state → readout colour (R, G, B)
DAILY_STATE_COLORS = {
    "warming":        (100, 116, 139),   # slate
    "compressed":     (56, 189, 248),    # sky — quiet, coiling
    "developing":     (16, 185, 129),    # emerald — normal progression
    "range_complete": (245, 158, 11),    # amber — full day's range spent
    "expansion":      (244, 63, 94),     # crimson — outsized / breakout day
}

# Direction → base RGB
_GREEN = (16, 185, 129)
_RED   = (244, 63, 94)


class MarketChart(QFrame):
    """
    The orderflow battle zone: a price curve overlaid with a tier-aware
    Bubble Footprint (retail · large · whale, with whale glow + pulse ripple),
    an ATR volatility envelope, daily-ATR projection levels, a Frontline
    'tug of war' bar, and battlefield annotations
    (🐋 whale, ⚡ breakout attempt, 🛡 liquidity walls) that turn the chart
    into a story of what is happening right now.
    """
    MAX_BUBBLES = 100
    MAX_MARKERS = 14
    WALL_MIN_STRENGTH = 40.0
    ALERT_COOLDOWN = 2.0       # seconds before the same alert can re-fire

    # (symbol, level, direction, price) — emitted when an alert line is crossed
    sigAlertTriggered = pyqtSignal(str, float, str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChartPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")

        # Price alerts, kept per-symbol; only the active symbol's lines are shown
        self._alerts = {}
        self._alert_symbol = None

        self.max_points = 2000
        self.price_history = []
        self.time_history = []
        self.atr_history = []

        # Bubble state — parallel arrays
        self.bubbles_x = []
        self.bubbles_y = []
        self.bubbles_sizes = []
        self.bubbles_tier = []     # "retail" | "large" | "whale"
        self.bubbles_rgb = []      # base directional colour (r, g, b)

        # Whale pulse ripples: dicts {x, y, rgb, age}
        self.pulses = []

        # Battlefield annotations: floating markers {x, y, text, rgb, age, max_age}
        self.markers = []
        self._last_overlay_state = None
        self._logged_whale_ts = deque(maxlen=60)

        self.band_mult = float(ATR_CONFIG.get("band_mult", 1.5))

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Header row: title + add-alert + live ATR readouts
        header = QHBoxLayout()
        self.title = QLabel("ORDERFLOW BATTLE ZONE", self)
        self.title.setObjectName("TitleLabel")
        header.addWidget(self.title)

        self.alert_btn = QPushButton("🔔 Add Alert", self)
        self.alert_btn.setToolTip("Add a price-alert line at the current price.\n"
                                  "Drag to move · right-click / double-click to remove.")
        self.alert_btn.setStyleSheet(
            "QPushButton { padding: 2px 10px; font-size: 11px; font-weight: bold;"
            " border-radius: 4px; }"
        )
        self.alert_btn.clicked.connect(lambda: self.add_alert())
        header.addWidget(self.alert_btn)
        header.addStretch(1)

        self.daily_label = QLabel("D-ATR --", self)
        self.daily_label.setObjectName("DailyAtrLabel")
        self.daily_label.setStyleSheet(
            "font-weight: 700; padding: 2px 8px; border-radius: 4px;"
            f"color: {COLORS['text_muted']};"
        )
        header.addWidget(self.daily_label)

        self.atr_label = QLabel("ATR --", self)
        self.atr_label.setObjectName("AtrLabel")
        self.atr_label.setStyleSheet(
            "font-weight: 700; padding: 2px 8px; border-radius: 4px;"
            f"color: {COLORS['text_muted']};"
        )
        header.addWidget(self.atr_label)
        layout.addLayout(header)

        # Frontline 'tug of war' bar — the one-glance verdict, above the chart
        self.frontline = FrontlineBar(self)
        layout.addWidget(self.frontline)

        # Configure pyqtgraph plot with a wall-clock time x-axis
        self.time_axis = TimeAxisItem(self, orientation="bottom")
        self.plot_widget = pg.PlotWidget(axisItems={"bottom": self.time_axis})
        self.plot_widget.setBackground(COLORS["bg_panel"])
        self.plot_widget.showGrid(x=True, y=True, alpha=0.15)

        # ATR volatility envelope (drawn beneath price + bubbles)
        self.atr_upper = pg.PlotDataItem(pen=pg.mkPen(COLORS["text_muted"], width=1,
                                                      style=pg.QtCore.Qt.PenStyle.DashLine))
        self.atr_lower = pg.PlotDataItem(pen=pg.mkPen(COLORS["text_muted"], width=1,
                                                      style=pg.QtCore.Qt.PenStyle.DashLine))
        self.plot_widget.addItem(self.atr_upper)
        self.plot_widget.addItem(self.atr_lower)
        self.atr_fill = pg.FillBetweenItem(self.atr_upper, self.atr_lower,
                                           brush=pg.mkBrush(16, 185, 129, 35))
        self.plot_widget.addItem(self.atr_fill)

        # Price curve
        self.price_curve = self.plot_widget.plot(
            pen=pg.mkPen(COLORS["accent"], width=2),
            name="Price"
        )

        # Footprint layers, back-to-front: glow halos → pulse rings → cores
        self.glow_scatter  = pg.ScatterPlotItem()
        self.pulse_scatter = pg.ScatterPlotItem()
        self.scatter       = pg.ScatterPlotItem()
        self.plot_widget.addItem(self.glow_scatter)
        self.plot_widget.addItem(self.pulse_scatter)
        self.plot_widget.addItem(self.scatter)

        # Daily-ATR projected envelope: horizontal levels at
        # today's open and open ± daily ATR (the expected day's range).
        def _hline(color, dash, label):
            line = pg.InfiniteLine(
                angle=0, movable=False,
                pen=pg.mkPen(color, width=1, style=dash),
                label=label,
                labelOpts={"position": 0.02, "color": color,
                           "fill": (0, 0, 0, 0), "movable": False},
            )
            line.setVisible(False)
            self.plot_widget.addItem(line, ignoreBounds=True)
            return line

        self.d_upper = _hline(COLORS["text_muted"], pg.QtCore.Qt.PenStyle.DotLine,  "D-ATR +")
        self.d_open  = _hline(COLORS["text_muted"], pg.QtCore.Qt.PenStyle.DashLine, "D-Open")
        self.d_lower = _hline(COLORS["text_muted"], pg.QtCore.Qt.PenStyle.DotLine,  "D-ATR -")

        # Liquidity walls (absorption): horizontal defended levels with labels
        self.sell_wall_line = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(_RED, width=2),
            label="🛡 SELL WALL",
            labelOpts={"position": 0.86, "color": _RED, "fill": (0, 0, 0, 0)},
        )
        self.buy_wall_line = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(_GREEN, width=2),
            label="🛡 BUY WALL",
            labelOpts={"position": 0.14, "color": _GREEN, "fill": (0, 0, 0, 0)},
        )
        for wl in (self.sell_wall_line, self.buy_wall_line):
            wl.setVisible(False)
            wl.setZValue(40)
            self.plot_widget.addItem(wl, ignoreBounds=True)

        # ── Trade-plan overlay (Entry Signal Engine) ──────────────────────
        # Garis entry/stop/TP + shading zona risk (merah) & reward (hijau).
        # Hanya tampil saat ada setup ACTIVE; harga dibekukan oleh engine.
        def _plan_line(color, width, dash, label, pos):
            line = pg.InfiniteLine(
                angle=0, movable=False,
                pen=pg.mkPen(color, width=width, style=dash),
                label=label,
                labelOpts={"position": pos, "color": color,
                           "fill": (11, 11, 13, 180), "movable": False},
            )
            line.setVisible(False)
            line.setZValue(45)
            self.plot_widget.addItem(line, ignoreBounds=True)
            return line

        _solid = pg.QtCore.Qt.PenStyle.SolidLine
        _dash  = pg.QtCore.Qt.PenStyle.DashLine
        _dot   = pg.QtCore.Qt.PenStyle.DotLine
        self.plan_entry_line = _plan_line(COLORS["accent"], 2, _solid, "🎯 ENTRY", 0.30)
        self.plan_stop_line  = _plan_line(_RED,   2, _dash, "✖ STOP", 0.30)
        self.plan_tp1_line   = _plan_line(_GREEN, 1.5, _dot, "TP1", 0.30)
        self.plan_tp2_line   = _plan_line(_GREEN, 1.5, _dot, "TP2", 0.30)

        self.plan_risk_region = pg.LinearRegionItem(
            orientation="horizontal", movable=False,
            brush=pg.mkBrush(244, 63, 94, 26), pen=pg.mkPen(None))
        self.plan_reward_region = pg.LinearRegionItem(
            orientation="horizontal", movable=False,
            brush=pg.mkBrush(16, 185, 129, 20), pen=pg.mkPen(None))
        for reg in (self.plan_risk_region, self.plan_reward_region):
            reg.setVisible(False)
            reg.setZValue(-5)
            self.plot_widget.addItem(reg, ignoreBounds=True)

        self._plan_active = False

        # Floating annotation pool (🐋 whale, ⚡ breakout) — reused each frame
        self._marker_pool = []
        for _ in range(self.MAX_MARKERS):
            t = pg.TextItem("", anchor=(0.5, 1.25))
            t.setZValue(60)
            t.setVisible(False)
            self.plot_widget.addItem(t, ignoreBounds=True)
            self._marker_pool.append(t)

        layout.addWidget(self.plot_widget)

    # ── Price alerts ──────────────────────────────────────────────────────

    def set_symbol(self, symbol: str):
        """Show this symbol's alert lines (alerts are kept per-symbol)."""
        if symbol == self._alert_symbol:
            return
        for ln in self._alerts.get(self._alert_symbol, []):
            self.plot_widget.removeItem(ln)
        self._alert_symbol = symbol
        for ln in self._alerts.setdefault(symbol, []):
            self.plot_widget.addItem(ln)

    def add_alert(self, price: float = None):
        """Add a draggable alert line (defaults to the current price)."""
        if self._alert_symbol is None:
            return
        if price is None or price <= 0:
            price = self.price_history[-1] if self.price_history else 0.0
        if not price or price <= 0:
            return
        line = AlertLine(price)
        line.sigRemove.connect(self._remove_alert)
        self._alerts.setdefault(self._alert_symbol, []).append(line)
        self.plot_widget.addItem(line)

    def _remove_alert(self, line):
        self.plot_widget.removeItem(line)
        for lst in self._alerts.values():
            if line in lst:
                lst.remove(line)
                break

    def _check_alerts(self, prev: float, cur: float):
        """Fire any alert whose level was crossed between prev and cur price."""
        if self._alert_symbol is None:
            return
        now = time.time()
        for line in self._alerts.get(self._alert_symbol, []):
            if getattr(line, "moving", False):     # don't fire while dragging
                continue
            lvl = line.value()
            crossed = (prev < lvl <= cur) or (prev > lvl >= cur)
            if crossed and now >= line._cooldown_until:
                line._cooldown_until = now + self.ALERT_COOLDOWN
                direction = "up" if cur >= prev else "down"
                self.sigAlertTriggered.emit(self._alert_symbol, float(lvl),
                                            direction, float(cur))

    # ── Reset (called on symbol switch) ───────────────────────────────────

    def reset(self, symbol: str = ""):
        if symbol:
            self.set_symbol(symbol)
        self.price_history.clear()
        self.time_history.clear()
        self.atr_history.clear()
        self.bubbles_x.clear()
        self.bubbles_y.clear()
        self.bubbles_sizes.clear()
        self.bubbles_tier.clear()
        self.bubbles_rgb.clear()
        self.pulses.clear()
        self.markers.clear()
        self._last_overlay_state = None
        self._logged_whale_ts.clear()
        self.scatter.clear()
        self.glow_scatter.clear()
        self.pulse_scatter.clear()
        self.sell_wall_line.setVisible(False)
        self.buy_wall_line.setVisible(False)
        self._hide_trade_plan()
        for t in self._marker_pool:
            t.setVisible(False)
        if symbol:
            self.title.setText(f"ORDERFLOW BATTLE ZONE — {symbol}")

    # ── Trade-plan overlay (Entry Signal Engine) ──────────────────────────

    def _hide_trade_plan(self):
        self._plan_active = False
        for it in (self.plan_entry_line, self.plan_stop_line,
                   self.plan_tp1_line, self.plan_tp2_line,
                   self.plan_risk_region, self.plan_reward_region):
            it.setVisible(False)

    def update_trade_plan(self, entry: dict | None):
        """Gambar plan dari EntrySignalEngine: garis entry/stop/TP + zona R/R."""
        if not entry or entry.get("phase") != "ACTIVE" or not entry.get("plan"):
            self._hide_trade_plan()
            return
        plan = entry["plan"]
        side = plan.get("side", "LONG")

        # Marker sekali saat setup baru menyala
        if entry.get("new_fire") and self.price_history:
            rgb = _GREEN if side == "LONG" else _RED
            self._spawn_marker(len(self.price_history) - 1, self.price_history[-1],
                               f"🎯 {side} ENTRY", rgb, 40)

        self.plan_entry_line.setPos(plan["entry"])
        self.plan_entry_line.label.setFormat(f"🎯 ENTRY {side}")
        self.plan_stop_line.setPos(plan["stop"])
        self.plan_tp1_line.setPos(plan["tp1"])
        self.plan_tp1_line.label.setFormat(f"TP1 · R {plan['rr1']:.1f}")
        self.plan_tp2_line.setPos(plan["tp2"])
        self.plan_tp2_line.label.setFormat(f"TP2 · R {plan['rr2']:.1f}")

        self.plan_risk_region.setRegion(sorted((plan["entry"], plan["stop"])))
        self.plan_reward_region.setRegion(sorted((plan["entry"], plan["tp1"])))

        if not self._plan_active:
            self._plan_active = True
            for it in (self.plan_entry_line, self.plan_stop_line,
                       self.plan_tp1_line, self.plan_tp2_line,
                       self.plan_risk_region, self.plan_reward_region):
                it.setVisible(True)

    # ── ATR readouts ──────────────────────────────────────────────────────

    def _update_atr_readout(self, atr: dict):
        """Refresh the header chip and recolour the volatility envelope."""
        regime = atr.get("regime", "warming")
        value  = atr.get("value", 0.0)
        pct    = atr.get("pct", 0.0)
        r, g, b = ATR_REGIME_COLORS.get(regime, ATR_REGIME_COLORS["warming"])

        if atr.get("ready", False):
            text = f"ATR {value:,.4g}  ({pct:.3f}%)  ·  {regime.upper()}"
        else:
            done = atr.get("bars_completed", 0)
            need = atr.get("period", 14)
            text = f"ATR warming  {done}/{need} bars"

        self.atr_label.setText(text)
        self.atr_label.setStyleSheet(
            f"font-weight: 700; padding: 2px 8px; border-radius: 4px;"
            f"color: rgb({r},{g},{b}); background: rgba({r},{g},{b},28);"
        )

        # Pen + fill follow the regime colour
        self.atr_upper.setPen(pg.mkPen(r, g, b, 160, width=1,
                                       style=pg.QtCore.Qt.PenStyle.DashLine))
        self.atr_lower.setPen(pg.mkPen(r, g, b, 160, width=1,
                                       style=pg.QtCore.Qt.PenStyle.DashLine))
        self.atr_fill.setBrush(pg.mkBrush(r, g, b, 40))

    def _update_daily_atr(self, d: dict):
        """Refresh the daily-ATR chip and reposition the projected band lines."""
        state = d.get("state", "warming")
        r, g, b = DAILY_STATE_COLORS.get(state, DAILY_STATE_COLORS["warming"])

        if not d.get("ready", False):
            days = d.get("days", 0)
            need = d.get("period", 14)
            self.daily_label.setText(f"D-ATR warming  {days}/{need}d")
            self.daily_label.setStyleSheet(
                "font-weight: 700; padding: 2px 8px; border-radius: 4px;"
                f"color: rgb({r},{g},{b});"
            )
            self.d_upper.setVisible(False)
            self.d_open.setVisible(False)
            self.d_lower.setVisible(False)
            return

        atr      = d.get("atr", 0.0)
        atr_pct  = d.get("atr_pct", 0.0)
        used     = d.get("range_used_pct", 0.0)
        move_atr = d.get("move_from_open_atr", 0.0)
        arrow    = "▲" if move_atr >= 0 else "▼"

        self.daily_label.setText(
            f"D-ATR {atr:,.4g} ({atr_pct:.2f}%)  ·  used {used:.0f}%  ·  "
            f"{arrow}{abs(move_atr):.2f}σ  ·  {state.upper()}"
        )
        self.daily_label.setStyleSheet(
            "font-weight: 700; padding: 2px 8px; border-radius: 4px;"
            f"color: rgb({r},{g},{b}); background: rgba({r},{g},{b},28);"
        )

        # Position the projected daily envelope (open ± ATR)
        for line, key, col in (
            (self.d_upper, "upper_band", (r, g, b)),
            (self.d_open,  "today_open", (148, 163, 184)),
            (self.d_lower, "lower_band", (r, g, b)),
        ):
            val = d.get(key, 0.0)
            if val and val > 0:
                line.setPen(pg.mkPen(col[0], col[1], col[2], 150, width=1,
                                     style=line.pen.style()))
                line.setPos(val)
                line.setVisible(True)
            else:
                line.setVisible(False)

    # ── Bubble footprint ──────────────────────────────────────────────────

    @staticmethod
    def _classify_tier(notional_usd, large_usd, block_usd):
        whale_ratio = (large_usd + block_usd) / max(notional_usd, 1.0)
        if block_usd > 0 or whale_ratio >= 0.55:
            return "whale"
        if large_usd > 0 or whale_ratio >= 0.25:
            return "large"
        return "retail"

    def _add_bubble(self, price, inst):
        notional_usd = inst.get("notional_usd", 0.0)
        if notional_usd <= 0:
            return
        delta_vel = inst.get("delta_velocity", 0.0)
        large_usd = inst.get("large_usd", 0.0)
        block_usd = inst.get("block_usd", 0.0)

        tier = self._classify_tier(notional_usd, large_usd, block_usd)
        rgb  = _GREEN if delta_vel >= 0 else _RED

        # Log-scaled core size, with a higher floor for big players
        size = int(np.clip(5 + 12 * np.log10(max(notional_usd, 100) / 100), 5, 60))
        if tier == "whale":
            size = max(size, 24)
        elif tier == "large":
            size = max(size, 15)

        current_x = len(self.price_history) - 1
        self.bubbles_x.append(current_x)
        self.bubbles_y.append(price)
        self.bubbles_sizes.append(size)
        self.bubbles_tier.append(tier)
        self.bubbles_rgb.append(rgb)

        # Whales emit a pulse ripple (⬤)))
        if tier == "whale":
            self.pulses.append({"x": current_x, "y": price, "rgb": rgb, "age": 0})

        if len(self.bubbles_x) > self.MAX_BUBBLES:
            for arr in (self.bubbles_x, self.bubbles_y, self.bubbles_sizes,
                        self.bubbles_tier, self.bubbles_rgb):
                arr.pop(0)

    def _render_bubbles(self):
        core_spots, glow_spots = [], []
        for i in range(len(self.bubbles_x)):
            x, y = self.bubbles_x[i], self.bubbles_y[i]
            size = self.bubbles_sizes[i]
            tier = self.bubbles_tier[i]
            r, g, b = self.bubbles_rgb[i]

            if tier == "whale":
                core_alpha, pen = 255, pg.mkPen(min(r + 60, 255), min(g + 60, 255),
                                                min(b + 60, 255), 230, width=1.6)
                glow_spots.append({"pos": (x, y), "size": size * 2.1,
                                   "pen": None, "brush": pg.mkBrush(r, g, b, 55)})
            elif tier == "large":
                core_alpha, pen = 215, None
                glow_spots.append({"pos": (x, y), "size": size * 1.6,
                                   "pen": None, "brush": pg.mkBrush(r, g, b, 38)})
            else:  # retail
                core_alpha, pen = 120, None

            core_spots.append({"pos": (x, y), "size": size, "pen": pen,
                               "brush": pg.mkBrush(r, g, b, core_alpha)})

        self.glow_scatter.setData(glow_spots)
        self.scatter.setData(core_spots)

    def _render_pulses(self):
        """Grow + fade whale pulse rings, then prune the dead ones."""
        spots = []
        for pl in self.pulses:
            r, g, b = pl["rgb"]
            alpha = int(max(0, 150 - pl["age"] * 20))
            if alpha <= 0:
                continue
            size = 20 + pl["age"] * 7
            spots.append({"pos": (pl["x"], pl["y"]), "size": size,
                          "pen": pg.mkPen(r, g, b, alpha, width=2), "brush": None})
            pl["age"] += 1
        self.pulses = [pl for pl in self.pulses if pl["age"] <= 8]
        self.pulse_scatter.setData(spots)

    # ── Battlefield overlay (story annotations) ───────────────────────────

    def _update_wall(self, line, strength, kind):
        """Place a liquidity wall at the recent swing it is defending."""
        if strength < self.WALL_MIN_STRENGTH or len(self.price_history) < 5:
            line.setVisible(False)
            return
        window = self.price_history[-150:]
        level = max(window) if kind == "sell" else min(window)
        r, g, b = _RED if kind == "sell" else _GREEN
        alpha = int(min(255, 90 + strength * 1.6))
        width = 1.0 + strength / 28.0
        line.setPen(pg.mkPen(r, g, b, alpha, width=width))
        line.setPos(level)
        line.setVisible(True)

    def _update_overlays(self, battle):
        if not battle:
            self.sell_wall_line.setVisible(False)
            self.buy_wall_line.setVisible(False)
            return
        cur_x = len(self.price_history) - 1
        price = self.price_history[-1]

        # 🐋 Whale annotations at the price they hit (de-duped by event ts)
        for ev in battle.get("events", []):
            if ev.get("kind") != "WHALE":
                continue
            ts = ev.get("ts", 0.0)
            if ts in self._logged_whale_ts:
                continue
            self._logged_whale_ts.append(ts)
            side = ev.get("side", "")
            rgb = _GREEN if side == "BUY" else _RED
            self._spawn_marker(cur_x, price, f"🐋 WHALE {side}", rgb, 30)

        # ⚡ Breakout attempt / 🚀 breakout on battlefield state transitions
        state = battle.get("state")
        dominant = battle.get("dominant", "NEUTRAL")
        if state != self._last_overlay_state:
            self._last_overlay_state = state
            side = "BUY" if dominant == "BUYERS" else "SELL" if dominant == "SELLERS" else ""
            if side:
                rgb = _GREEN if side == "BUY" else _RED
                if state == "ASSAULT":
                    self._spawn_marker(cur_x, price, "⚡ BREAKOUT ATTEMPT", rgb, 28)
                elif state == "BREAKTHROUGH":
                    self._spawn_marker(cur_x, price, f"🚀 {side} BREAKOUT", rgb, 34)

        # 🛡 Liquidity walls from absorption
        fort = battle.get("fortress", {})
        self._update_wall(self.sell_wall_line, float(fort.get("sell_wall", 0.0)), "sell")
        self._update_wall(self.buy_wall_line,  float(fort.get("buy_wall", 0.0)),  "buy")

    def _spawn_marker(self, x, y, text, rgb, max_age):
        self.markers.append({"x": x, "y": y, "text": text, "rgb": rgb,
                             "age": 0, "max_age": max_age})
        if len(self.markers) > self.MAX_MARKERS:
            self.markers.pop(0)

    def _render_markers(self):
        """Age, fade and lay out the floating annotations onto the pool."""
        self.markers = [m for m in self.markers if m["age"] <= m["max_age"]][-self.MAX_MARKERS:]
        for i, t in enumerate(self._marker_pool):
            if i >= len(self.markers):
                t.setVisible(False)
                continue
            m = self.markers[i]
            frac = m["age"] / max(m["max_age"], 1)
            alpha = 255 if frac < 0.5 else int(max(0, 255 * (1.0 - (frac - 0.5) / 0.5)))
            r, g, b = m["rgb"]
            t.setText(m["text"], color=(r, g, b, alpha))
            t.setPos(m["x"], m["y"])
            t.setVisible(m["x"] >= 0)
            m["age"] += 1

    # ── Main update ───────────────────────────────────────────────────────

    def update_data(self, price: float, velocity_metrics: dict):
        """Adds live data point and triggers refresh."""
        # Wait for the first real price before plotting anything — otherwise the
        # curve starts at 0 and the y-axis spans from 0 to the live price.
        if price is None or price <= 0.0:
            return

        # Slide the window if we have reached max capacity
        slide = len(self.price_history) >= self.max_points

        if slide:
            self.price_history.pop(0)
            if self.time_history:
                self.time_history.pop(0)
            if self.atr_history:
                self.atr_history.pop(0)
            # Shift footprint + pulses + markers left by 1 coordinate unit
            self.bubbles_x = [x - 1 for x in self.bubbles_x]
            for pl in self.pulses:
                pl["x"] -= 1
            for m in self.markers:
                m["x"] -= 1

            valid = [i for i, x in enumerate(self.bubbles_x) if x >= 0]
            self.bubbles_x     = [self.bubbles_x[i] for i in valid]
            self.bubbles_y     = [self.bubbles_y[i] for i in valid]
            self.bubbles_sizes = [self.bubbles_sizes[i] for i in valid]
            self.bubbles_tier  = [self.bubbles_tier[i] for i in valid]
            self.bubbles_rgb   = [self.bubbles_rgb[i] for i in valid]
            self.pulses  = [pl for pl in self.pulses if pl["x"] >= 0]
            self.markers = [m for m in self.markers if m["x"] >= 0]

        # Price-alert crossing check (against the previous price)
        if self.price_history:
            self._check_alerts(self.price_history[-1], price)

        self.price_history.append(price)
        self.time_history.append(time.time())

        # ── Frontline 'tug of war' verdict ────────────────────────────
        self.frontline.set_battle(velocity_metrics.get("battle"))

        # ── ATR volatility envelope ───────────────────────────────────
        atr = velocity_metrics.get("atr", {}) or {}
        atr_value = float(atr.get("value", 0.0))
        self.atr_history.append(atr_value)
        self._update_atr_readout(atr)

        # ── Daily ATR vs current price movement ───────────────────────
        self._update_daily_atr(velocity_metrics.get("daily_atr", {}) or {})

        x_indices = list(range(len(self.price_history)))
        self.price_curve.setData(x_indices, self.price_history)

        prices = np.asarray(self.price_history, dtype=np.float64)
        atrs   = np.asarray(self.atr_history,   dtype=np.float64)
        band   = self.band_mult * atrs
        self.atr_upper.setData(x_indices, prices + band)
        self.atr_lower.setData(x_indices, prices - band)

        # ── Footprint bubbles (tier-aware glow + whale pulse) ─────────
        self._add_bubble(price, velocity_metrics.get("instantaneous", {}))
        self._render_pulses()
        self._render_bubbles()

        # ── Battlefield overlay (🐋 whale · ⚡ breakout · 🛡 walls) ─────
        self._update_overlays(velocity_metrics.get("battle"))

        # ── Trade plan (Entry Signal Engine) ──────────────────────────
        self.update_trade_plan(velocity_metrics.get("entry"))
        self._render_markers()
