"""
Liquidity Probability Ladder
============================

Memvisualkan output `LiquidityProbabilityEngine`: peta probabilitas likuiditas
yang diprediksi akan menjadi medan pertempuran berikutnya (5–30 s ke depan).

Tata letak = peta spasial vertikal (posisi mengikuti harga, selaras dengan tab
heatmap): tiap level digambar sebagai bar horizontal — panjang ∝ probabilitas,
warna mengikuti sisi (merah = Sell/resistance di atas harga, hijau = Buy/support
di bawah harga) — lengkap dengan harga, %, dan label kekuatan. Garis harga
sekarang memisahkan zona buy/sell.

    155.10  ████████████  88%  ← Strong Sell Liquidity
    154.82  ██████████    81%  ← Strong Buy Liquidity
    154.70  ███           28%  ← Weak Interest
"""

import math
import time
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QSplitter,
)
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtCore import Qt, QRectF
from pulseflow.ui.styles import COLORS

_GREEN = (16, 185, 129)
_RED   = (244, 63, 94)


def _ema(prev: float, new: float, alpha: float) -> float:
    return prev + alpha * (new - prev)


def _fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _price_decimals(price: float) -> int:
    if price <= 0:
        return 2
    return max(2, 5 - int(math.floor(math.log10(price))))


class _MicroReadWidget(QWidget):
    """
    Tampilan micro yang sudah DIOLAH & STABIL (bukan spatial ladder cepat yang
    cuma berguna untuk HFT). Micro engine tetap jalan di background; di sini level
    di-*debounce* (hanya yang persisten muncul), nilai di-EMA, dan repaint
    di-throttle ~1.5 s sehingga bisa dibaca trader.

    Tiap baris: harga · sisi · bar+prob · STATE (HOLDING/CAPPING/BUILDING/FADING/
    SHIFTING) · jumlah tes · 🔥 pressure.
    """

    CONFIRM = 14            # tick persisten sebelum sebuah level ditampilkan (~1.4s)
    DROP    = 28            # tick menghilang sebelum dibuang (~2.8s)
    REFRESH = 1.5           # detik antar repaint (stabil & terbaca)
    PRESS_HOT = 50.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self._tracked: list = []     # dict per level terlacak
        self._price = 0.0
        self._bias = "NEUTRAL"
        self._last_render = 0.0

    def reset(self):
        self._tracked = []
        self._price = 0.0
        self._bias = "NEUTRAL"
        self.update()

    def set_data(self, levels, price, bias="NEUTRAL"):
        """Dipanggil tiap tick (10 Hz): update model debounce; repaint di-throttle."""
        self._price = price or 0.0
        self._bias = bias or "NEUTRAL"
        if price and price > 0:
            self._ingest(levels or [], price)
        now = time.time()
        if now - self._last_render >= self.REFRESH:
            self._last_render = now
            self.update()

    def _ingest(self, levels, price):
        band = max(price * 0.0008, 1e-9)
        for t in self._tracked:
            t["_matched"] = False
        for L in levels:
            lp = float(L["price"])
            best, bd = None, band
            for t in self._tracked:
                d = abs(t["price"] - lp)
                if d <= bd:
                    best, bd = t, d
            prob = float(L.get("prob", 0.0))
            if best is None:
                self._tracked.append({
                    "price": lp, "side": L.get("side", "BUY"),
                    "prob": prob, "prob_slow": prob,
                    "pressure": float(L.get("pressure", 0.0)),
                    "mig": float(L.get("migration", 0.0)),
                    "touch": int(L.get("touch", 0)),
                    "seen": 1, "missing": 0, "_matched": True,
                })
            else:
                best["price"] = _ema(best["price"], lp, 0.3)
                best["side"] = L.get("side", best["side"])
                best["prob"] = _ema(best["prob"], prob, 0.3)
                best["prob_slow"] = _ema(best["prob_slow"], prob, 0.06)
                best["pressure"] = _ema(best["pressure"], float(L.get("pressure", 0.0)), 0.3)
                best["mig"] = _ema(best["mig"], float(L.get("migration", 0.0)), 0.3)
                best["touch"] = max(best["touch"], int(L.get("touch", 0)))
                best["seen"] += 1
                best["missing"] = 0
                best["_matched"] = True
        for t in self._tracked:
            if not t["_matched"]:
                t["missing"] += 1
        self._tracked = [t for t in self._tracked if t["missing"] < self.DROP]

    def _state(self, t) -> str:
        side = t["side"]
        trend = t["prob"] - t["prob_slow"]
        if abs(t["mig"]) >= 0.4:
            return "SHIFTING " + ("↑" if t["mig"] > 0 else "↓")
        if t["pressure"] >= self.PRESS_HOT:
            return "HOLDING" if side == "BUY" else "CAPPING"
        if trend >= 3:
            return "BUILDING ↑"
        if trend <= -3:
            return "FADING ↓"
        return "SUPPORT" if side == "BUY" else "RESISTANCE"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(COLORS["bg_panel"]))

        # header: judul + verdict
        verdict = {"UP": "Buyers in control", "DOWN": "Sellers in control"}.get(
            self._bias, "Balanced / chop")
        vcol = _GREEN if self._bias == "UP" else _RED if self._bias == "DOWN" else (150, 150, 160)
        p.setFont(QFont("Outfit", 9, QFont.Weight.Black))
        p.setPen(QPen(QColor(COLORS["accent"])))
        p.drawText(QRectF(8, 4, w - 16, 16), Qt.AlignmentFlag.AlignLeft, "⚙ MICRO READ")
        p.setPen(QPen(QColor(*vcol)))
        p.drawText(QRectF(8, 4, w - 16, 16), Qt.AlignmentFlag.AlignRight, verdict)
        p.setPen(QPen(QColor(40, 40, 50)))
        p.drawLine(8, 22, w - 8, 22)

        confirmed = [t for t in self._tracked if t["seen"] >= self.CONFIRM]
        if not confirmed or self._price <= 0:
            p.setPen(QPen(QColor(120, 120, 132)))
            p.setFont(QFont("Outfit", 10))
            p.drawText(QRectF(0, 24, w, h - 24), Qt.AlignmentFlag.AlignCenter,
                       "Menunggu level micro yang stabil…")
            p.end()
            return

        confirmed.sort(key=lambda t: t["price"], reverse=True)   # sell di atas
        confirmed = confirmed[:7]
        dec = _price_decimals(self._price)
        y0 = 28
        row_h = max(22, min(34, (h - y0 - 6) / max(1, len(confirmed))))
        price_w = 76
        bar_x = price_w + 6
        bar_max = max(36, w * 0.20)

        for i, t in enumerate(confirmed):
            y = y0 + i * row_h + row_h / 2
            sell = t["side"] == "SELL"
            r, g, b = (_RED if sell else _GREEN)
            prob = t["prob"]
            dist = (t["price"] - self._price) / self._price * 100.0

            # harga
            p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            p.setPen(QPen(QColor(225, 225, 230)))
            p.drawText(QRectF(0, y - 9, price_w, 18),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{t['price']:.{dec}f}")

            # sisi
            p.setFont(QFont("Outfit", 8, QFont.Weight.Black))
            p.setPen(QPen(QColor(r, g, b)))
            p.drawText(QRectF(bar_x, y - 9, 34, 18),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       "SELL" if sell else "BUY")

            # bar + prob
            bx = bar_x + 36
            bw = bar_max * (prob / 100.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(r, g, b, int(70 + 165 * prob / 100.0)))
            p.drawRoundedRect(QRectF(bx, y - 7, bw, 14), 3, 3)
            p.setFont(QFont("Outfit", 8, QFont.Weight.Bold))
            p.setPen(QPen(QColor(235, 235, 240)))
            p.drawText(QRectF(bx + 3, y - 8, bar_max, 16),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"{int(prob)}")

            # STATE + tes + jarak + 🔥
            tx = bx + bar_max + 10
            extras = f"{self._state(t)}"
            if t["touch"] > 0:
                extras += f"   ·{t['touch']} tes"
            extras += f"   ({dist:+.2f}%)"
            p.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
            p.setPen(QPen(QColor(r, g, b) if prob >= 55 else QColor(165, 165, 175)))
            p.drawText(QRectF(tx, y - 9, w - tx - 26, 18),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, extras)
            if t["pressure"] >= 40:
                p.setPen(QPen(QColor(255, 150, 40)))
                p.drawText(QRectF(w - 24, y - 9, 20, 18),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "🔥")
        p.end()


class _MacroPoolsWidget(QWidget):
    """Daftar kolam likuiditas BESAR (magnet makro) — bisa jauh dari harga.
    Baris: harga · bar strength · $notional · jarak% · tag sumber."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self._pools = []
        self._price = 0.0

    def set_data(self, pools, price):
        self._pools = pools or []
        self._price = price or 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(COLORS["bg_dark"]))

        # judul section
        p.setFont(QFont("Outfit", 9, QFont.Weight.Black))
        p.setPen(QPen(QColor(COLORS["accent"])))
        p.drawText(QRectF(8, 4, w - 16, 16), Qt.AlignmentFlag.AlignLeft,
                   "🌊 MAJOR LIQUIDITY POOLS")
        if self._price > 0:
            p.setFont(QFont("Consolas", 8))
            p.setPen(QPen(QColor(0, 255, 210)))
            p.drawText(QRectF(8, 4, w - 16, 16), Qt.AlignmentFlag.AlignRight,
                       f"px {self._price:.6g}")

        if not self._pools:
            p.setPen(QPen(QColor(120, 120, 132)))
            p.setFont(QFont("Outfit", 10))
            p.drawText(QRectF(0, 22, w, h - 22), Qt.AlignmentFlag.AlignCenter,
                       "Scanning for large liquidity pools…")
            p.end()
            return

        dec = _price_decimals(self._price)
        y0 = 24
        row_h = max(20, min(30, (h - y0 - 4) / max(1, len(self._pools))))
        price_w = 78
        bar_x = price_w + 6
        bar_max = max(40, w * 0.30)

        for i, P in enumerate(self._pools):
            y = y0 + i * row_h + row_h / 2
            sell = P["side"] == "SELL"
            r, g, b = P.get("color", (_RED if sell else _GREEN))
            strength = float(P["strength"])

            # harga
            p.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            p.setPen(QPen(QColor(225, 225, 230)))
            p.drawText(QRectF(0, y - 9, price_w, 18),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{P['price']:.{dec}f}")

            # bar strength
            bw = bar_max * (strength / 100.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(r, g, b, int(80 + 150 * strength / 100.0)))
            p.drawRoundedRect(QRectF(bar_x, y - 7, bw, 14), 3, 3)
            p.setFont(QFont("Outfit", 8, QFont.Weight.Bold))
            p.setPen(QPen(QColor(235, 235, 240)))
            p.drawText(QRectF(bar_x + 4, y - 8, bar_max, 16),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"{int(strength)}")

            # $notional · jarak% · tipe (kanan)
            dist = P["distance_pct"]
            arrow = "▲" if dist > 0 else "▼"
            txt = f"{_fmt_usd(P['notional'])}   {arrow}{dist:+.1f}%   {P['type']}"
            p.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
            p.setPen(QPen(QColor(r, g, b)))
            p.drawText(QRectF(bar_x + bar_max + 12, y - 9, w - (bar_x + bar_max + 12) - 6, 18),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
        p.end()


class LiquidityLadder(QFrame):
    """Panel tab: header + section pool MAKRO (atas) + ladder MICRO (bawah)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LiquidityLadderPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._symbol = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title = QLabel("LIQUIDITY PROBABILITY", self)
        self.title.setObjectName("TitleLabel")
        header.addWidget(self.title)

        badge = QLabel("PREDICTED", self)
        badge.setStyleSheet(
            "color:#0b0b0d; background:#00ffd2; font-weight:800; font-size:9px;"
            " padding:1px 6px; border-radius:4px;"
        )
        badge.setMaximumHeight(18)
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)

        self.flow_label = QLabel("FLOW —", self)
        self.flow_label.setToolTip("Flow energy: momentum × persistensi arah "
                                   "(apakah market benar-benar menuju ke sana)")
        self.flow_label.setStyleSheet(
            f"color:{COLORS['text_muted']}; font-weight:700; padding:2px 8px;"
        )
        header.addWidget(self.flow_label)

        self.bias_label = QLabel("BIAS —", self)
        self.bias_label.setStyleSheet(
            f"color:{COLORS['text_muted']}; font-weight:700; padding:2px 8px;"
        )
        header.addWidget(self.bias_label)
        layout.addLayout(header)

        # Dua layer: pool MAKRO (atas) + ladder MICRO spasial (bawah). Dipisah
        # karena makro bisa −15% sementara micro ±0.2% → satu sumbu menyembunyikan.
        self.macro = _MacroPoolsWidget(self)
        self.micro = _MicroReadWidget(self)
        split = QSplitter(Qt.Orientation.Vertical, self)
        split.addWidget(self.macro)
        split.addWidget(self.micro)
        split.setSizes([320, 440])
        layout.addWidget(split, 1)

    # ── API ───────────────────────────────────────────────────────────

    def set_symbol(self, symbol: str):
        self.reset(symbol)

    def reset(self, symbol: str = None):
        if symbol is not None:
            self._symbol = symbol
        self.bias_label.setText("BIAS —")
        self.flow_label.setText("FLOW —")
        self.micro.reset()
        self.macro.set_data([], 0.0)

    def update_levels(self, liq: dict | None):
        if not liq:
            return
        levels = liq.get("levels", [])
        bias = liq.get("bias", "NEUTRAL")
        price = float(liq.get("price", 0.0))
        arrow = "▲" if bias == "UP" else "▼" if bias == "DOWN" else "—"
        col = _GREEN if bias == "UP" else _RED if bias == "DOWN" else (125, 125, 137)
        self.bias_label.setText(f"BIAS {arrow} {bias}")
        self.bias_label.setStyleSheet(
            f"color:rgb({col[0]},{col[1]},{col[2]}); font-weight:800; padding:2px 8px;"
        )

        fe = float(liq.get("flow_energy", 0.0))
        farrow = "▲" if fe > 0.12 else "▼" if fe < -0.12 else "—"
        fcol = _GREEN if fe > 0.12 else _RED if fe < -0.12 else (125, 125, 137)
        self.flow_label.setText(f"FLOW {farrow} {fe:+.2f}")
        self.flow_label.setStyleSheet(
            f"color:rgb({fcol[0]},{fcol[1]},{fcol[2]}); font-weight:800; padding:2px 8px;"
        )

        self.micro.set_data(levels, price, bias)

    def update_macro(self, macro: dict | None):
        if not macro:
            return
        self.macro.set_data(macro.get("pools", []), float(macro.get("price", 0.0)))
