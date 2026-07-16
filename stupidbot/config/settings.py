"""Konfigurasi terpusat. Semua parameter strategi dan risiko ada di sini.

Prinsip: default konservatif — bot lebih memilih tidak trading daripada
mengambil trade berkualitas rendah.
"""
from dataclasses import dataclass, asdict
import json
from pathlib import Path


@dataclass
class Settings:
    # --- Risiko ---
    risk_per_trade_pct: float = 1.0    # fallback bila adaptive risk dimatikan
    min_rr: float = 2.0                # RR minimum, tidak boleh diturunkan
    max_rr: float = 5.0                # RR maksimum yang dikejar dari struktur
    fee_pct: float = 0.05              # taker fee per sisi (%) — SL / exit market
    maker_fee_pct: float = 0.02        # maker fee per sisi (%) — entry limit & TP limit

    # --- Adaptive risk ---
    # tier naik satu tingkat HANYA saat equity mencetak high baru;
    # turun satu tingkat saat drawdown dari peak melewati ambang.
    risk_tiers_pct: tuple = (0.5, 1.0, 1.5)
    risk_step_down_dd_pct: float = 3.0

    # --- Equity protection ---
    daily_dd_stop_pct: float = 2.0     # dd harian > ini → stop sampai hari berikutnya (UTC)
    total_dd_stop_pct: float = 8.0     # dd total > ini → cooldown
    total_dd_cooldown_days: int = 14   # lama cooldown setelah dd total tersentuh

    # --- Quality over quantity ---
    max_trades_per_month: int = 10     # target 3-10 trade berkualitas per bulan
    entry_cooldown_hours: float = 12.0  # jeda minimal antar entry

    # --- Portfolio mode ---
    max_open_positions: int = 1        # posisi bersamaan maksimal
    min_structure_score: float = 50.0  # skor struktur Daily minimal agar layak dipilih

    # --- ATR (satu-satunya indikator yang diizinkan) ---
    atr_period: int = 14
    atr_sl_buffer_mult: float = 0.25   # LIMIT entry dipasang sejauh ini di bawah/atas wick rejection
    limit_sl_atr_mult: float = 1.0     # jarak SL dari harga limit entry (x ATR)

    # --- Filter volatilitas ---
    min_daily_atr_pct: float = 1.0     # ATR Daily minimal sebagai % harga; di bawah ini pasar mati
    min_entry_atr_pct: float = 0.10    # ATR TF entry minimal sebagai % harga
    min_candle_range_atr: float = 0.5  # candle sinyal minimal 0.5x ATR (tolak candle mungil)

    # --- Struktur pasar ---
    daily_swing_k: int = 2             # fractal strength swing TF Daily
    entry_swing_k: int = 3             # fractal strength swing TF entry
    min_leg_atr_mult: float = 1.5      # impulse leg minimal (x ATR) agar layak di-pullback
    pullback_min: float = 0.382        # retracement minimal masuk zona logis
    pullback_max: float = 0.786        # retracement maksimal; lebih dalam = struktur rusak

    # --- Manajemen posisi ---
    partial_tp_r: float = 1.5          # partial TP di +1.5R
    partial_fraction: float = 0.5      # porsi yang ditutup saat partial
    be_after_partial: bool = True      # pindahkan SL ke breakeven setelah partial
    trail_start_r: float = 2.0         # trailing baru aktif setelah +2R (jangan trail terlalu dini)
    trail_atr_mult: float = 2.0        # jarak trailing stop (x ATR)

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        s = cls()
        if path:
            p = Path(path)
            if p.exists():
                for k, v in json.loads(p.read_text()).items():
                    if hasattr(s, k):
                        setattr(s, k, v)
        return s

    def to_dict(self) -> dict:
        return asdict(self)
