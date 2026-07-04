from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QLinearGradient
from PyQt6.QtCore import Qt, QRectF
from pulseflow.ui.styles import COLORS


class FrontlineBar(QWidget):
    """
    A single 'tug of war' bar that collapses delta, aggression, velocity and
    flow into ONE glanceable verdict:

        SELLERS ◄──────⚔────► BUYERS

    The ⚔ marker position maps the battle frontline (-100 seller .. +100
    buyer). The winning side's half lights up; the loser's half dims.
    Driven by `battle["frontline"]` + `battle["dominant"]`.
    """

    GREEN = QColor(16, 185, 129)
    RED   = QColor(244, 63, 94)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(46)
        self.setMaximumHeight(54)
        self._frontline = 0.0          # smoothed display value (-100..100)
        self._target = 0.0
        self._dominant = "NEUTRAL"
        self._state = ""

    def set_battle(self, battle: dict | None):
        if not battle:
            return
        self._target = max(-100.0, min(100.0, float(battle.get("frontline", 0.0))))
        self._dominant = battle.get("dominant", "NEUTRAL")
        self._state = battle.get("state", "")
        # Ease toward target for a smooth slide rather than jumps
        self._frontline += 0.35 * (self._target - self._frontline)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        # Geometry: leave room for side labels
        pad_l, pad_r = 78, 78
        track_x = pad_l
        track_w = max(10, w - pad_l - pad_r)
        track_y = h / 2 - 5
        track_h = 10
        center = track_x + track_w / 2

        frac = self._frontline / 100.0          # -1..1
        marker_x = center + frac * (track_w / 2)

        buyers_win = self._frontline > 6
        sellers_win = self._frontline < -6

        # ── Track background ─────────────────────────────────────────────
        bg = QColor(20, 20, 26)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(track_x, track_y, track_w, track_h), 5, 5)

        # ── Filled territory from center to marker ───────────────────────
        if abs(self._frontline) > 0.5:
            if frac >= 0:
                grad = QLinearGradient(center, 0, marker_x, 0)
                col = QColor(self.GREEN)
                col.setAlpha(70 if not buyers_win else 200)
                grad.setColorAt(0.0, QColor(self.GREEN.red(), self.GREEN.green(), self.GREEN.blue(), 30))
                grad.setColorAt(1.0, col)
                p.setBrush(grad)
                p.drawRoundedRect(QRectF(center, track_y, marker_x - center, track_h), 5, 5)
            else:
                grad = QLinearGradient(center, 0, marker_x, 0)
                col = QColor(self.RED)
                col.setAlpha(70 if not sellers_win else 200)
                grad.setColorAt(0.0, QColor(self.RED.red(), self.RED.green(), self.RED.blue(), 30))
                grad.setColorAt(1.0, col)
                p.setBrush(grad)
                p.drawRoundedRect(QRectF(marker_x, track_y, center - marker_x, track_h), 5, 5)

        # ── Center tick ──────────────────────────────────────────────────
        p.setPen(QPen(QColor(60, 60, 72), 1))
        p.drawLine(int(center), int(track_y - 4), int(center), int(track_y + track_h + 4))

        # ── Marker (⚔) ───────────────────────────────────────────────────
        marker_col = self.GREEN if buyers_win else self.RED if sellers_win else QColor(180, 180, 190)
        # Glow halo
        halo = QColor(marker_col)
        halo.setAlpha(60)
        p.setBrush(halo)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(marker_x - 16, h / 2 - 16, 32, 32))
        # Core knob
        p.setBrush(marker_col)
        p.drawEllipse(QRectF(marker_x - 9, h / 2 - 9, 18, 18))
        p.setFont(QFont("Segoe UI Symbol", 10, QFont.Weight.Bold))
        p.setPen(QPen(QColor(10, 10, 12)))
        p.drawText(QRectF(marker_x - 12, h / 2 - 11, 24, 22),
                   Qt.AlignmentFlag.AlignCenter, "⚔")

        # ── Side labels ──────────────────────────────────────────────────
        sellers_col = self.RED if sellers_win else QColor(120, 120, 132)
        buyers_col  = self.GREEN if buyers_win else QColor(120, 120, 132)

        p.setFont(QFont("Outfit", 10, QFont.Weight.Black))
        p.setPen(QPen(sellers_col))
        p.drawText(QRectF(4, 0, pad_l - 10, h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "SELLERS")
        p.setPen(QPen(buyers_col))
        p.drawText(QRectF(w - pad_r + 6, 0, pad_r - 10, h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "BUYERS")
        p.end()
