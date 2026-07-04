from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from pulseflow.ui.styles import COLORS

class SymbolScanner(QFrame):
    """
    Scans, updates, and ranks multiple crypto assets
    based on real-time trade velocity and aggression.

    Mode SIMPLE (default dashboard): hanya kolom SYMBOL · PRICE · SIGNAL —
    kolom teknikal (velocity/aggression/regime) disembunyikan supaya scanner
    langsung menjawab "simbol mana yang ada sinyal entry".
    """
    # Signal emitted when a symbol row is clicked, allowing focus shifts
    symbol_selected = pyqtSignal(str)

    COL_SYMBOL, COL_PRICE, COL_SIGNAL, COL_VEL, COL_AGG, COL_REGIME = range(6)
    _TECH_COLS = (COL_VEL, COL_AGG, COL_REGIME)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.title = QLabel("REALTIME SYMBOL SCANNER (SORTED)", self)
        self.title.setObjectName("TitleLabel")
        layout.addWidget(self.title)

        # Configure Table
        self.table = QTableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["SYMBOL", "PRICE", "SIGNAL", "VELOCITY", "AGGRESSION", "REGIME"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        # Header configurations
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self.table)

        self.symbol_to_row = {}

    def set_simple_mode(self, simple: bool):
        """SIMPLE: sembunyikan kolom teknikal, sisakan SYMBOL·PRICE·SIGNAL."""
        for col in self._TECH_COLS:
            self.table.setColumnHidden(col, simple)

    def _on_cell_clicked(self, row, column):
        symbol_item = self.table.item(row, 0)
        if symbol_item:
            self.symbol_selected.emit(symbol_item.text())

    @staticmethod
    def _signal_text_color(metrics: dict):
        """Ringkas status EntrySignalEngine untuk satu sel: teks + warna."""
        entry = metrics.get("entry") or {}
        phase = entry.get("phase", "WAIT")
        side = entry.get("side")
        score = entry.get("score", 0)
        if phase == "ACTIVE" and side:
            icon = "🟢" if side == "LONG" else "🔴"
            col = COLORS["green_glow"] if side == "LONG" else COLORS["red_glow"]
            return f"{icon} {side} {score}", QColor(col)
        if phase == "FORMING" and side:
            return f"◔ {side} {score}", QColor(COLORS["orange_alert"])
        return "—", QColor(COLORS["text_muted"])

    def update_symbol_metrics(self, symbol: str, price: float, metrics: dict):
        """Creates or updates an asset's row inside the scanning table."""
        agg_score = metrics.get("aggression_score", 30.0)
        regime = metrics.get("regime", "normal")
        inst = metrics.get("instantaneous", {})
        trade_vel = inst.get("trade_velocity", 0.0)
        sig_text, sig_color = self._signal_text_color(metrics)

        if symbol not in self.symbol_to_row:
            row_idx = self.table.rowCount()
            self.table.insertRow(row_idx)

            # Create cells
            for col, text in ((self.COL_SYMBOL, symbol),
                              (self.COL_PRICE, f"${price:.2f}"),
                              (self.COL_SIGNAL, sig_text),
                              (self.COL_VEL, f"{trade_vel:.1f}/s"),
                              (self.COL_AGG, f"{agg_score:.1f}"),
                              (self.COL_REGIME, regime.upper())):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col, item)
            self.table.item(row_idx, self.COL_SIGNAL).setForeground(sig_color)

            self.symbol_to_row[symbol] = row_idx
        else:
            row_idx = self.symbol_to_row[symbol]

            self.table.item(row_idx, self.COL_PRICE).setText(f"${price:.2f}")
            self.table.item(row_idx, self.COL_VEL).setText(f"{trade_vel:.1f}/s")
            self.table.item(row_idx, self.COL_AGG).setText(f"{agg_score:.1f}")

            sig_item = self.table.item(row_idx, self.COL_SIGNAL)
            sig_item.setText(sig_text)
            sig_item.setForeground(sig_color)

            regime_item = self.table.item(row_idx, self.COL_REGIME)
            regime_item.setText(regime.upper())

            # Highlight extreme aggression regimes with alert colors
            if regime == "extreme":
                regime_item.setForeground(Qt.GlobalColor.red)
            elif regime == "aggressive":
                regime_item.setForeground(Qt.GlobalColor.yellow)
            elif regime == "active":
                regime_item.setForeground(Qt.GlobalColor.green)
            else:
                regime_item.setForeground(Qt.GlobalColor.white)

        # Trigger auto sorting based on Aggression Score desc
        # Disconnect signal briefly to avoid side-effects
        self.table.sortItems(self.COL_AGG, Qt.SortOrder.DescendingOrder)

        # Re-build mapping because sorting changes row indices
        self.symbol_to_row.clear()
        for r in range(self.table.rowCount()):
            sym = self.table.item(r, 0).text()
            self.symbol_to_row[sym] = r
