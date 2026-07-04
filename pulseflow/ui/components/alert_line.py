import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal


class AlertLine(pg.InfiniteLine):
    """
    A draggable horizontal price-alert line.

    • Drag vertically to move the alert level (label tracks the price live).
    • Right-click or double-click to remove it (emits `sigRemove`).
    • `_cooldown_until` throttles repeated triggers when price oscillates.
    """
    sigRemove = pyqtSignal(object)

    def __init__(self, price: float, color=(245, 158, 11)):
        super().__init__(
            pos=price, angle=0, movable=True,
            pen=pg.mkPen(color[0], color[1], color[2], 220, width=1,
                         style=Qt.PenStyle.DashLine),
            hoverPen=pg.mkPen(color[0], color[1], color[2], 255, width=2),
            label="🔔 {value:.6g}",   # pyqtgraph auto-updates this as the line moves
            labelOpts={"position": 0.04, "color": color,
                       "fill": (20, 20, 26, 200), "movable": False},
        )
        self.setZValue(80)
        self._cooldown_until = 0.0

    def mouseClickEvent(self, ev):
        # Right-click or double-click removes the alert
        if ev.button() == Qt.MouseButton.RightButton or ev.double():
            ev.accept()
            self.sigRemove.emit(self)
            return
        super().mouseClickEvent(ev)
