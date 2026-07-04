import time
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QListWidget, QListWidgetItem, QSplitter, QAbstractItemView, QHeaderView
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from pulseflow.ui.styles import COLORS

STATUS_COLORS = {
    "CONNECTED":    "#10b981",
    "CONNECTING":   "#f59e0b",
    "RECONNECTING": "#f59e0b",
    "DISCONNECTED": "#f43f5e",
    "ERROR":        "#ff4444",
    "IDLE":         "#7d7d8e",
}

STATUS_ICONS = {
    "CONNECTED":    "●",
    "CONNECTING":   "◐",
    "RECONNECTING": "◑",
    "DISCONNECTED": "○",
    "ERROR":        "✕",
    "IDLE":         "○",
}


class ConnectionLogPanel(QFrame):
    """
    Displays the live connection status of every feed (top table)
    and a scrollable log of all connection lifecycle events (bottom list).

    Layout
    ──────
    ┌─ FEED CONNECTION STATUS ──────────────────────────────┐
    │ SYMBOL │ FEED      │ STATUS    │ TRADES  │ LAST MSG    │
    │ BTC    │ BINANCE   │ ● CONNECT.│ 12,451  │ 0.1s ago    │
    │ ETH    │ BINANCE   │ ● CONNECT.│  8,302  │ 0.2s ago    │
    ├───────────────────────────────────────────────────────┤
    │ [16:38:52] BTC  BINANCE    CONNECTED   Stream active   │
    │ [16:38:50] ETH  BINANCE    CONNECTING  Connecting…     │
    └───────────────────────────────────────────────────────┘
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")

        self._feed_data: dict = {}      # symbol → {feed_name, status, trade_count, last_trade_time}
        self._symbol_to_row: dict = {}  # symbol → table row index

        self._init_ui()

        # Refresh the "LAST MSG" column every second
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_time_column)
        self._timer.start(1000)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title = QLabel("FEED CONNECTION STATUS", self)
        title.setObjectName("TitleLabel")
        layout.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setHandleWidth(4)

        # ── Status table ──────────────────────────────────────────────
        self.table = QTableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["SYMBOL", "FEED", "STATUS", "TRADES", "LAST MSG"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {COLORS['bg_panel']};
                gridline-color: #1e1e2a;
                border: none;
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }}
            QTableWidget::item {{
                padding: 4px 6px;
            }}
            QHeaderView::section {{
                background-color: #17171f;
                color: {COLORS['text_muted']};
                padding: 5px;
                border: none;
                font-weight: bold;
                font-size: 10px;
                letter-spacing: 0.5px;
            }}
        """)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)

        # ── Event log ─────────────────────────────────────────────────
        self.log_list = QListWidget(self)
        self.log_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
            }}
            QListWidget::item {{
                padding: 2px 6px;
                border-bottom: 1px solid #1a1a24;
            }}
        """)
        splitter.addWidget(self.log_list)

        splitter.setSizes([120, 80])
        layout.addWidget(splitter)

    # ── Public API ────────────────────────────────────────────────────

    def on_feed_status(self, symbol: str, feed_name: str, status, message: str):
        """
        Called from the engine when connection state changes.
        Thread-safe: must be invoked via Qt signal from the asyncio thread.
        """
        status_str = status.value if hasattr(status, "value") else str(status)

        if symbol not in self._feed_data:
            self._feed_data[symbol] = {
                "feed_name":       feed_name,
                "status":          status_str,
                "trade_count":     0,
                "last_trade_time": 0.0,
            }
        else:
            self._feed_data[symbol]["status"]    = status_str
            self._feed_data[symbol]["feed_name"] = feed_name

        self._sync_table_row(symbol)
        self._append_log(symbol, feed_name, status_str, message)

    def update_trade_stats(self, symbol: str, trade_count: int, last_trade_time: float):
        """Called every 100 ms from the metric update cycle to keep counters fresh."""
        if symbol not in self._feed_data:
            return
        self._feed_data[symbol]["trade_count"]     = trade_count
        self._feed_data[symbol]["last_trade_time"] = last_trade_time
        self._sync_table_row(symbol)

    # ── Internal helpers ──────────────────────────────────────────────

    def _sync_table_row(self, symbol: str):
        data       = self._feed_data.get(symbol, {})
        status_str = data.get("status", "IDLE")
        feed_name  = data.get("feed_name", "—").upper()
        count      = data.get("trade_count", 0)
        last_time  = data.get("last_trade_time", 0.0)

        last_msg = self._elapsed_str(last_time)
        icon     = STATUS_ICONS.get(status_str, "○")
        color    = QColor(STATUS_COLORS.get(status_str, "#7d7d8e"))

        # Create row if it doesn't exist yet
        if symbol not in self._symbol_to_row:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._symbol_to_row[symbol] = row
            for col in range(5):
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

        row = self._symbol_to_row[symbol]

        self.table.item(row, 0).setText(symbol)
        self.table.item(row, 1).setText(feed_name)

        status_item = self.table.item(row, 2)
        status_item.setText(f"{icon} {status_str}")
        status_item.setForeground(color)

        self.table.item(row, 3).setText(f"{count:,}")
        self._update_last_msg_item(row, last_time)

    def _update_last_msg_item(self, row: int, last_time: float):
        item = self.table.item(row, 4)
        if item is None:
            return
        elapsed = time.time() - last_time if last_time > 0 else None
        item.setText(self._elapsed_str(last_time))

        # Amber warning when connected but stale > 5 s
        status_str = ""
        for sym, r in self._symbol_to_row.items():
            if r == row:
                status_str = self._feed_data.get(sym, {}).get("status", "")
                break

        if elapsed is not None and elapsed > 5.0 and status_str == "CONNECTED":
            item.setForeground(QColor("#f59e0b"))
        else:
            item.setForeground(QColor(COLORS["text_main"]))

    def _refresh_time_column(self):
        for sym, row in self._symbol_to_row.items():
            last_time = self._feed_data.get(sym, {}).get("last_trade_time", 0.0)
            self._update_last_msg_item(row, last_time)

    def _append_log(self, symbol: str, feed_name: str, status_str: str, message: str):
        ts       = time.strftime("%H:%M:%S")
        icon     = STATUS_ICONS.get(status_str, "○")
        feed_lbl = feed_name.upper()[:12]
        text     = f"[{ts}]  {symbol:<8} {feed_lbl:<12} {icon} {status_str:<14}  {message}"

        item = QListWidgetItem(text)
        item.setForeground(QColor(STATUS_COLORS.get(status_str, "#7d7d8e")))

        self.log_list.insertItem(0, item)
        while self.log_list.count() > 200:
            self.log_list.takeItem(self.log_list.count() - 1)

    @staticmethod
    def _elapsed_str(last_time: float) -> str:
        if last_time <= 0:
            return "—"
        elapsed = time.time() - last_time
        if elapsed < 60:
            return f"{elapsed:.1f}s ago"
        return f"{elapsed / 60:.0f}m ago"
