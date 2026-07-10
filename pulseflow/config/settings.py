import os

# System Configurations
TICK_INTERVAL_MS = 100
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XAUUSDT", "HYPEUSDT"]

# Velocity Engine Settings
ROLLING_WINDOWS = {
    "5s": 5,
    "30s": 30,
    "1m": 60,
    "5m": 300
}

# ATR (Average True Range) — volatility analytic for the orderflow chart
ATR_CONFIG = {
    "bar_seconds":   5,    # aggregate the price stream into 5s OHLC bars
    "period":       14,    # Wilder smoothing period (number of bars)
    "band_mult":   1.5,    # ATR multiplier for the chart volatility envelope
    "regime_window": 240,  # rolling sample of ATR% bars for regime ranking
}

# Daily ATR — macro/structural volatility from daily klines, compared
# against the live intraday price movement.
DAILY_ATR_CONFIG = {
    "period":   14,   # Wilder ATR period in days
    "interval": "1d", # kline interval
}

# HTF Bias — timeframe tinggi untuk filter arah entry (mode AUTO).
# Interval bisa dipilih user: combo di GUI atau --htf-interval headless
# (juga via control.json → key "htf_interval", hot-reload tanpa restart).
HTF_BIAS_CONFIG = {
    "interval": "1h",  # default timeframe bias HTF
    # Interval Binance yang diizinkan untuk dipilih user
    "allowed": ["15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
}

# Weights for Aggression Score
AGGRESSION_WEIGHTS = {
    "volume_velocity": 0.30,
    "delta_velocity": 0.30,
    "trade_velocity": 0.15,
    "price_velocity": 0.15,
    "oi_velocity": 0.10
}

# Signal Engine Thresholds
THRESHOLDS = {
    "velocity_spike_z": 2.5,
    "absorption_ratio": 10.0,
    "liquidation_cascade_usd": 50000,
    "squeeze_oi_percent_drop": -1.5,

    # NEW: minimum aggression score to fire any signal
    "min_aggression_score": 40.0,

    # NEW: liquidation USD over 5s window to confirm squeeze / cascade
    "squeeze_liq_usd": 80000,

    # NEW: spread expansion ratio to confirm real impulse
    "spread_expansion_confirm": 1.5,

    # NEW: absorption efficiency threshold (volume / |dPrice|)
    "absorption_efficiency_min": 800.0,
}

# NEW: Per-priority cooldown seconds
# Bypass cooldown if incoming signal has higher priority than last
SIGNAL_COOLDOWNS = {
    "CRITICAL": 5.0,
    "HIGH": 10.0,
    "WARNING": 15.0,
    "INFO": 20.0,
}

# NEW: Minimum Z-score thresholds per signal class (used by interpreter)
INTERPRETER_THRESHOLDS = {
    "squeeze_vol_z": 2.0,
    "squeeze_price_z": 1.5,
    "squeeze_oi_pct": -0.4,
    "aggressive_vol_z": 2.0,
    "aggressive_delta_z": 1.5,
    "aggressive_price_z": 1.0,
    "aggressive_oi_pct": 0.3,
    "absorption_vol_z": 2.0,
    "absorption_price_z_max": 0.7,
    "exhaustion_vol_z": 3.0,
    "exhaustion_price_z_max": 1.2,
    "velocity_spike_min_z": 2.5,
}

# Trade Filtering — minimum USD notional to pass the bot-noise floor
MIN_NOTIONAL_USD = {
    "BTCUSDT":        500.0,
    "ETHUSDT":        200.0,
    "XAUUSDT":         500.0,
    "HYPEUSDT":        100.0,
    "__default__": 50.0,
}

# Whale tier thresholds (USD notional per trade)
WHALE_THRESHOLDS_USD = {
    "SMALL":  {"BTCUSDT":   5_000, "ETHUSDT":   2_000, "XAUUSDT":    500, "HYPEUSDT":    500, "__default__":    500},
    "MEDIUM": {"BTCUSDT":  25_000, "ETHUSDT":  10_000, "XAUUSDT":  5_000, "HYPEUSDT":  5_000, "__default__":  2_500},
    "LARGE":  {"BTCUSDT": 100_000, "ETHUSDT":  50_000, "XAUUSDT": 50_000, "HYPEUSDT":  25_000, "__default__": 10_000},
    "BLOCK":  {"BTCUSDT": 500_000, "ETHUSDT": 200_000, "XAUUSDT": 500_000, "HYPEUSDT": 100_000, "__default__": 50_000},
}

# Whale ADAPTIF — ambang tier dihitung dari persentil rolling notional
# per-symbol, supaya definisi "whale" relatif terhadap aliran symbol itu
# sendiri (BTC vs coin mikro sama-sama benar) dan ikut likuiditas sesi.
# WHALE_THRESHOLDS_USD tetap dipakai sebagai fallback saat warm-up; floor
# absolut mencegah coin super-sepi melabeli trade receh sebagai whale.
WHALE_ADAPTIVE = {
    "enabled": True,
    "window_trades":   2000,   # sampel rolling notional per symbol
    "warmup_trades":    500,   # di bawah ini pakai tabel statis
    "recompute_every":  100,   # hitung ulang persentil tiap N trade
    "percentiles": {"MEDIUM": 95.0, "LARGE": 99.0, "BLOCK": 99.9},
    "floors_usd":  {"MEDIUM": 500.0, "LARGE": 2_000.0, "BLOCK": 10_000.0},
}

# Adaptive percentile filter — keep trades above this percentile of recent notionals
PERCENTILE_FILTER = {
    "window_trades": 500,  # rolling sample size for P-value computation
    "percentile":     70,  # top-30% by notional passes the adaptive filter
}

# Execution clustering — merge same-direction trades within this window
TRADE_CLUSTERING = {
    "window_ms": 200,
}

# Storage Settings
STORAGE_DIR = os.path.join(os.path.expanduser("~"), ".pulseflow", "data")
RETENTION_DAYS = 7
PARQUET_COMPRESSION = "SNAPPY"
