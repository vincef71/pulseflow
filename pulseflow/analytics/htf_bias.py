"""
HTF Bias Tracker — bias trend timeframe tinggi (4H) per symbol.

Berbeda dengan MarketContextTracker (bar 1m dibangun dari trade stream),
bar 4H tidak mungkin di-warm-up dari trade live — tracker ini murni REST:
fetch klines 4h Binance Futures saat start lalu refresh berkala di
background thread. Formula bias sama dengan konteks 1m (spread EMA20/50 +
slope EMA-cepat, dinormalisasi ATR-4H → [-1, +1]) supaya skalanya konsisten.

Dipakai untuk filter arah entry (mode AUTO: hanya entry searah bias 4H)
dan ditampilkan di entry card / heartbeat headless.

Threading: worker menulis `self._cached` dengan penggantian dict utuh
(atomik di bawah GIL); `snapshot()` dari thread mana pun aman.
"""

import json
import logging
import math
import threading
import time
import urllib.request
from typing import Any, Dict, List

from pulseflow.analytics.context import _clamp, _ema_series

logger = logging.getLogger("PulseFlow.HTFBias")

_BINANCE_KLINES = ("https://fapi.binance.com/fapi/v1/klines"
                   "?symbol={symbol}&interval={interval}&limit={limit}")


class HTFBiasTracker:
    """Satu instance per-symbol. `start()` sekali; `snapshot()` kapan pun."""

    INTERVAL     = "4h"
    LIMIT        = 120        # 120 bar 4H ≈ 20 hari
    EMA_FAST     = 20
    EMA_SLOW     = 50
    ATR_PERIOD   = 14
    MIN_READY    = 55         # bar minimum sebelum bias dianggap valid
    REFRESH_SEC  = 300        # refresh tiap 5 menit (bar 4H berubah lambat)
    FLAT_BAND    = 0.15       # |bias| di bawah ini = FLAT

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol
        self._cached: Dict[str, Any] = self._empty()
        self._stop_ev = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"htf-bias-{self.symbol}")
        self._thread.start()

    def stop(self):
        self._stop_ev.set()

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._cached)

    # ── Worker ────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_ev.is_set():
            try:
                self._refresh()
            except Exception as e:
                logger.warning("Bias 4H %s gagal refresh: %s", self.symbol, e)
            self._stop_ev.wait(self.REFRESH_SEC)

    def _refresh(self):
        sym = self.symbol.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        url = _BINANCE_KLINES.format(symbol=sym, interval=self.INTERVAL,
                                     limit=self.LIMIT)
        with urllib.request.urlopen(url, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
        # Baris terakhir = bar yang masih terbentuk — tetap dipakai (bias 4H
        # harus merespons pergerakan intrabar), tapi dicatat di n_bars closed.
        bars: List[Dict[str, float]] = [{
            "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
        } for r in rows]
        if len(bars) < self.EMA_SLOW + 3:
            self._cached = self._empty(n_bars=len(bars))
            return

        closes = [b["close"] for b in bars]
        ema_f = _ema_series(closes, self.EMA_FAST)
        ema_s = _ema_series(closes, self.EMA_SLOW)

        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        p = self.ATR_PERIOD
        atr = sum(trs[:p]) / p
        for tr in trs[p:]:
            atr = (atr * (p - 1) + tr) / p

        spread = ema_f[-1] - ema_s[-1]
        slope = ema_f[-1] - ema_f[-4] if len(ema_f) >= 4 else 0.0
        bias = (_clamp(0.6 * math.tanh(spread / (0.8 * atr))
                       + 0.4 * math.tanh(slope / (0.6 * atr)), -1.0, 1.0)
                if atr > 0 else 0.0)

        first = self._cached.get("ready") is False
        self._cached = {
            "ready": len(bars) >= self.MIN_READY and atr > 0,
            "n_bars": len(bars),
            "bias": float(round(bias, 3)),
            "trend": ("UP" if bias > self.FLAT_BAND else
                      "DOWN" if bias < -self.FLAT_BAND else "FLAT"),
            "ema_fast": float(ema_f[-1]),
            "ema_slow": float(ema_s[-1]),
            "atr_4h": float(atr),
            "last_close": float(closes[-1]),
            "ts": time.time(),
        }
        if first and self._cached["ready"]:
            logger.info("Bias 4H %s siap: %s (%+.2f)", sym,
                        self._cached["trend"], bias)

    @staticmethod
    def _empty(n_bars: int = 0) -> Dict[str, Any]:
        return {"ready": False, "n_bars": n_bars, "bias": 0.0,
                "trend": "FLAT", "ema_fast": 0.0, "ema_slow": 0.0,
                "atr_4h": 0.0, "last_close": 0.0, "ts": time.time()}
