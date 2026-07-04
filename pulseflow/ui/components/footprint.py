import time
import queue

import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QFrame
)
from PyQt6.QtCore import Qt, QTimer, QRectF, QLineF
from PyQt6.QtGui import QPainter, QPen, QColor, QFont

from pulseflow.ui.styles import COLORS, QSS_STYLE


TIMEFRAMES = {"1m": 60, "3m": 180, "5m": 300}
MAX_CANDLES = 40
VISIBLE_CANDLES = 20


def _tick_size(price: float) -> float:
    if price >= 50000: return 10.0
    if price >= 5000:  return 5.0
    if price >= 1000:  return 1.0
    if price >= 100:   return 0.5
    if price >= 10:    return 0.1
    return 0.01


class FootprintCandle:
    __slots__ = ["open_time", "open", "high", "low", "close", "levels"]

    def __init__(self, open_time: float):
        self.open_time = open_time
        self.open = self.high = self.low = self.close = None
        self.levels: dict = {}  # price_level -> [buy_vol, sell_vol]

    def add_trade(self, price: float, size: float, is_buyer_maker: bool, tick: float):
        lvl = round(price / tick) * tick
        if lvl not in self.levels:
            self.levels[lvl] = [0.0, 0.0]
        # is_buyer_maker=True → taker is seller (aggressive sell) → sell vol
        # is_buyer_maker=False → taker is buyer (aggressive buy) → buy vol
        if is_buyer_maker:
            self.levels[lvl][1] += size
        else:
            self.levels[lvl][0] += size

        if self.open is None:
            self.open = self.high = self.low = price
        self.close = price
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price

    @property
    def is_empty(self) -> bool:
        return self.open is None

    @property
    def delta(self) -> float:
        return sum(v[0] - v[1] for v in self.levels.values())


class TimeAxis(pg.AxisItem):
    """X-axis yang menampilkan waktu candle (HH:MM) bukan index."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candles: list[FootprintCandle] = []

    def tickStrings(self, values, scale, spacing):
        n = len(self.candles)
        result = []
        for v in values:
            i = int(round(v))
            if 0 <= i < n and not self.candles[i].is_empty:
                result.append(time.strftime("%H:%M", time.localtime(self.candles[i].open_time)))
            else:
                result.append("")
        return result


class FootprintItem(pg.GraphicsObject):
    """
    Renderer footprint candle: OHLC wick + body delta + sel per price level
    dengan warna hijau (buy dominan) / merah (sell dominan) + teks buy×sell.
    """

    def __init__(self):
        super().__init__()
        self._candles: list[FootprintCandle] = []
        self._tick: float = 1.0
        self._bounds = QRectF(0, 0, 1, 1)

    def set_data(self, candles: list, tick: float):
        self._candles = candles
        self._tick = tick
        self._update_bounds()
        self.prepareGeometryChange()
        self.update()

    def _update_bounds(self):
        prices = []
        for c in self._candles:
            if not c.is_empty:
                prices.extend([c.high, c.low])
        if not prices:
            self._bounds = QRectF(0, 0, 1, 1)
            return
        n = len(self._candles)
        pad = self._tick * 2
        self._bounds = QRectF(-0.5, min(prices) - pad, n + 1.0, max(prices) - min(prices) + pad * 2)

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, p: QPainter, option, widget):
        if not self._candles:
            return

        tick = self._tick
        half_tick = tick * 0.5

        # Hitung max volume untuk scaling intensitas warna
        max_vol = 1.0
        for c in self._candles:
            for buy_v, sell_v in c.levels.values():
                mv = max(buy_v, sell_v)
                if mv > max_vol:
                    max_vol = mv

        # Hitung ukuran font adaptif berdasarkan zoom saat ini
        y_scale = abs(p.transform().m22())  # piksel per scene y-unit
        cell_px = tick * y_scale
        font_px = cell_px * 0.55
        font_pt = int(font_px * 72.0 / 96.0)
        draw_text = 5 <= font_pt <= 14
        if draw_text:
            p.setFont(QFont("Consolas", max(5, min(12, font_pt))))

        wick_pen  = QPen(QColor(110, 110, 125), 0)
        text_pen  = QPen(QColor(230, 230, 235, 210), 0)
        eq_brush  = QColor(45, 45, 58, 70)

        for i, candle in enumerate(self._candles):
            if candle.is_empty:
                continue

            x = float(i)
            hw = 0.40

            # Wick
            p.setPen(wick_pen)
            p.drawLine(QLineF(x, candle.low, x, candle.high))

            # Price level cells
            for lvl, (buy_v, sell_v) in candle.levels.items():
                cell = QRectF(x - hw, lvl - half_tick + tick * 0.01,
                              2 * hw, tick * 0.98)

                if buy_v > sell_v:
                    alpha = int(20 + 185 * (buy_v / max_vol))
                    p.fillRect(cell, QColor(16, 185, 129, alpha))
                elif sell_v > buy_v:
                    alpha = int(20 + 185 * (sell_v / max_vol))
                    p.fillRect(cell, QColor(244, 63, 94, alpha))
                else:
                    p.fillRect(cell, eq_brush)

                if draw_text:
                    p.setPen(text_pen)
                    mv = max(buy_v, sell_v)
                    if mv >= 1000:
                        txt = f"{int(buy_v)}×{int(sell_v)}"
                    elif mv >= 10:
                        txt = f"{buy_v:.1f}×{sell_v:.1f}"
                    else:
                        txt = f"{buy_v:.2f}×{sell_v:.2f}"
                    p.drawText(cell, Qt.AlignmentFlag.AlignCenter, txt)

            # Body (thin bar, warna sesuai delta)
            delta = candle.delta
            body_top = max(candle.open, candle.close)
            body_bot = min(candle.open, candle.close)
            body_h = max(body_top - body_bot, tick * 0.04)
            body_color = QColor(16, 185, 129, 230) if delta >= 0 else QColor(244, 63, 94, 230)
            p.fillRect(QRectF(x - 0.055, body_bot, 0.11, body_h), body_color)


class FootprintChart(QFrame):
    """Widget chart footprint: aggregasi trade → candle, render via FootprintItem."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChartPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")

        self._tf_seconds = 60
        self._candles: list[FootprintCandle] = []
        self._current: FootprintCandle | None = None
        self._tick = 1.0
        self._dirty = False

        self._init_ui()

        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._refresh)
        self._render_timer.start(200)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        self._time_axis = TimeAxis(orientation="bottom")
        self._time_axis.setTextPen(pg.mkPen(COLORS["text_muted"]))

        self._plot = pg.PlotWidget(axisItems={"bottom": self._time_axis})
        self._plot.setBackground(COLORS["bg_panel"])
        self._plot.showGrid(x=False, y=True, alpha=0.10)
        self._plot.getAxis("left").setTextPen(pg.mkPen(COLORS["text_muted"]))
        self._plot.getAxis("left").setStyle(tickLength=-5)
        self._plot.getAxis("bottom").setStyle(tickLength=-5)
        self._plot.setMouseEnabled(x=True, y=True)

        self._fp_item = FootprintItem()
        self._plot.addItem(self._fp_item)

        layout.addWidget(self._plot)

    def set_timeframe(self, tf_str: str):
        self._tf_seconds = TIMEFRAMES.get(tf_str, 60)
        self._candles.clear()
        self._current = None
        self._dirty = True

    def add_trade(self, price: float, size: float, is_buyer_maker: bool):
        self._tick = _tick_size(price)
        now = time.time()

        if self._current is None:
            candle_start = (now // self._tf_seconds) * self._tf_seconds
            self._current = FootprintCandle(candle_start)

        # Roll candle jika waktu sudah habis
        if now >= self._current.open_time + self._tf_seconds:
            if not self._current.is_empty:
                self._candles.append(self._current)
                if len(self._candles) > MAX_CANDLES:
                    self._candles.pop(0)
            candle_start = (now // self._tf_seconds) * self._tf_seconds
            self._current = FootprintCandle(candle_start)

        self._current.add_trade(price, size, is_buyer_maker, self._tick)
        self._dirty = True

    def _refresh(self):
        if not self._dirty:
            return
        self._dirty = False

        all_candles = self._candles.copy()
        if self._current and not self._current.is_empty:
            all_candles.append(self._current)

        if not all_candles:
            return

        self._fp_item.set_data(all_candles, self._tick)
        self._time_axis.candles = all_candles

        # Auto-scroll: tempel ke kanan, tampilkan VISIBLE_CANDLES terakhir
        n = len(all_candles)
        x_max = n - 0.5 + 1.5
        x_min = max(-0.5, n - VISIBLE_CANDLES - 0.5)
        self._plot.setXRange(x_min, x_max, padding=0)

        # Y range dari candle yang terlihat
        visible = all_candles[max(0, n - VISIBLE_CANDLES):]
        prices = [p for c in visible if not c.is_empty for p in [c.high, c.low]]
        if prices:
            pad = self._tick * 3
            self._plot.setYRange(min(prices) - pad, max(prices) + pad, padding=0)


class FootprintWindow(QMainWindow):
    """Window terpisah berisi Footprint Chart untuk satu symbol yang sudah dipilih."""

    def __init__(self, symbol: str, engine, parent=None):
        super().__init__(parent)
        self.symbol = symbol
        self._engine = engine
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"PulseFlow  |  Footprint Chart  —  {symbol}")
        self.resize(1000, 660)
        self.setStyleSheet(QSS_STYLE + f"""
            QComboBox {{
                background-color: #14141a;
                border: 1px solid #2d2d38;
                color: {COLORS["text_main"]};
                padding: 3px 8px;
                border-radius: 4px;
                font-size: 12px;
            }}
            QComboBox:focus {{ border: 1px solid {COLORS["accent"]}; }}
            QComboBox QAbstractItemView {{
                background-color: #121216;
                selection-background-color: #1c1c22;
                color: {COLORS["text_main"]};
            }}
        """)

        self._trade_queue: queue.Queue = queue.Queue()

        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(50)

        self._init_ui()
        engine.register_raw_trade_callback(self._on_raw_trade)

    def _init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()

        sym_lbl = QLabel(self.symbol)
        sym_lbl.setStyleSheet(
            f"color: {COLORS['accent']}; font-size: 14px; font-weight: bold; letter-spacing: 2px;"
        )

        tf_lbl = QLabel("TIMEFRAME")
        tf_lbl.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 11px; font-weight: bold; letter-spacing: 1px;"
        )
        self._tf_combo = QComboBox()
        for tf in TIMEFRAMES:
            self._tf_combo.addItem(tf)
        self._tf_combo.setFixedWidth(80)
        self._tf_combo.currentTextChanged.connect(self._on_tf_changed)

        hint = QLabel("hijau = buy dominan  |  merah = sell dominan  |  angka: buy×sell per level harga")
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")

        top.addWidget(sym_lbl)
        top.addSpacing(16)
        top.addWidget(hint)
        top.addStretch()
        top.addWidget(tf_lbl)
        top.addSpacing(6)
        top.addWidget(self._tf_combo)
        layout.addLayout(top)

        self.chart = FootprintChart(self)
        layout.addWidget(self.chart)

    def _on_tf_changed(self, tf: str):
        self.chart.set_timeframe(tf)

    def _on_raw_trade(self, symbol: str, price: float, size: float, is_buyer_maker: bool):
        # Dipanggil dari background thread — push ke queue
        if symbol == self.symbol:
            try:
                self._trade_queue.put_nowait((price, size, is_buyer_maker))
            except queue.Full:
                pass

    def _drain(self):
        # Dipanggil dari main thread setiap 50ms
        drained = 0
        while drained < 500:
            try:
                price, size, ibm = self._trade_queue.get_nowait()
                self.chart.add_trade(price, size, ibm)
                drained += 1
            except queue.Empty:
                break

    def closeEvent(self, event):
        self._drain_timer.stop()
        self._engine.unregister_raw_trade_callback(self._on_raw_trade)
        event.accept()
