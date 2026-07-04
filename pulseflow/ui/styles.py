# PulseFlow Dark Institutional Style Theme

COLORS = {
    "bg_dark": "#0b0b0d",       # Deep charcoal/almost black
    "bg_panel": "#121216",      # Sleek panel background
    "bg_hover": "#1c1c22",      # Active hover color
    "accent": "#00ffd2",        # High-energy aqua/cyan
    "text_main": "#e3e3e7",     # Soft white
    "text_muted": "#7d7d8e",    # Dark gray for labels
    "green_glow": "#10b981",    # Bright emerald
    "red_glow": "#f43f5e",      # Deep neon crimson
    "orange_alert": "#f59e0b",  # Amber orange
    "purple_liq": "#d946ef"     # Liquidation neon purple
}

QSS_STYLE = f"""
QMainWindow {{
    background-color: {COLORS["bg_dark"]};
}}

QWidget {{
    color: {COLORS["text_main"]};
    font-family: "Outfit", "Inter", "Segoe UI", sans-serif;
    font-size: 13px;
}}

QFrame.Panel {{
    background-color: {COLORS["bg_panel"]};
    border: 1px solid #23232a;
    border-radius: 8px;
}}

QLabel {{
    color: {COLORS["text_main"]};
}}

QLabel#TitleLabel {{
    font-size: 16px;
    font-weight: bold;
    color: {COLORS["accent"]};
    border-bottom: 1px solid #23232a;
    padding-bottom: 4px;
    margin-bottom: 6px;
}}

QTableWidget {{
    background-color: {COLORS["bg_panel"]};
    gridline-color: #23232a;
    border: none;
    border-radius: 6px;
    selection-background-color: {COLORS["bg_hover"]};
    selection-color: {COLORS["accent"]};
}}

QTableWidget::item {{
    padding: 6px;
}}

QHeaderView::section {{
    background-color: #17171f;
    color: {COLORS["text_muted"]};
    padding: 6px;
    border: none;
    font-weight: bold;
    font-size: 11px;
}}

QScrollBar:vertical {{
    background: #0f0f13;
    width: 8px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: #33333f;
    min-height: 20px;
    border-radius: 4px;
}}

QPushButton {{
    background-color: #1a1a24;
    border: 1px solid #323242;
    color: {COLORS["text_main"]};
    padding: 8px 16px;
    border-radius: 4px;
    font-weight: bold;
}}

QPushButton:hover {{
    background-color: {COLORS["bg_hover"]};
    border: 1px solid {COLORS["accent"]};
}}

QPushButton:pressed {{
    background-color: #0b0b0d;
}}
"""
