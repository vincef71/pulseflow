import time
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton)
from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt
from pulseflow.ui.styles import COLORS


class AlertLogWindow(QDialog):
    """
    Non-modal window logging every price-alert trigger (time, symbol, level,
    cross direction, price). Auto-raised when a new alert fires.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔔 Alert Log")
        self.setModal(False)
        self.resize(420, 320)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {COLORS['bg_dark']}; }}
            QLabel  {{ color: {COLORS['text_main']}; font-weight: bold; }}
        """)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("PRICE ALERT LOG", self)
        title.setStyleSheet(f"color: {COLORS['accent']}; font-size: 13px; letter-spacing: 1px;")
        header.addWidget(title)
        header.addStretch(1)
        self.clear_btn = QPushButton("Clear", self)
        self.clear_btn.clicked.connect(self.clear_log)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self.list_widget = QListWidget(self)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: 1px solid #23232a; border-radius: 6px;
                font-family: 'Consolas', 'Courier New', monospace; font-size: 13px;
            }}
            QListWidget::item {{ padding: 5px 6px; border-bottom: 1px solid #1a1a24; }}
        """)
        layout.addWidget(self.list_widget)

    def log(self, symbol: str, level: float, direction: str, price: float):
        ts = time.strftime("%H:%M:%S")
        arrow = "▲" if direction == "up" else "▼"
        color = COLORS["green_glow"] if direction == "up" else COLORS["red_glow"]
        text = f"[{ts}]  🔔 {symbol}  crossed {level:.6g} {arrow}  (px {price:.6g})"
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        self.list_widget.insertItem(0, item)
        while self.list_widget.count() > 200:
            self.list_widget.takeItem(self.list_widget.count() - 1)

    def clear_log(self):
        self.list_widget.clear()

    def show_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()
