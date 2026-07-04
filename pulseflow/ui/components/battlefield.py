"""
Battlefield Panel
=================

Visual renderer untuk Battle State Engine. Menggambarkan pertempuran Buyers vs
Sellers dalam satu pandang:

  Layer 1  — Army Strength bars (SELLERS atas, BUYERS bawah)
  Layer 2  — Frontline Meter (fitur inti: SELLER ──⚔── BUYER)
  Layer 3  — Liquidity Fortress (tembok benteng yang terkikis)
  Layer 4  — Special Events (🐋 whale, ☠ liquidation) yang muncul lalu memudar
  Layer 5  — Battle Narrative (menggantikan log mentah dengan cerita pertempuran)

Nilai dari engine di-ease pada 30 fps agar gerakan mulus (Sprint 3 — animasi).
"""

import time
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QListWidget,
    QListWidgetItem, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QColor, QLinearGradient, QBrush, QPen, QFont, QPainterPath,
)
from pulseflow.ui.styles import COLORS

RED   = QColor("#f43f5e")
GREEN = QColor("#10b981")
MUTED = QColor("#7d7d8e")


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _fmt_usd(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:.0f}"


class BattlefieldCanvas(QWidget):
    """Widget ber-paint kustom yang menggambar medan perang."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Nilai target (dari engine) dan nilai tampil (di-ease)
        self._t = {  # target
            "front": 0.0, "buyer": 0.0, "seller": 0.0,
            "buy_wall": 0.0, "sell_wall": 0.0,
            "buyer_mom": 0.0, "seller_mom": 0.0,
        }
        self._d = dict(self._t)  # displayed
        self.state_color = QColor(MUTED)
        self.dominant = "NEUTRAL"
        self.events = []
        self._anim_phase = 0.0

        # Loop animasi ~30 fps
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(33)

    def set_state(self, battle: dict):
        if not battle:
            return
        b = battle.get("buyer", {})
        s = battle.get("seller", {})
        f = battle.get("fortress", {})
        self._t.update({
            "front":  float(battle.get("frontline", 0.0)),
            "buyer":  float(b.get("strength", 0.0)),
            "seller": float(s.get("strength", 0.0)),
            "buy_wall":  float(f.get("buy_wall", 0.0)),
            "sell_wall": float(f.get("sell_wall", 0.0)),
            "buyer_mom":  float(b.get("momentum", 0.0)),
            "seller_mom": float(s.get("momentum", 0.0)),
        })
        self.state_color = QColor(battle.get("state_color", "#7d7d8e"))
        self.dominant = battle.get("dominant", "NEUTRAL")
        self.events = battle.get("events", [])

    def _animate(self):
        for k in self._t:
            self._d[k] = _lerp(self._d[k], self._t[k], 0.25)
        self._anim_phase = (self._anim_phase + 0.08) % 1000.0
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        pad = 14
        x0 = pad
        x1 = w - pad
        track_w = x1 - x0

        # Alokasi vertikal: sellers / frontline / buyers
        top = 8
        h_army = (h - 2 * top) * 0.24
        h_front = (h - 2 * top) * 0.46
        gap = (h - 2 * top - 2 * h_army - h_front) / 2.0

        y_sell = top
        y_front = y_sell + h_army + gap
        y_buy = y_front + h_front + gap

        self._draw_army(p, "SELLERS", self._d["seller"], self._d["seller_mom"],
                        x0, y_sell, track_w, h_army, RED, align_right=False)
        self._draw_frontline(p, x0, y_front, track_w, h_front)
        self._draw_army(p, "BUYERS", self._d["buyer"], self._d["buyer_mom"],
                        x0, y_buy, track_w, h_army, GREEN, align_right=False)
        self._draw_events(p, x0, y_front, track_w, h_front)
        p.end()

    def _draw_army(self, p, name, strength, momentum, x, y, w, h,
                   color: QColor, align_right):
        # Track
        track = QRectF(x, y + h * 0.45, w, h * 0.42)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#15151c"))
        p.drawRoundedRect(track, 5, 5)

        # Fill
        fill_w = max(0.0, min(1.0, strength / 100.0)) * w
        if fill_w > 1:
            grad = QLinearGradient(x, 0, x + fill_w, 0)
            c0 = QColor(color); c0.setAlpha(70)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, color)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, y + h * 0.45, fill_w, h * 0.42), 5, 5)

        # Label baris
        p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        p.setPen(QPen(QColor(color)))
        p.drawText(QRectF(x, y, w * 0.6, h * 0.42),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setPen(QPen(QColor("#e3e3e7")))
        p.drawText(QRectF(x, y, w, h * 0.42),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{strength:4.0f}%")

        # Chevron momentum bergerak (>>> menyerang / <<< mundur)
        if abs(momentum) > 6:
            advancing = momentum > 0
            ch = "›"
            p.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
            cc = QColor(color); cc.setAlpha(200)
            p.setPen(QPen(cc))
            base = x + fill_w
            for i in range(3):
                off = (self._anim_phase * 6 + i * 9) % 27
                cx = base + (off if advancing else -off) + 4
                p.drawText(QPointF(cx, y + h * 0.45 + h * 0.30), ch if advancing else "‹")

    def _draw_frontline(self, p, x, y, w, h):
        cx = x + w / 2.0
        track_top = y + h * 0.30
        track_h = h * 0.40
        track = QRectF(x, track_top, w, track_h)

        # Gradient teritori: merah (seller) → gelap → hijau (buyer)
        grad = QLinearGradient(x, 0, x + w, 0)
        cr = QColor(RED); cr.setAlpha(120)
        cg = QColor(GREEN); cg.setAlpha(120)
        grad.setColorAt(0.0, cr)
        grad.setColorAt(0.42, QColor("#141420"))
        grad.setColorAt(0.58, QColor("#141420"))
        grad.setColorAt(1.0, cg)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(track, 6, 6)

        # Garis tengah netral
        p.setPen(QPen(QColor("#33333f"), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(cx, track_top), QPointF(cx, track_top + track_h))

        # Tick -50 / +50
        for frac in (0.25, 0.75):
            tx = x + w * frac
            p.setPen(QPen(QColor("#262630"), 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(tx, track_top), QPointF(tx, track_top + track_h))

        # Teritori yang dikuasai: arsir dari tengah ke marker
        front = self._d["front"]
        half = w / 2.0
        mx = cx + (front / 100.0) * half
        terr_color = GREEN if front >= 0 else RED
        tc = QColor(terr_color); tc.setAlpha(60)
        p.setBrush(QBrush(tc))
        p.setPen(Qt.PenStyle.NoPen)
        lft = min(cx, mx)
        p.drawRect(QRectF(lft, track_top, abs(mx - cx), track_h))

        # Benteng (Layer 3): tembok terkikis di kedua ujung
        self._draw_fortress(p, x, track_top, track_h, self._d["buy_wall"], side="left")
        self._draw_fortress(p, x + w, track_top, track_h, self._d["sell_wall"], side="right")

        # Marker frontline ⚔ (fitur inti)
        glow = QColor(terr_color); glow.setAlpha(60)
        p.setPen(QPen(glow, 7))
        p.drawLine(QPointF(mx, track_top - 4), QPointF(mx, track_top + track_h + 4))
        p.setPen(QPen(QColor(terr_color), 2.5))
        p.drawLine(QPointF(mx, track_top - 4), QPointF(mx, track_top + track_h + 4))

        # Pointer segitiga + simbol pedang
        path = QPainterPath()
        path.moveTo(mx, track_top - 6)
        path.lineTo(mx - 6, track_top - 16)
        path.lineTo(mx + 6, track_top - 16)
        path.closeSubpath()
        p.setBrush(QColor(terr_color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

        p.setFont(QFont("Segoe UI Emoji", 13))
        p.setPen(QPen(QColor("#e3e3e7")))
        p.drawText(QRectF(mx - 12, track_top + track_h + 4, 24, 18),
                   Qt.AlignmentFlag.AlignCenter, "⚔")

        # Label ujung
        p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        p.setPen(QPen(QColor(RED)))
        p.drawText(QRectF(x, y, w * 0.4, h * 0.28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "◄ SELLER")
        p.setPen(QPen(QColor(GREEN)))
        p.drawText(QRectF(x + w * 0.6, y, w * 0.4, h * 0.28),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "BUYER ►")

        # Nilai numerik frontline di tengah atas
        p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        p.setPen(QPen(QColor("#9a9aa8")))
        sign = "+" if front >= 0 else ""
        p.drawText(QRectF(cx - 60, y, 120, h * 0.28),
                   Qt.AlignmentFlag.AlignCenter, f"FRONTLINE {sign}{front:.0f}")

    def _draw_fortress(self, p, x_edge, top, height, wall, side):
        """Tembok benteng sebagai tumpukan bata; makin terkikis makin pendek."""
        if wall < 4:
            return
        n_bricks = 6
        active = wall / 100.0 * n_bricks
        bw = 9.0
        bh = height / n_bricks
        color = QColor(RED) if side == "right" else QColor(GREEN)
        for i in range(n_bricks):
            filled = (i + 1) <= active
            partial = (not filled) and (i < active)
            if not filled and not partial:
                continue
            alpha = 200 if filled else 90
            c = QColor(color); c.setAlpha(alpha)
            p.setBrush(QBrush(c))
            p.setPen(QPen(QColor("#0b0b0d"), 1))
            yy = top + height - (i + 1) * bh
            xx = (x_edge - bw - 1) if side == "right" else (x_edge + 1)
            # ofset bata seperti tembok (crenellation)
            inset = 2 if i % 2 == 0 else 0
            p.drawRect(QRectF(xx + (inset if side == "right" else -inset),
                              yy + 1, bw, bh - 2))

    def _draw_events(self, p, x, y, w, h):
        if not self.events:
            return
        now = time.time()
        cx = x + w / 2.0
        for ev in self.events:
            age = now - ev.get("ts", now)
            ttl = 3.0
            if age > ttl:
                continue
            life = 1.0 - age / ttl
            alpha = int(_lerp(0, 255, min(1.0, life * 1.6)))
            rise = (1.0 - life) * 26.0
            side = ev.get("side", "")
            if ev.get("kind") == "WHALE":
                col = GREEN if side == "BUY" else RED
            else:
                col = QColor("#d946ef")  # liquidation ungu
            c = QColor(col); c.setAlpha(alpha)
            p.setFont(QFont("Segoe UI Emoji", 13, QFont.Weight.Bold))
            p.setPen(QPen(c))
            txt = f"{ev.get('icon','')} {ev.get('label','')}  {_fmt_usd(ev.get('usd',0))}"
            yy = y + h * 0.10 - rise
            p.drawText(QRectF(cx - w * 0.45, yy, w * 0.9, 22),
                       Qt.AlignmentFlag.AlignCenter, txt)


class BattlefieldPanel(QFrame):
    """Panel utama: header + canvas medan perang + log narasi."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._symbol = ""
        self._last_headline = None
        self._seen_event_ts = set()
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Header: judul + badge state + narasi ringkas
        header = QHBoxLayout()
        self.title = QLabel("BATTLEFIELD", self)
        self.title.setObjectName("TitleLabel")
        header.addWidget(self.title)
        header.addStretch()

        self.state_badge = QLabel("⬜ CEASEFIRE", self)
        self.state_badge.setStyleSheet(
            "font-size: 13px; font-weight: 900; color: #7d7d8e; "
            "padding: 2px 10px; border: 1px solid #2d2d38; border-radius: 10px;")
        header.addWidget(self.state_badge)
        layout.addLayout(header)

        # Headline live
        self.headline = QLabel("MARKET QUIET", self)
        self.headline.setStyleSheet("font-size: 18px; font-weight: 900; color: #7d7d8e;")
        layout.addWidget(self.headline)

        self.subline = QLabel("Pressure: —   Momentum: —   Whale: —", self)
        self.subline.setStyleSheet(f"font-size: 12px; color: {COLORS['text_muted']};")
        layout.addWidget(self.subline)

        # Canvas medan perang
        self.canvas = BattlefieldCanvas(self)
        layout.addWidget(self.canvas, stretch=1)

        # Log narasi (Layer 5)
        nav_lbl = QLabel("BATTLE NARRATIVE", self)
        nav_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {COLORS['text_muted']}; "
            "letter-spacing: 1px; margin-top: 2px;")
        layout.addWidget(nav_lbl)

        self.log = QListWidget(self)
        self.log.setMaximumHeight(150)
        self.log.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['bg_panel']};
                border: 1px solid #1a1a24;
                border-radius: 6px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
            }}
            QListWidget::item {{
                padding: 2px 6px;
                border-bottom: 1px solid #141420;
            }}
        """)
        layout.addWidget(self.log)

    def set_symbol(self, symbol: str):
        self._symbol = symbol
        self.title.setText(f"BATTLEFIELD — {symbol}")
        self._last_headline = None
        self.log.clear()
        self._seen_event_ts.clear()

    def update_battle(self, battle: dict):
        if not battle:
            return
        self.canvas.set_state(battle)

        nar = battle.get("narrative", {})
        color = nar.get("color", "#7d7d8e")
        headline = nar.get("headline", "—")
        emoji = battle.get("state_emoji", "")
        state = battle.get("state", "")

        self.state_badge.setText(f"{emoji} {state}")
        self.state_badge.setStyleSheet(
            f"font-size: 13px; font-weight: 900; color: {color}; "
            f"padding: 2px 10px; border: 1px solid {color}; border-radius: 10px;")

        self.headline.setText(headline)
        self.headline.setStyleSheet(f"font-size: 18px; font-weight: 900; color: {color};")
        self.subline.setText(
            f"Pressure: {nar.get('pressure','—')}   "
            f"Momentum: {nar.get('momentum','—')}   "
            f"Whale: {nar.get('whale_support','—')}   "
            f"→ {nar.get('target','—')}")

        # Catat ke log saat headline berubah (transisi fase pertempuran)
        if headline != self._last_headline:
            self._last_headline = headline
            ts = time.strftime("%H:%M:%S")
            line = (f"[{ts}]  {emoji} {headline}\n"
                    f"        Pressure: {nar.get('pressure','—')}  |  "
                    f"Momentum: {nar.get('momentum','—')}  |  "
                    f"Whale: {nar.get('whale_support','—')}\n"
                    f"        Target: {nar.get('target','—')}")
            self._add_log(line, color)

        # Catat event spesial (whale / liquidation) ke log sekali per event
        for ev in battle.get("events", []):
            ts_key = (ev.get("kind"), round(ev.get("ts", 0), 2))
            if ts_key in self._seen_event_ts:
                continue
            self._seen_event_ts.add(ts_key)
            tstr = time.strftime("%H:%M:%S")
            side = ev.get("side", "")
            ecol = ("#10b981" if side == "BUY" else "#f43f5e") if ev.get("kind") == "WHALE" else "#d946ef"
            self._add_log(
                f"[{tstr}]  {ev.get('icon','')} {ev.get('label','')}  {_fmt_usd(ev.get('usd',0))}",
                ecol)

        # Prune set agar tidak tumbuh tak terbatas
        if len(self._seen_event_ts) > 200:
            self._seen_event_ts = set(list(self._seen_event_ts)[-100:])

    def _add_log(self, text: str, color: str):
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        self.log.insertItem(0, item)
        while self.log.count() > 60:
            self.log.takeItem(self.log.count() - 1)
