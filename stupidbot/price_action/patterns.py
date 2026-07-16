"""Pola candle price action murni dari OHLC — tanpa indikator apa pun."""
from core.models import Candle, Direction


def is_bullish_pin(c: Candle) -> bool:
    rng = c.range
    if rng <= 0:
        return False
    # ekor bawah panjang, body kecil di sepertiga atas
    return c.lower_wick >= 0.6 * rng and c.body_size <= 0.35 * rng


def is_bearish_pin(c: Candle) -> bool:
    rng = c.range
    if rng <= 0:
        return False
    return c.upper_wick >= 0.6 * rng and c.body_size <= 0.35 * rng


def is_bullish_engulfing(prev: Candle, c: Candle) -> bool:
    return (
        c.is_bull
        and prev.is_bear
        and c.close >= prev.open
        and c.open <= prev.close
        and c.body_size >= prev.body_size
    )


def is_bearish_engulfing(prev: Candle, c: Candle) -> bool:
    return (
        c.is_bear
        and prev.is_bull
        and c.close <= prev.open
        and c.open >= prev.close
        and c.body_size >= prev.body_size
    )


def is_inside_bar(prev: Candle, c: Candle) -> bool:
    return c.high <= prev.high and c.low >= prev.low


def is_outside_bar(prev: Candle, c: Candle) -> bool:
    return c.high >= prev.high and c.low <= prev.low


def detect_rejection(prev: Candle | None, c: Candle, direction: Direction) -> str | None:
    """Kembalikan nama pola bila candle mengonfirmasi penolakan searah bias."""
    if direction == Direction.LONG:
        if is_bullish_pin(c):
            return "pin_bar"
        if prev is not None and is_bullish_engulfing(prev, c):
            return "engulfing"
    elif direction == Direction.SHORT:
        if is_bearish_pin(c):
            return "pin_bar"
        if prev is not None and is_bearish_engulfing(prev, c):
            return "engulfing"
    return None
