"""
Entry Signal Card — panel verdict entry.

Menjawab SATU pertanyaan yang selama ini harus dirakit manual dari banyak
panel: "boleh entry sekarang atau tidak, di mana, stop di mana, target di mana?"

    🟢 LONG — BREAKOUT                 (verdict besar, berwarna)
    [████████░░]  82   GRADE A         (skor confluence)

    CONFLUENCE 5/6
    ✓ FLOW        ✓ BATTLE
    ✓ WHALE       ✓ STRUKTUR
    ✗ RUANG GERAK ✓ TIMING

    ENTRY   64,120 – 64,180
    STOP    63,890   (-0.42%)
    TP1     64,560   R 1.8   ✓hit
    TP2     65,010   R 3.1

    ⚠ Range harian 92% terpakai

Fed by `metrics["entry"]` (EntrySignalEngine output).
"""

from PyQt6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QProgressBar, QPushButton, QComboBox)
from PyQt6.QtCore import Qt, pyqtSignal
from pulseflow.ui.styles import COLORS

_GREEN  = COLORS["green_glow"]
_RED    = COLORS["red_glow"]
_MUTED  = COLORS["text_muted"]
_AMBER  = COLORS["orange_alert"]
_ACCENT = COLORS["accent"]

# Urutan tampil checklist (2 kolom × 3 baris)
_CHECK_ORDER = ["flow", "battle", "whale", "structure", "room", "timing"]


class EntrySignalCard(QFrame):
    """Panel keputusan entry — konsumsi output EntrySignalEngine."""

    # Snapshot entry saat tombol EKSEKUSI ditekan (phase ACTIVE + plan)
    execute_requested = pyqtSignal(dict)
    # User klik toggle auto-trade; dashboard yang konfirmasi & memfinalkan
    auto_toggle_requested = pyqtSignal(bool)
    # User ganti filter arah entry: "BOTH" | "LONG" | "SHORT" | "AUTO"
    direction_changed = pyqtSignal(str)

    _DIR_OPTIONS = [("SEMUA", "BOTH"), ("LONG only", "LONG"),
                    ("SHORT only", "SHORT"), ("AUTO (bias 4H)", "AUTO"),
                    ("AUTO ketat (4H+1m)", "AUTO_STRICT")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._last_entry: dict | None = None
        self._exec_mode = "PAPER"
        self._init_ui()
        self.reset()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(7)

        title = QLabel("🎯 ENTRY SIGNAL", self)
        title.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {_MUTED}; letter-spacing: 1px;")
        root.addWidget(title)

        # ── Verdict besar ────────────────────────────────────────────
        self.verdict = QLabel("⏳ WAIT", self)
        self.verdict.setStyleSheet(
            f"font-size: 21px; font-weight: 900; color: {_MUTED}; letter-spacing: 0.5px;")
        root.addWidget(self.verdict)

        self.subline = QLabel("Menunggu confluence…", self)
        self.subline.setStyleSheet(f"font-size: 11px; color: {_MUTED};")
        root.addWidget(self.subline)

        # ── Skor ─────────────────────────────────────────────────────
        score_row = QHBoxLayout()
        score_row.setSpacing(10)
        self.score_bar = QProgressBar(self)
        self.score_bar.setRange(0, 100)
        self.score_bar.setValue(0)
        self.score_bar.setTextVisible(False)
        self.score_bar.setFixedHeight(12)
        score_row.addWidget(self.score_bar, 1)

        self.score_lbl = QLabel("0", self)
        self.score_lbl.setStyleSheet(f"font-size: 17px; font-weight: 900; color: {_MUTED};")
        self.score_lbl.setFixedWidth(34)
        self.score_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        score_row.addWidget(self.score_lbl)

        self.grade_lbl = QLabel("—", self)
        self.grade_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 900; color: {_MUTED};"
            "padding: 1px 7px; border: 1px solid #2d2d38; border-radius: 4px;")
        score_row.addWidget(self.grade_lbl)
        root.addLayout(score_row)

        # ── Checklist confluence ─────────────────────────────────────
        self.conf_title = QLabel("CONFLUENCE 0/6", self)
        self.conf_title.setStyleSheet(
            f"font-size: 10px; font-weight: bold; color: {_MUTED}; letter-spacing: 1px;")
        root.addWidget(self.conf_title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        self.check_labels = {}
        for i, key in enumerate(_CHECK_ORDER):
            lbl = QLabel("· —", self)
            lbl.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {_MUTED};")
            grid.addWidget(lbl, i // 2, i % 2)
            self.check_labels[key] = lbl
        root.addLayout(grid)

        # ── Trade plan ───────────────────────────────────────────────
        self.plan_rows = {}
        plan_grid = QGridLayout()
        plan_grid.setHorizontalSpacing(8)
        plan_grid.setVerticalSpacing(2)
        for r, (key, name) in enumerate((("entry", "ENTRY"), ("stop", "STOP"),
                                         ("tp1", "TP1"), ("tp2", "TP2"))):
            k = QLabel(name, self)
            k.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {_MUTED};")
            v = QLabel("—", self)
            v.setStyleSheet("font-size: 12px; font-weight: 900; font-family: monospace;"
                            f"color: {COLORS['text_main']};")
            v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            plan_grid.addWidget(k, r, 0)
            plan_grid.addWidget(v, r, 1)
            self.plan_rows[key] = v
        plan_grid.setColumnStretch(1, 1)
        root.addLayout(plan_grid)

        # ── Filter arah entry + bias 4H ──────────────────────────────
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_lbl = QLabel("ARAH", self)
        dir_lbl.setStyleSheet(
            f"font-size: 10px; font-weight: bold; color: {_MUTED}; letter-spacing: 1px;")
        dir_row.addWidget(dir_lbl)

        self.dir_combo = QComboBox(self)
        for label, _val in self._DIR_OPTIONS:
            self.dir_combo.addItem(label)
        self.dir_combo.setStyleSheet(
            "QComboBox { background: #16161c; color: #e8e8f0; border: 1px solid #2d2d38;"
            " border-radius: 4px; padding: 2px 8px; font-size: 11px; }")
        self.dir_combo.currentIndexChanged.connect(
            lambda i: self.direction_changed.emit(self._DIR_OPTIONS[i][1]))
        dir_row.addWidget(self.dir_combo, 1)

        self.bias4h_lbl = QLabel("4H: —", self)
        self.bias4h_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {_MUTED};"
            " font-family: 'Consolas', monospace;")
        dir_row.addWidget(self.bias4h_lbl)
        root.addLayout(dir_row)

        # ── Tombol eksekusi (aktif hanya saat setup ACTIVE + plan) ───
        self.exec_btn = QPushButton("🚀 EKSEKUSI (PAPER)", self)
        self.exec_btn.setEnabled(False)
        self.exec_btn.clicked.connect(self._emit_execute)
        self._style_exec_btn()
        root.addWidget(self.exec_btn)

        # ── Toggle auto-trade (konfirmasi sekali, lalu eksekusi otomatis) ─
        self.auto_btn = QPushButton("🤖 AUTO TRADE: OFF", self)
        self.auto_btn.setCheckable(True)
        self.auto_btn.clicked.connect(
            lambda checked: self.auto_toggle_requested.emit(bool(checked)))
        self._style_auto_btn(False)
        root.addWidget(self.auto_btn)

        # ── Warning ──────────────────────────────────────────────────
        self.warn_lbl = QLabel("", self)
        self.warn_lbl.setStyleSheet(f"font-size: 10px; color: {_AMBER};")
        self.warn_lbl.setWordWrap(True)
        root.addWidget(self.warn_lbl)
        root.addStretch()

    # ── Eksekusi ──────────────────────────────────────────────────────

    def set_exec_mode(self, mode: str):
        """'PAPER' atau 'LIVE' — hanya mengubah label/warna tombol."""
        self._exec_mode = mode.upper()
        self._style_exec_btn()
        self._style_auto_btn(self.auto_btn.isChecked())

    def _style_exec_btn(self):
        live = self._exec_mode == "LIVE"
        self.exec_btn.setText("🔴 EKSEKUSI (LIVE)" if live else "🚀 EKSEKUSI (PAPER)")
        accent = _RED if live else _GREEN
        self.exec_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #171722; border: 1px solid {accent};
                color: {accent}; padding: 8px; border-radius: 4px;
                font-weight: 900; font-size: 12px;
            }}
            QPushButton:disabled {{
                border: 1px solid #2d2d38; color: {_MUTED};
            }}
            QPushButton:hover:enabled {{ background-color: #1c1c26; }}
        """)

    def _emit_execute(self):
        if self._last_entry and self._last_entry.get("plan"):
            self.execute_requested.emit(dict(self._last_entry))

    def set_auto_state(self, on: bool):
        """Finalisasi state toggle auto-trade (dipanggil dashboard setelah
        konfirmasi diterima/ditolak/dimatikan otomatis)."""
        self.auto_btn.blockSignals(True)
        self.auto_btn.setChecked(on)
        self.auto_btn.blockSignals(False)
        self._style_auto_btn(on)

    def _style_auto_btn(self, on: bool):
        live = self._exec_mode == "LIVE"
        if on:
            accent = _RED if live else _AMBER
            self.auto_btn.setText(f"🤖 AUTO TRADE: ON ({self._exec_mode})")
            self.auto_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {accent}; border: 1px solid {accent};
                    color: #0b0b0d; padding: 7px; border-radius: 4px;
                    font-weight: 900; font-size: 12px;
                }}
                QPushButton:hover {{ background-color: {accent}; }}
            """)
        else:
            self.auto_btn.setText("🤖 AUTO TRADE: OFF")
            self.auto_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #171722; border: 1px solid #2d2d38;
                    color: {_MUTED}; padding: 7px; border-radius: 4px;
                    font-weight: 900; font-size: 12px;
                }}
                QPushButton:hover {{ border: 1px solid {_AMBER}; color: {_AMBER}; }}
            """)

    # ── Helpers ───────────────────────────────────────────────────────

    def _bar_color(self, color: str):
        self.score_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #171720;
                border: 1px solid #282835;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 6px;
            }}
        """)

    def reset(self):
        self.verdict.setText("⏳ WAIT")
        self.verdict.setStyleSheet(
            f"font-size: 21px; font-weight: 900; color: {_MUTED}; letter-spacing: 0.5px;")
        self.subline.setText("Menunggu confluence…")
        self.subline.setStyleSheet(f"font-size: 11px; color: {_MUTED};")
        self.score_bar.setValue(0)
        self._bar_color(_MUTED)
        self.score_lbl.setText("0")
        self.score_lbl.setStyleSheet(f"font-size: 17px; font-weight: 900; color: {_MUTED};")
        self.grade_lbl.setText("—")
        self.conf_title.setText("CONFLUENCE 0/6")
        for lbl in self.check_labels.values():
            lbl.setText("· —")
            lbl.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {_MUTED};")
            lbl.setToolTip("")
        for v in self.plan_rows.values():
            v.setText("—")
        self.warn_lbl.setText("")
        self._last_entry = None
        self.exec_btn.setEnabled(False)

    def update_bias4h(self, b4: dict | None):
        """Refresh label bias 4H (symbol fokus)."""
        if not b4 or not b4.get("ready"):
            self.bias4h_lbl.setText("4H: …")
            self.bias4h_lbl.setStyleSheet(
                f"font-size: 11px; font-weight: bold; color: {_MUTED};"
                " font-family: 'Consolas', monospace;")
            return
        trend, bias = b4.get("trend", "FLAT"), float(b4.get("bias", 0.0))
        arrow = {"UP": "▲", "DOWN": "▼"}.get(trend, "─")
        col = _GREEN if trend == "UP" else _RED if trend == "DOWN" else _MUTED
        self.bias4h_lbl.setText(f"4H: {arrow} {trend} {bias:+.2f}")
        self.bias4h_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {col};"
            " font-family: 'Consolas', monospace;")

    # ── Update ────────────────────────────────────────────────────────

    def update_entry(self, entry: dict | None):
        if not entry or not entry.get("ready"):
            return

        phase = entry.get("phase", "WAIT")
        side = entry.get("side")
        score = int(entry.get("score", 0))
        grade = entry.get("grade", "—")
        setup = entry.get("setup", "")
        status = entry.get("status", "")

        side_col = _GREEN if side == "LONG" else _RED if side == "SHORT" else _MUTED

        # Verdict
        if phase == "ACTIVE" and side:
            icon = "🟢" if side == "LONG" else "🔴"
            self.verdict.setText(f"{icon} {side} — {setup}" if setup else f"{icon} {side}")
            col = side_col
            sub = "Setup AKTIF — plan terkunci di chart"
            if status == "TP1":
                sub = "TP1 tercapai ✓ — sisa posisi menuju TP2"
            elif status == "PARTIAL":
                sub = "PARTIAL ✓ 50% profit dikunci — SL → breakeven"
            elif status == "RUNNER":
                sub = "RUNNER 🏃 — SL trailing di belakang harga"
        elif phase == "FORMING" and side:
            self.verdict.setText(f"◔ {side} TERBENTUK…")
            col = side_col
            sub = f"Confluence menguat ({setup})" if setup else "Confluence menguat…"
        else:
            self.verdict.setText("⏳ WAIT")
            col = _MUTED
            sub = "Belum ada edge — jangan paksa entry"
        self.verdict.setStyleSheet(
            f"font-size: 21px; font-weight: 900; color: {col}; letter-spacing: 0.5px;")
        self.subline.setText(sub)
        self.subline.setStyleSheet(f"font-size: 11px; color: {col};")

        # Skor
        self.score_bar.setValue(score)
        self._bar_color(col if phase != "WAIT" else _MUTED)
        self.score_lbl.setText(str(score))
        self.score_lbl.setStyleSheet(f"font-size: 17px; font-weight: 900; color: {col};")
        grade_col = _GREEN if grade in ("A", "A+") else _AMBER if grade == "B" else _MUTED
        self.grade_lbl.setText(grade)
        self.grade_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 900; color: {grade_col};"
            "padding: 1px 7px; border: 1px solid #2d2d38; border-radius: 4px;")

        # Checklist
        checks = {c["key"]: c for c in entry.get("checks", [])}
        n_ok = sum(1 for c in checks.values() if c["ok"])
        self.conf_title.setText(f"CONFLUENCE {n_ok}/6" if checks else "CONFLUENCE —")
        for key, lbl in self.check_labels.items():
            c = checks.get(key)
            if c is None:
                lbl.setText(f"· {_shorten(key)}")
                lbl.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {_MUTED};")
                lbl.setToolTip("")
                continue
            mark = "✓" if c["ok"] else "✗"
            ccol = _GREEN if c["ok"] else "#5c5c6e"
            lbl.setText(f"{mark} {c['name']}")
            lbl.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {ccol};")
            lbl.setToolTip(c.get("detail", ""))

        # Trade plan
        plan = entry.get("plan")
        if plan and phase == "ACTIVE":
            self.plan_rows["entry"].setText(
                f"{plan['entry_lo']:,.6g} – {plan['entry_hi']:,.6g}")
            self.plan_rows["entry"].setStyleSheet(
                "font-size: 12px; font-weight: 900; font-family: monospace;"
                f"color: {_ACCENT};")
            self.plan_rows["stop"].setText(
                f"{plan['stop']:,.6g}   (-{plan['risk_pct']:.2f}%)")
            self.plan_rows["stop"].setStyleSheet(
                "font-size: 12px; font-weight: 900; font-family: monospace;"
                f"color: {_RED};")
            tp1_extra = "  ✓hit" if plan.get("tp1_hit") else ""
            self.plan_rows["tp1"].setText(
                f"{plan['tp1']:,.6g}   R {plan['rr1']:.1f}{tp1_extra}")
            self.plan_rows["tp1"].setStyleSheet(
                "font-size: 12px; font-weight: 900; font-family: monospace;"
                f"color: {_GREEN};")
            self.plan_rows["tp2"].setText(
                f"{plan['tp2']:,.6g}   R {plan['rr2']:.1f}")
            self.plan_rows["tp2"].setStyleSheet(
                "font-size: 12px; font-weight: 900; font-family: monospace;"
                f"color: {_GREEN};")
        else:
            for v in self.plan_rows.values():
                v.setText("—")
                v.setStyleSheet("font-size: 12px; font-weight: 900;"
                                f"font-family: monospace; color: {_MUTED};")

        # Warnings
        warns = entry.get("warnings", [])
        self.warn_lbl.setText("\n".join(f"⚠ {w}" for w in warns[:2]))

        # Tombol eksekusi hidup hanya saat setup ACTIVE dengan plan valid
        self._last_entry = entry
        self.exec_btn.setEnabled(phase == "ACTIVE" and bool(plan))


def _shorten(key: str) -> str:
    return {"flow": "FLOW", "battle": "BATTLE", "whale": "WHALE",
            "structure": "STRUKTUR", "room": "RUANG GERAK",
            "timing": "TIMING"}.get(key, key.upper())
