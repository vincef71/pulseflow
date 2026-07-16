"""Entry engine TF rendah (1H / 15M).

Entry hanya terjadi bila SEMUA syarat terpenuhi:
1. Bias Daily ada (dari DailyBiasEngine).
2. Struktur TF entry mendukung bias (trend searah, tanpa CHoCH melawan).
3. Harga pullback ke area logis (retracement 38.2%–78.6% dari impulse leg).
4. Candle rejection mengonfirmasi kelanjutan (pin bar / engulfing).
5. RR minimal 1:2 terhadap target struktural.

Satu syarat gagal → tidak ada trade.
"""
from config.settings import Settings
from core.atr import ATRCalculator
from core.models import Candle, Direction, Signal, SwingType, Trend
from daily_bias.bias import DailyBiasEngine
from market_structure.structure import StructureTracker
from price_action.patterns import detect_rejection


class EntryEngine:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.tracker = StructureTracker(cfg.entry_swing_k)
        self.atr = ATRCalculator(cfg.atr_period)
        self._prev: Candle | None = None
        self._cur: Candle | None = None
        # alasan syarat yang gagal pada check() terakhir — untuk status console
        self.last_reject: str | None = None

    def _no(self, why: str) -> None:
        self.last_reject = why
        return None

    def update(self, candle: Candle) -> None:
        self.tracker.update(candle)
        self.atr.update(candle)
        self._prev = self._cur
        self._cur = candle

    # ------------------------------------------------------------------ #
    def check(self, bias: Direction, bias_reason: str, daily: DailyBiasEngine) -> Signal | None:
        self.last_reject = None
        if bias == Direction.NEUTRAL:
            return self._no("bias Daily netral")
        if self._cur is None or not self.atr.ready:
            return self._no("data TF entry belum cukup (ATR belum siap)")

        c = self.atr.value
        candle = self._cur
        atr_pct = 100.0 * c / candle.close
        if atr_pct < self.cfg.min_entry_atr_pct:
            return self._no(f"volatilitas TF entry rendah (ATR {atr_pct:.2f}% "
                            f"< {self.cfg.min_entry_atr_pct}%)")
        if candle.range < self.cfg.min_candle_range_atr * c:
            return self._no(f"candle sinyal terlalu kecil (range < "
                            f"{self.cfg.min_candle_range_atr}x ATR)")

        if bias == Direction.LONG:
            return self._check_long(candle, c, bias_reason, daily)
        return self._check_short(candle, c, bias_reason, daily)

    # ------------------------------------------------------------------ #
    def _check_long(self, candle: Candle, atr: float, bias_reason: str,
                    daily: DailyBiasEngine) -> Signal | None:
        # 2. struktur TF entry mendukung bias
        if self.tracker.trend != Trend.UP:
            return self._no(f"trend TF entry {self.tracker.trend.value}, "
                            f"belum mendukung bias LONG")

        high = self.tracker.last_swing(SwingType.HIGH)
        low = self.tracker.last_swing(SwingType.LOW)
        if not high or not low or low.index >= high.index:
            return self._no("belum ada impulse leg valid (swing low → swing high)")
        leg = high.price - low.price
        if leg < self.cfg.min_leg_atr_mult * atr:
            return self._no(f"impulse leg terlalu kecil ({leg:.6g} < "
                            f"{self.cfg.min_leg_atr_mult}x ATR)")

        # 3. pullback ke area logis
        zone_hi = high.price - self.cfg.pullback_min * leg
        zone_lo = high.price - self.cfg.pullback_max * leg
        if candle.close >= high.price:
            return self._no("close di atas leg high — breakout, bukan pullback")
        if candle.close <= low.price:
            return self._no("struktur jebol (close di bawah leg low)")
        if not (candle.low <= zone_hi and candle.close >= zone_lo):
            return self._no(f"harga belum masuk zona pullback "
                            f"{zone_lo:.6g}–{zone_hi:.6g}")

        # 4. rejection mengonfirmasi kelanjutan
        pattern = detect_rejection(self._prev, candle, Direction.LONG)
        if pattern is None:
            return self._no("di zona pullback tapi belum ada candle rejection "
                            "(pin bar / engulfing)")

        # 5. LIMIT entry di bekas level SL (bawah wick rejection — area
        #    stop-hunt); SL baru lebih dalam berbasis ATR; RR minimal 1:2.
        entry = candle.low - self.cfg.atr_sl_buffer_mult * atr
        stop = self.cfg.limit_sl_atr_mult * atr
        sl = entry - stop

        target = daily.nearest_high_above(entry)
        if target is not None:
            rr_avail = (target - entry) / stop
            if rr_avail < self.cfg.min_rr:
                return self._no(f"ruang ke resistance Daily {target:.6g} hanya "
                                f"{rr_avail:.1f}R (< {self.cfg.min_rr}R)")
            rr = min(rr_avail, self.cfg.max_rr)
        else:
            rr = self.cfg.min_rr  # tidak ada resistance di atas → target konservatif

        tp = entry + rr * stop
        reason = (f"{bias_reason}; pullback {self._depth(high.price, low.price, candle.low):.0f}% "
                  f"ke zona HL, rejection {pattern}, limit di bekas SL")
        return Signal(Direction.LONG, candle.ts, entry, sl, tp, rr, atr, pattern,
                      reason, leg_high=high.price, leg_low=low.price)

    # ------------------------------------------------------------------ #
    def _check_short(self, candle: Candle, atr: float, bias_reason: str,
                     daily: DailyBiasEngine) -> Signal | None:
        if self.tracker.trend != Trend.DOWN:
            return self._no(f"trend TF entry {self.tracker.trend.value}, "
                            f"belum mendukung bias SHORT")

        low = self.tracker.last_swing(SwingType.LOW)
        high = self.tracker.last_swing(SwingType.HIGH)
        if not low or not high or high.index >= low.index:
            return self._no("belum ada impulse leg valid (swing high → swing low)")
        leg = high.price - low.price
        if leg < self.cfg.min_leg_atr_mult * atr:
            return self._no(f"impulse leg terlalu kecil ({leg:.6g} < "
                            f"{self.cfg.min_leg_atr_mult}x ATR)")

        zone_lo = low.price + self.cfg.pullback_min * leg
        zone_hi = low.price + self.cfg.pullback_max * leg
        if candle.close <= low.price:
            return self._no("close di bawah leg low — breakdown, bukan pullback")
        if candle.close >= high.price:
            return self._no("struktur jebol (close di atas leg high)")
        if not (candle.high >= zone_lo and candle.close <= zone_hi):
            return self._no(f"harga belum masuk zona pullback "
                            f"{zone_lo:.6g}–{zone_hi:.6g}")

        pattern = detect_rejection(self._prev, candle, Direction.SHORT)
        if pattern is None:
            return self._no("di zona pullback tapi belum ada candle rejection "
                            "(pin bar / engulfing)")

        entry = candle.high + self.cfg.atr_sl_buffer_mult * atr
        stop = self.cfg.limit_sl_atr_mult * atr
        sl = entry + stop

        target = daily.nearest_low_below(entry)
        if target is not None:
            rr_avail = (entry - target) / stop
            if rr_avail < self.cfg.min_rr:
                return self._no(f"ruang ke support Daily {target:.6g} hanya "
                                f"{rr_avail:.1f}R (< {self.cfg.min_rr}R)")
            rr = min(rr_avail, self.cfg.max_rr)
        else:
            rr = self.cfg.min_rr

        tp = entry - rr * stop
        reason = (f"{bias_reason}; pullback {self._depth(low.price, high.price, candle.high):.0f}% "
                  f"ke zona LH, rejection {pattern}, limit di bekas SL")
        return Signal(Direction.SHORT, candle.ts, entry, sl, tp, rr, atr, pattern,
                      reason, leg_high=high.price, leg_low=low.price)

    @staticmethod
    def _depth(anchor: float, origin: float, extreme: float) -> float:
        leg = anchor - origin
        if leg == 0:
            return 0.0
        return abs((anchor - extreme) / leg) * 100.0
