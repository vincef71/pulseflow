"""Model data inti: candle, swing, sinyal, dan catatan trade."""
from dataclasses import dataclass, field, asdict
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class Trend(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"


class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass
class Candle:
    ts: int  # open time, epoch ms (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open


@dataclass
class Swing:
    index: int          # index candle pembentuk swing
    ts: int
    price: float
    type: SwingType
    confirmed_at: int   # index candle saat swing terkonfirmasi (index + k, anti-lookahead)
    label: str = ""     # HH / HL / LH / LL
    broken: bool = False


@dataclass
class Signal:
    direction: Direction
    ts: int
    entry: float       # harga LIMIT entry (di bekas level SL, bawah/atas wick rejection)
    sl: float
    tp: float
    rr: float
    atr: float
    pattern: str
    reason: str
    leg_high: float = 0.0  # swing high leg — pembatalan pending saat breakout tanpa fill
    leg_low: float = 0.0   # swing low leg


@dataclass
class Trade:
    """Catatan lengkap satu trade — semua field wajib dari spesifikasi logging."""
    entry_ts: int
    entry_date: str
    exit_date: str
    symbol: str
    daily_bias: str
    entry_reason: str
    pattern: str
    atr: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_amount: float
    risk_pct: float
    rr_planned: float
    exit_reason: str
    exit_price: float
    pnl: float
    r_multiple: float
    mfe_r: float
    mae_r: float

    def to_dict(self) -> dict:
        return asdict(self)
