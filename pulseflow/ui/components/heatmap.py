"""
Bookmap-style order book liquidity heatmap.

Renders a 2D heatmap of *resting* limit-order liquidity: the Y axis is price,
the X axis is wall-clock time, and colour intensity is the size sitting at each
price level (top-20 depth snapshots streamed every 100 ms from Binance). On top
of the heatmap it overlays the live price corridor (best bid / mid / best ask)
and trade prints (dots sized by notional, green = aggressive buy, red = sell).

To span a long window (default 1 hour) without keeping 36 000 raw 100 ms
columns, snapshots are aggregated into fixed time buckets (one heatmap column
per ``COL_SECONDS``); within a bucket each price level's size is averaged. The
still-forming bucket is rendered as a live partial column on the right edge.

Only the focused symbol is fed (see dashboard wiring); nothing is persisted.
The heatmap image is rebuilt with a single vectorised ``np.histogram2d`` call,
throttled to a few FPS, and ``_repaint`` is skipped while the tab is hidden.
"""

import math
import time
import numpy as np
import pyqtgraph as pg
from collections import deque
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QSlider,
)
from PyQt6.QtCore import QRectF, Qt
from pulseflow.ui.styles import COLORS
from pulseflow.ui.components.chart import TimeAxisItem

try:
    from scipy.ndimage import gaussian_filter as _gaussian_filter
except Exception:                       # pragma: no cover
    _gaussian_filter = None


# Bookmap-like heat ramp: near-black → deep blue → cyan → green → yellow → red
_HEAT_STOPS = [0.0, 0.12, 0.30, 0.50, 0.70, 0.86, 1.0]
_HEAT_COLORS = [
    (11, 11, 13),      # empty
    (18, 24, 90),      # faint resting liquidity
    (28, 80, 190),     # blue
    (0, 180, 200),     # cyan
    (40, 200, 90),     # green
    (240, 220, 40),    # yellow
    (255, 70, 40),     # heavy wall — red
]

_GREEN = (16, 185, 129)
_RED   = (244, 63, 94)

# Selectable time windows → number of 1 s columns held in view
_RANGES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


class OrderBookHeatmap(QFrame):
    """Time × price liquidity heatmap with price corridor and trade prints."""

    COL_SECONDS = 1.0        # wall-clock seconds aggregated into one column
    NBINS       = 160        # price buckets (heatmap vertical resolution)
    MAX_TRADES  = 8000       # trade prints retained before pruning
    REPAINT_MIN = 0.18       # min seconds between repaints (throttle)
    MAX_LIQ_ZONES = 8        # predicted-liquidity overlay zones
    SMOOTH_SIGMA = (0.8, 1.4)  # gaussian blur σ (time, price) when smoothing on

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HeatmapPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")

        self._symbol = None
        self._max_cols = _RANGES["1h"]
        self._smooth = False
        self._vmax = 1.0
        self._last_paint = 0.0
        self._last_liq = 0.0           # throttle for liquidity-zone overlay
        self._right_x = 0              # x of the right edge (latest column)
        self._pdec = None              # price decimals for level aggregation

        # Committed columns (index 0 = oldest in view)
        self.snapshots = deque(maxlen=self._max_cols)   # each np.ndarray[K,2]
        self._ctimes   = deque(maxlen=self._max_cols)   # bucket timestamps
        self.mids      = deque(maxlen=self._max_cols)
        self.best_bids = deque(maxlen=self._max_cols)
        self.best_asks = deque(maxlen=self._max_cols)
        self._committed = 0                              # total columns committed

        # Cached flat point arrays for the (immutable) committed columns —
        # rebuilt only when a column commits, not every repaint.
        self._cache_dirty = True
        self._cflat = None                               # (cols, prices, sizes)

        # Current (still-forming) bucket accumulators
        self._cur_bucket_id = None
        self._cur_levels = []
        self._cur_n = 0
        self._cur_mid = self._cur_bid = self._cur_ask = 0.0
        self._cur_ts = 0.0

        # Trade prints: dicts {col(abs), price, notional, buy}
        self.trades = deque(maxlen=self.MAX_TRADES)
        # Cached QBrush by (buy, alpha bucket) — keeps pyqtgraph's symbol atlas
        # small (few unique size/brush combos) so scatter setData stays fast.
        self._brush_cache = {}

        # Read by TimeAxisItem; rebuilt (render-aligned) each repaint
        self.time_history = []

        self._init_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title = QLabel("LIQUIDITY HEATMAP", self)
        self.title.setObjectName("TitleLabel")
        header.addWidget(self.title)
        header.addStretch(1)

        opacity_lbl = QLabel("Opacity", self)
        opacity_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        header.addWidget(opacity_lbl)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedWidth(90)
        self.opacity_slider.setToolTip("Heatmap transparency")
        # valueChanged is wired after self.image exists (see below)
        header.addWidget(self.opacity_slider)

        self.range_combo = QComboBox(self)
        self.range_combo.addItems(list(_RANGES.keys()))
        self.range_combo.setCurrentText("1h")
        self.range_combo.setToolTip("Time window shown on the X axis")
        self.range_combo.currentTextChanged.connect(self._on_range_changed)
        header.addWidget(self.range_combo)

        self.smooth_btn = QPushButton("✨ Smooth", self)
        self.smooth_btn.setCheckable(True)
        self.smooth_btn.setToolTip("Blur the heatmap (gaussian) for a softer look")
        self.smooth_btn.setStyleSheet(
            "QPushButton { padding: 2px 10px; font-size: 11px; font-weight: bold;"
            " border-radius: 4px; }"
            "QPushButton:checked { background: #1c4a44; color: #00ffd2; }"
        )
        self.smooth_btn.toggled.connect(self._on_smooth_toggled)
        header.addWidget(self.smooth_btn)

        self.readout = QLabel("waiting for order book…", self)
        self.readout.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-weight: 700; padding: 2px 8px;"
        )
        header.addWidget(self.readout)
        layout.addLayout(header)

        # Wall-clock x-axis (reuses the chart's TimeAxisItem; reads our
        # time_history, which is rebuilt render-aligned each repaint)
        self.time_axis = TimeAxisItem(self, orientation="bottom")
        self.plot_widget = pg.PlotWidget(axisItems={"bottom": self.time_axis})
        self.plot_widget.setBackground(COLORS["bg_panel"])
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        self.plot_widget.showGrid(x=False, y=True, alpha=0.12)

        self.image = pg.ImageItem()
        cmap = pg.ColorMap(_HEAT_STOPS, _HEAT_COLORS)
        self.image.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self.plot_widget.addItem(self.image)
        # Now that the image exists, wire the opacity slider to it
        self.image.setOpacity(self.opacity_slider.value() / 100.0)
        self.opacity_slider.valueChanged.connect(
            lambda v: self.image.setOpacity(v / 100.0)
        )

        # Price corridor: best bid (green) · mid (cyan) · best ask (red)
        self.bid_curve = self.plot_widget.plot(pen=pg.mkPen(_GREEN, width=1))
        self.ask_curve = self.plot_widget.plot(pen=pg.mkPen(_RED, width=1))
        self.mid_curve = self.plot_widget.plot(
            pen=pg.mkPen(COLORS["accent"], width=2)
        )

        self.trade_scatter = pg.ScatterPlotItem(pxMode=True)
        self.trade_scatter.setZValue(20)
        self.plot_widget.addItem(self.trade_scatter)

        # Crosshair (follows the mouse) + a price/time readout at the cursor
        _xpen = pg.mkPen((170, 170, 185), width=1, style=Qt.PenStyle.DashLine)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=_xpen)
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=_xpen)
        self.cursor_label = pg.TextItem(
            anchor=(0, 1), color=(235, 235, 240), fill=(0, 0, 0, 170)
        )
        for it in (self.v_line, self.h_line, self.cursor_label):
            it.setZValue(55)
            it.setVisible(False)
            self.plot_widget.addItem(it, ignoreBounds=True)
        self._mouse_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved,
            rateLimit=60, slot=self._on_mouse_moved,
        )

        # Predicted-liquidity zones (from LiquidityProbabilityEngine): a pool of
        # horizontal lines + right-edge labels overlaid ON the actual resting
        # liquidity → "predicted vs actual" battlefield.
        self._liq_lines = []
        self._liq_labels = []
        for _ in range(self.MAX_LIQ_ZONES):
            ln = pg.InfiniteLine(angle=0, movable=False)
            ln.setZValue(40)
            ln.setVisible(False)
            self.plot_widget.addItem(ln, ignoreBounds=True)
            lbl = pg.TextItem(anchor=(1, 0.5))
            lbl.setZValue(45)
            lbl.setVisible(False)
            self.plot_widget.addItem(lbl, ignoreBounds=True)
            self._liq_lines.append(ln)
            self._liq_labels.append(lbl)

        layout.addWidget(self.plot_widget)

    # ── Header actions ────────────────────────────────────────────────

    def _on_range_changed(self, text: str):
        self._max_cols = _RANGES.get(text, _RANGES["1h"])
        # Recreate the column deques keeping the most recent items that fit.
        self.snapshots = deque(self.snapshots, maxlen=self._max_cols)
        self._ctimes   = deque(self._ctimes,   maxlen=self._max_cols)
        self.mids      = deque(self.mids,       maxlen=self._max_cols)
        self.best_bids = deque(self.best_bids,  maxlen=self._max_cols)
        self.best_asks = deque(self.best_asks,  maxlen=self._max_cols)
        self._cache_dirty = True
        self._repaint(force=True)

    def _on_smooth_toggled(self, checked: bool):
        self._smooth = checked
        self._repaint(force=True)

    # ── Public API ────────────────────────────────────────────────────

    def set_symbol(self, symbol: str):
        self.reset(symbol)

    def reset(self, symbol: str = None):
        if symbol is not None:
            self._symbol = symbol
        self._vmax = 1.0
        self._pdec = None
        self._committed = 0
        self._cur_bucket_id = None
        self._cur_levels = []
        self._cur_n = 0
        self._cur_mid = self._cur_bid = self._cur_ask = 0.0
        self._cur_ts = 0.0
        self._cache_dirty = True
        self._cflat = None
        self.snapshots.clear()
        self._ctimes.clear()
        self.mids.clear()
        self.best_bids.clear()
        self.best_asks.clear()
        self.trades.clear()
        self.time_history = []
        self.image.clear()
        for c in (self.bid_curve, self.ask_curve, self.mid_curve):
            c.clear()
        self.trade_scatter.clear()
        for it in (self.v_line, self.h_line, self.cursor_label):
            it.setVisible(False)
        for ln in self._liq_lines:
            ln.setVisible(False)
        for lbl in self._liq_labels:
            lbl.setVisible(False)
        self.readout.setText("waiting for order book…")

    def update_liquidity(self, liq: dict | None):
        """Overlay predicted-liquidity zones (top levels) as horizontal lines.
        Throttled ~1.2 s so the zones read as stable, not flickering per-tick."""
        now = time.time()
        if now - self._last_liq < 1.2:
            return
        self._last_liq = now
        levels = (liq or {}).get("levels", [])
        # tampilkan hanya zona yang cukup kuat agar tidak ramai
        levels = [L for L in levels if float(L.get("prob", 0)) >= 55.0]
        levels = levels[:self.MAX_LIQ_ZONES]
        for i in range(self.MAX_LIQ_ZONES):
            ln = self._liq_lines[i]
            lbl = self._liq_labels[i]
            if i < len(levels):
                L = levels[i]
                prob = float(L["prob"])
                r, g, b = L.get("color", (0, 255, 210))
                alpha = int(90 + 130 * (prob / 100.0))
                width = 1 + int(prob >= 75) + int(prob >= 88)
                ln.setPen(pg.mkPen(r, g, b, alpha, width=width,
                                   style=Qt.PenStyle.DashLine))
                ln.setPos(L["price"])
                ln.setVisible(True)
                mig = float(L.get("migration", 0.0))
                tag = "  ↑" if mig > 0.25 else "  ↓" if mig < -0.25 else ""
                lbl.setText(f"{int(prob)}%{tag}", color=(r, g, b))
                lbl.setPos(self._right_x, L["price"])
                lbl.setVisible(True)
            else:
                ln.setVisible(False)
                lbl.setVisible(False)

    def update_depth(self, bids: list, asks: list, ts: float):
        """Ingest a top-N order book snapshot into the current time bucket."""
        if not bids or not asks:
            return

        arr = np.asarray(bids + asks, dtype=np.float64)   # (K, 2) price, size
        arr = arr[arr[:, 1] > 0.0]                        # drop padded zero levels
        if arr.shape[0] == 0:
            return

        best_bid = max(p for p, q in bids if q > 0)
        best_ask = min(p for p, q in asks if q > 0)
        mid = (best_bid + best_ask) / 2.0

        if self._pdec is None and mid > 0:
            self._pdec = max(0, 5 - int(math.floor(math.log10(mid))))

        bucket_id = int(ts // self.COL_SECONDS)
        if self._cur_bucket_id is None:
            self._cur_bucket_id = bucket_id
        elif bucket_id != self._cur_bucket_id:
            self._commit_bucket()
            self._cur_bucket_id = bucket_id

        self._cur_levels.append(arr)
        self._cur_n += 1
        self._cur_mid, self._cur_bid, self._cur_ask = mid, best_bid, best_ask
        self._cur_ts = ts

        self._repaint()

    def add_trade(self, price: float, size: float, is_buyer_maker: bool):
        """Record a trade print in the current (rightmost) column."""
        if price <= 0 or size <= 0 or self._cur_bucket_id is None:
            return
        self.trades.append({
            "col": self._committed,          # belongs to the in-progress column
            "price": price,
            "notional": price * size,
            "buy": not is_buyer_maker,       # buyer_maker == aggressive sell
        })

    # ── Crosshair ─────────────────────────────────────────────────────

    def _on_mouse_moved(self, evt):
        pos = evt[0]
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            for it in (self.v_line, self.h_line, self.cursor_label):
                it.setVisible(False)
            return
        vb = self.plot_widget.getPlotItem().vb
        mp = vb.mapSceneToView(pos)
        x, y = mp.x(), mp.y()
        self.v_line.setPos(x)
        self.h_line.setPos(y)

        i = int(round(x))
        th = self.time_history
        tstr = (time.strftime("%H:%M:%S", time.localtime(th[i]))
                if 0 <= i < len(th) else "")
        dec = self._pdec if self._pdec is not None else 4
        self.cursor_label.setText(f"{y:.{dec}f}   {tstr}")
        self.cursor_label.setPos(x, y)
        for it in (self.v_line, self.h_line, self.cursor_label):
            it.setVisible(True)

    # ── Aggregation ───────────────────────────────────────────────────

    def _aggregate(self, levels_list, n):
        """Collapse a bucket's snapshots into averaged (price, size) levels."""
        arr = np.vstack(levels_list)
        key = np.round(arr[:, 0], self._pdec if self._pdec is not None else 4)
        uniq, inv = np.unique(key, return_inverse=True)
        summ = np.zeros(len(uniq))
        np.add.at(summ, inv, arr[:, 1])
        return np.column_stack([uniq, summ / max(1, n)])

    def _commit_bucket(self):
        if self._cur_n > 0:
            self.snapshots.append(self._aggregate(self._cur_levels, self._cur_n))
            self._ctimes.append(self._cur_ts)
            self.mids.append(self._cur_mid)
            self.best_bids.append(self._cur_bid)
            self.best_asks.append(self._cur_ask)
            self._committed += 1
            self._cache_dirty = True
        self._cur_levels = []
        self._cur_n = 0

    # ── Rendering ─────────────────────────────────────────────────────

    def _repaint(self, force: bool = False):
        if not self.isVisible():
            return
        ncomm = len(self.snapshots)
        # Throttle scales with column count (1 s columns change slowly, so a
        # macro 1 h view need not repaint as often as a snappy 5 m view).
        interval = min(0.5, max(0.12, ncomm / 12000.0))
        now = time.time()
        if not force and (now - self._last_paint) < interval:
            return
        self._last_paint = now

        # Committed columns are immutable → flatten them once per commit (cached).
        if self._cache_dirty or self._cflat is None:
            if ncomm:
                cp, pp, sp = [], [], []
                for j, s in enumerate(self.snapshots):
                    cp.append(np.full(s.shape[0], j, dtype=np.float64))
                    pp.append(s[:, 0])
                    sp.append(s[:, 1])
                self._cflat = (np.concatenate(cp), np.concatenate(pp), np.concatenate(sp))
            else:
                z = np.empty(0)
                self._cflat = (z, z, z)
            self._cache_dirty = False
        ccols, cprices, csizes = self._cflat

        # Append the still-forming partial column (cheap, recomputed each frame).
        times = list(self._ctimes)
        mids  = list(self.mids)
        bids  = list(self.best_bids)
        asks  = list(self.best_asks)
        if self._cur_n > 0:
            part = self._aggregate(self._cur_levels, self._cur_n)
            cols_all   = np.concatenate([ccols, np.full(part.shape[0], ncomm, dtype=np.float64)])
            prices_all = np.concatenate([cprices, part[:, 0]])
            sizes_all  = np.concatenate([csizes, part[:, 1]])
            n = ncomm + 1
            times.append(self._cur_ts)
            mids.append(self._cur_mid)
            bids.append(self._cur_bid)
            asks.append(self._cur_ask)
        else:
            cols_all, prices_all, sizes_all = ccols, cprices, csizes
            n = ncomm

        if n == 0 or prices_all.size == 0:
            return

        # Price window from the levels in view (percentile-clipped, padded).
        pmin = float(np.percentile(prices_all, 1))
        pmax = float(np.percentile(prices_all, 99))
        if pmax <= pmin:
            pmin, pmax = float(prices_all.min()), float(prices_all.max())
            if pmax <= pmin:
                pmax = pmin + 1e-6
        pad = (pmax - pmin) * 0.04
        pmin -= pad
        pmax += pad

        img, _, _ = np.histogram2d(
            cols_all, prices_all,
            bins=[np.arange(n + 1) - 0.5, np.linspace(pmin, pmax, self.NBINS + 1)],
            weights=sizes_all,
        )  # shape (n, NBINS): axis0 → x (time), axis1 → y (price)

        if self._smooth:
            img = self._blur(img)

        nz = img[img > 0]
        target = float(np.percentile(nz, 97)) if nz.size else 1.0
        self._vmax = max(1e-9, 0.7 * self._vmax + 0.3 * target)

        self.image.setImage(img, autoLevels=False, levels=(0.0, self._vmax))
        self.image.setRect(QRectF(0.0, pmin, float(n), pmax - pmin))

        xs = np.arange(n)
        self.bid_curve.setData(xs, np.asarray(bids))
        self.ask_curve.setData(xs, np.asarray(asks))
        self.mid_curve.setData(xs, np.asarray(mids))

        # Time axis labels: render-aligned timestamps (index 0..n-1).
        self.time_history = times

        # Trade prints: absolute col → relative x; prune off-screen left.
        # Size and alpha are quantised so the symbol atlas stays tiny (fast).
        oldest_abs = self._committed - len(self.snapshots)
        while self.trades and self.trades[0]["col"] < oldest_abs:
            self.trades.popleft()
        if self.trades:
            denom = max(1, n - 1)
            xs, ys, szs, brs = [], [], [], []
            for t in self.trades:
                relx = t["col"] - oldest_abs
                if relx < 0:
                    continue
                sz = min(26.0, max(3.0, np.sqrt(t["notional"]) / 6.0))
                ab = min(245, (int(70 + 175 * (relx / denom)) // 40) * 40 + 40)
                xs.append(relx)
                ys.append(t["price"])
                szs.append(round(sz / 3.0) * 3.0)
                brs.append(self._brush(t["buy"], ab))
            if xs:
                self.trade_scatter.setData(
                    x=np.asarray(xs, dtype=np.float64),
                    y=np.asarray(ys, dtype=np.float64),
                    size=np.asarray(szs, dtype=np.float64),
                    pen=None, brush=brs,
                )
            else:
                self.trade_scatter.clear()
        else:
            self.trade_scatter.clear()

        self._right_x = max(1, n - 1)
        self.plot_widget.setXRange(0, self._right_x, padding=0.0)
        self.plot_widget.setYRange(pmin, pmax, padding=0.0)

        if bids:
            self.readout.setText(
                f"{self._symbol or ''}  bid {bids[-1]:.6g}  ask {asks[-1]:.6g}"
            )

    def _brush(self, buy: bool, alpha: int):
        key = (buy, alpha)
        b = self._brush_cache.get(key)
        if b is None:
            rgb = _GREEN if buy else _RED
            b = pg.mkBrush(rgb[0], rgb[1], rgb[2], alpha)
            self._brush_cache[key] = b
        return b

    def _blur(self, img: np.ndarray) -> np.ndarray:
        """Soften the heatmap. Uses scipy when available, else a numpy fallback."""
        st, sp = self.SMOOTH_SIGMA
        if _gaussian_filter is not None:
            return _gaussian_filter(img, sigma=(st, sp), mode="nearest")
        # Separable gaussian fallback (time axis then price axis)
        def kern(sigma):
            r = max(1, int(round(sigma * 2)))
            x = np.arange(-r, r + 1)
            k = np.exp(-(x ** 2) / (2 * sigma * sigma))
            return k / k.sum()
        kt, kp = kern(st), kern(sp)
        out = np.apply_along_axis(lambda m: np.convolve(m, kt, mode="same"), 0, img)
        out = np.apply_along_axis(lambda m: np.convolve(m, kp, mode="same"), 1, out)
        return out
