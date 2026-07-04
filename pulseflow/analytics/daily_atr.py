from collections import deque
from typing import Dict, Any, Optional


class DailyATRTracker:
    """
    Average True Range over **daily** candles — the macro/structural
    volatility — measured against the live intraday price movement.

    Fed by the feed layer through `on_kline`:
      • closed daily candles seed/extend the ATR series (Wilder smoothing)
      • the live forming 'today' candle tracks the current session's range

    The live trade price further refines today's high/low/close between
    kline pushes (`observe_price`) so the analysis stays in sync with the
    current move.

    True Range (daily) = max(high - low,
                             |high - prev_day_close|,
                             |low  - prev_day_close|)
    """

    def __init__(self, period: int = 14):
        self.period = period
        self.closed: deque = deque(maxlen=period * 3)   # closed daily OHLC dicts
        self.today: Optional[Dict[str, float]] = None
        self.atr: float = 0.0

    # ── Ingestion ─────────────────────────────────────────────────────

    def on_kline(self, k: Dict[str, Any]) -> None:
        """Receive a daily candle (closed or live-forming)."""
        candle = {
            "open":      float(k["open"]),
            "high":      float(k["high"]),
            "low":       float(k["low"]),
            "close":     float(k["close"]),
            "open_time": int(k.get("open_time", 0)),
        }
        if k.get("is_closed"):
            # REST seed and WS pushes can overlap — de-dupe by open_time.
            if self.closed and self.closed[-1]["open_time"] == candle["open_time"]:
                self.closed[-1] = candle
            else:
                self.closed.append(candle)
            self._recompute_atr()
            self.today = None   # a new session begins after a close
        else:
            self.today = candle

    def observe_price(self, price: float) -> None:
        """Refine today's range with the latest trade price."""
        if price is None or price <= 0.0:
            return
        if self.today is None:
            self.today = {"open": price, "high": price, "low": price,
                          "close": price, "open_time": 0}
        else:
            self.today["high"]  = max(self.today["high"], price)
            self.today["low"]   = min(self.today["low"], price)
            self.today["close"] = price

    # ── Wilder ATR over closed daily candles ──────────────────────────

    def _recompute_atr(self) -> None:
        candles = list(self.closed)
        if len(candles) < 2:
            self.atr = (candles[0]["high"] - candles[0]["low"]) if candles else 0.0
            return

        trs = []
        for i in range(1, len(candles)):
            h, l = candles[i]["high"], candles[i]["low"]
            pc   = candles[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

        p = self.period
        if len(trs) <= p:
            self.atr = sum(trs) / len(trs)
        else:
            atr = sum(trs[:p]) / p          # SMA seed
            for tr in trs[p:]:              # Wilder smoothing
                atr = (atr * (p - 1) + tr) / p
            self.atr = atr

    @property
    def ready(self) -> bool:
        return len(self.closed) >= self.period and self.atr > 0.0

    # ── Analysis vs current price movement ────────────────────────────

    def snapshot(self, current_price: float) -> Dict[str, Any]:
        self.observe_price(current_price)
        today = self.today or {}
        price = current_price if (current_price and current_price > 0) \
            else today.get("close", 0.0)
        atr = self.atr

        out: Dict[str, Any] = {
            "atr":        atr,
            "atr_pct":    (atr / price * 100.0) if price > 0 else 0.0,
            "ready":      self.ready,
            "days":       len(self.closed),
            "period":     self.period,
            "today_open": today.get("open", 0.0),
            "today_high": today.get("high", 0.0),
            "today_low":  today.get("low", 0.0),
        }

        if not (atr > 0.0 and today):
            out.update({
                "today_range": 0.0, "range_used_pct": 0.0,
                "move_from_open": 0.0, "move_from_open_atr": 0.0,
                "range_position_pct": 0.0,
                "upper_band": 0.0, "lower_band": 0.0,
                "direction": "flat", "state": "warming",
            })
            return out

        t_open = today["open"]
        t_high = today["high"]
        t_low  = today["low"]
        t_range = t_high - t_low

        range_used = t_range / atr * 100.0          # % of a normal day consumed
        move       = price - t_open
        move_atr   = move / atr                      # signed move from open in ATRs
        rng_pos    = ((price - t_low) / t_range * 100.0) if t_range > 1e-12 else 50.0

        upper = t_open + atr                         # projected daily envelope
        lower = t_open - atr
        direction = "up" if move > 0 else ("down" if move < 0 else "flat")

        # How far through a normal day's range we are
        if range_used < 40.0:
            state = "compressed"
        elif range_used < 80.0:
            state = "developing"
        elif range_used <= 120.0:
            state = "range_complete"
        else:
            state = "expansion"                       # outsized / volatility break

        out.update({
            "today_range":        t_range,
            "range_used_pct":     range_used,
            "move_from_open":     move,
            "move_from_open_atr": move_atr,
            "range_position_pct": rng_pos,
            "upper_band":         upper,
            "lower_band":         lower,
            "direction":          direction,
            "state":              state,
        })
        return out
