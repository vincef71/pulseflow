"""Deteksi struktur pasar dari swing murni: HH/HL/LH/LL, BOS, CHoCH.

Swing dideteksi dengan fractal berkekuatan k dan baru dianggap valid setelah
k candle berikutnya close (anti-lookahead untuk backtest).
"""
from dataclasses import dataclass

from core.models import Candle, Swing, SwingType, Trend


@dataclass
class StructureEvent:
    type: str    # BOS_UP | BOS_DOWN | CHOCH_UP | CHOCH_DOWN
    level: float  # level swing yang ditembus
    index: int   # index candle saat penembusan


class StructureTracker:
    """Memproses candle closed satu per satu dan memelihara daftar swing
    berlabel, event BOS/CHoCH, serta arah trend struktural."""

    def __init__(self, k: int = 2):
        self.k = k
        self.candles: list[Candle] = []
        self.swings: list[Swing] = []
        self.events: list[StructureEvent] = []

    # ------------------------------------------------------------------ #
    def update(self, candle: Candle) -> list[StructureEvent]:
        self.candles.append(candle)
        i = len(self.candles) - 1 - self.k
        if i >= self.k:
            self._try_confirm_swing(i)
        new_events = self._detect_breaks(len(self.candles) - 1)
        self.events.extend(new_events)
        return new_events

    # ------------------------------------------------------------------ #
    def _try_confirm_swing(self, i: int) -> None:
        c = self.candles[i]
        before = self.candles[i - self.k : i]
        after = self.candles[i + 1 : i + 1 + self.k]

        if all(c.high > x.high for x in before) and all(c.high >= x.high for x in after):
            self._add_swing(
                Swing(index=i, ts=c.ts, price=c.high, type=SwingType.HIGH, confirmed_at=i + self.k)
            )
        if all(c.low < x.low for x in before) and all(c.low <= x.low for x in after):
            self._add_swing(
                Swing(index=i, ts=c.ts, price=c.low, type=SwingType.LOW, confirmed_at=i + self.k)
            )

    def _add_swing(self, s: Swing) -> None:
        # dua swing sejenis berurutan → simpan hanya yang lebih ekstrem
        if self.swings and self.swings[-1].type == s.type:
            prev = self.swings[-1]
            if s.type == SwingType.HIGH and s.price <= prev.price:
                return
            if s.type == SwingType.LOW and s.price >= prev.price:
                return
            self.swings.pop()
        s.label = self._label(s)
        self.swings.append(s)

    def _label(self, s: Swing) -> str:
        prev = next((x for x in reversed(self.swings) if x.type == s.type), None)
        if prev is None:
            return ""
        if s.type == SwingType.HIGH:
            return "HH" if s.price > prev.price else "LH"
        return "HL" if s.price > prev.price else "LL"

    # ------------------------------------------------------------------ #
    def _detect_breaks(self, idx: int) -> list[StructureEvent]:
        c = self.candles[idx]
        events: list[StructureEvent] = []
        lt = self._label_trend()

        sh = self.last_swing(SwingType.HIGH)
        if sh and not sh.broken and c.close > sh.price:
            sh.broken = True
            etype = "CHOCH_UP" if lt == Trend.DOWN else "BOS_UP"
            events.append(StructureEvent(etype, sh.price, idx))

        sl = self.last_swing(SwingType.LOW)
        if sl and not sl.broken and c.close < sl.price:
            sl.broken = True
            etype = "CHOCH_DOWN" if lt == Trend.UP else "BOS_DOWN"
            events.append(StructureEvent(etype, sl.price, idx))

        return events

    # ------------------------------------------------------------------ #
    def last_swing(self, stype: SwingType) -> Swing | None:
        return next((s for s in reversed(self.swings) if s.type == stype), None)

    def _label_trend(self) -> Trend:
        sh = self.last_swing(SwingType.HIGH)
        sl = self.last_swing(SwingType.LOW)
        if not sh or not sl:
            return Trend.NEUTRAL
        if sh.label == "HH" and sl.label == "HL":
            return Trend.UP
        if sh.label == "LH" and sl.label == "LL":
            return Trend.DOWN
        return Trend.NEUTRAL

    @property
    def trend(self) -> Trend:
        """Trend berbasis label swing, dinetralkan bila event terakhir CHoCH
        melawan trend tersebut (konservatif: preservasi modal dulu)."""
        lt = self._label_trend()
        ev = self.events[-1] if self.events else None
        if ev:
            if lt == Trend.UP and ev.type == "CHOCH_DOWN":
                return Trend.NEUTRAL
            if lt == Trend.DOWN and ev.type == "CHOCH_UP":
                return Trend.NEUTRAL
        return lt

    # --- target struktural ------------------------------------------- #
    def nearest_high_above(self, price: float) -> float | None:
        levels = [s.price for s in self.swings if s.type == SwingType.HIGH and s.price > price]
        return min(levels) if levels else None

    def nearest_low_below(self, price: float) -> float | None:
        levels = [s.price for s in self.swings if s.type == SwingType.LOW and s.price < price]
        return max(levels) if levels else None
