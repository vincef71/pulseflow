from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QListWidget, QListWidgetItem
from PyQt6.QtCore import Qt
import time
from pulseflow.ui.styles import COLORS

class LiquidationTape(QFrame):
    """
    Real-time scrolling ticker showcasing liquidation events and 
    outsized institutional force-orders.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        self.title = QLabel("LIQUIDATION & BLOCK TAPE", self)
        self.title.setObjectName("TitleLabel")
        layout.addWidget(self.title)
        
        self.list_widget = QListWidget(self)
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: none;
                font-family: monospace;
                font-size: 12px;
            }}
        """)
        layout.addWidget(self.list_widget)

    def add_liquidation(self, symbol: str, usd_value: float, side: str):
        """Append a new liquidation row onto the tape."""
        timestamp = time.strftime("%H:%M:%S")
        side_text = "SHORT LIQ" if side == "BUY" else "LONG LIQ" # buying liquidation means short forced covers, selling means long liquidated
        color = COLORS["purple_liq"] if side == "BUY" else COLORS["orange_alert"]
        
        row_text = f"[{timestamp}]  {symbol:<5}  {side_text:<9}  ${usd_value:,.0f}"
        
        item = QListWidgetItem(row_text)
        item.setForeground(Qt.GlobalColor.magenta if side == "BUY" else Qt.GlobalColor.yellow)
        
        # Insert at the top (newest first)
        self.list_widget.insertItem(0, item)
        
        # Cap size to 100 entries to maintain low memory profile
        if self.list_widget.count() > 100:
            self.list_widget.takeItem(self.list_widget.count() - 1)
