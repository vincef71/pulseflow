from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import Qt
from pulseflow.ui.styles import COLORS

class VelocityMeter(QFrame):
    """
    Displays real-time trade, volume, delta and price velocity stats,
    Z-scores, and the unified Aggression Score.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("class", "Panel")
        self._vel_ema = {}   # per-key smoothed per-second rate → readable per-minute
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        self.title = QLabel("REALTIME AGGRESSION METER", self)
        self.title.setObjectName("TitleLabel")
        layout.addWidget(self.title)

        # Aggression Score layout
        score_layout = QHBoxLayout()
        self.score_label = QLabel("AGGRESSION SCORE:", self)
        self.score_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        
        self.score_val = QLabel("30.0", self)
        self.score_val.setStyleSheet(f"font-size: 24px; font-weight: 900; color: {COLORS['accent']};")
        
        self.regime_val = QLabel("[NORMAL]", self)
        self.regime_val.setStyleSheet("font-size: 14px; font-weight: bold; color: #7d7d8e;")
        
        score_layout.addWidget(self.score_label)
        score_layout.addWidget(self.score_val)
        score_layout.addWidget(self.regime_val)
        score_layout.addStretch()
        layout.addLayout(score_layout)

        # Progress bar representation
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(30)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: #171720;
                border: 1px solid #282835;
                height: 12px;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background-color: {COLORS['accent']};
                border-radius: 6px;
            }}
        """)
        layout.addWidget(self.progress)
        layout.addSpacing(10)

        # Inner grid layout for specific velocities
        stats_layout = QVBoxLayout()
        self.vel_labels = {}
        self.vel_bars = {}

        metrics_list = [
            ("trade_velocity", "Trade Speed (trades/min)"),
            ("volume_velocity", "Volume Velocity (qty/min)"),
            ("delta_velocity", "Orderflow Delta (net/min)"),
            ("price_velocity", "Price Velocity ($/min)")
        ]

        for key, name in metrics_list:
            row = QHBoxLayout()
            lbl_name = QLabel(name, self)
            lbl_name.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
            
            lbl_val = QLabel("0.0", self)
            lbl_val.setStyleSheet("font-weight: bold; font-size: 12px;")
            
            self.vel_labels[key] = lbl_val
            
            row.addWidget(lbl_name)
            row.addStretch()
            row.addWidget(lbl_val)
            stats_layout.addLayout(row)

        layout.addLayout(stats_layout)

    def update_metrics(self, metrics: dict):
        agg_score = metrics.get("aggression_score", 30.0)
        regime = metrics.get("regime", "normal").upper()
        
        self.score_val.setText(f"{agg_score:.1f}")
        self.regime_val.setText(f"[{regime}]")
        self.progress.setValue(int(agg_score))

        # Color the aggression elements according to value intensity
        if agg_score < 20.0:
            regime_color = COLORS["text_muted"]
        elif agg_score < 40.0:
            regime_color = "#3b82f6"  # Blue
        elif agg_score < 60.0:
            regime_color = COLORS["green_glow"]
        elif agg_score < 80.0:
            regime_color = COLORS["orange_alert"]
        else:
            regime_color = COLORS["red_glow"]
            
        self.score_val.setStyleSheet(f"font-size: 24px; font-weight: 900; color: {regime_color};")
        self.regime_val.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {regime_color};")
        
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: #171720;
                border: 1px solid #282835;
                height: 12px;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background-color: {regime_color};
                border-radius: 6px;
            }}
        """)

        # Update specific velocities text
        inst = metrics.get("instantaneous", {})
        z_scores = metrics.get("z_scores", {}).get("30s", {})

        # Per-key display format for the per-minute rate
        fmt = {
            "trade_velocity":  "{:,.0f}",
            "volume_velocity": "{:,.1f}",
            "delta_velocity":  "{:+,.1f}",
            "price_velocity":  "{:,.2f}",
        }

        for key in self.vel_labels:
            val = inst.get(key, 0.0)
            # Find the z-score key match (e.g. trade_velocity_z)
            z_val = z_scores.get(f"{key}_z", 0.0)

            # Smooth the per-second rate then show it per minute — the raw
            # 100 ms value changes too fast to read.
            prev = self._vel_ema.get(key)
            ema = val if prev is None else prev + 0.08 * (val - prev)
            self._vel_ema[key] = ema
            per_min = ema * 60.0

            self.vel_labels[key].setText(f"{fmt[key].format(per_min)} (Z: {z_val:+.1f})")

            # Color delta velocity based on net buyer/seller flows
            if key == "delta_velocity":
                if per_min > 0:
                    self.vel_labels[key].setStyleSheet(f"color: {COLORS['green_glow']}; font-weight: bold;")
                elif per_min < 0:
                    self.vel_labels[key].setStyleSheet(f"color: {COLORS['red_glow']}; font-weight: bold;")
                else:
                    self.vel_labels[key].setStyleSheet("color: white;")
