"""
HTF Bias Tracker — bias trend timeframe tinggi (interval dipilih user) per symbol.

Berbeda dengan MarketContextTracker (bar 1m dibangun dari trade stream),
bar HTF tidak mungkin di-warm-up dari trade live — tracker ini murni REST:
fetch klines Binance Futures saat start lalu refresh berkala di background
thread. Formula bias sama dengan konteks 1m (spread EMA20/50 + slope
EMA-cepat, dinormalisasi ATR-HTF → [-1, +1]) supaya skalanya konsisten.

Interval bisa diganti runtime lewat `set_interval()` (GUI combo /
--htf-interval headless / control.json); worker langsung refresh ulang.

Dipakai untuk filter arah entry (mode AUTO: hanya entry searah bias HTF)
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
from typing import Any, Dict, List, Optional

from pulseflow.analytics.context import _clamp, _ema_series
from pulseflow.config.settings import HTF_BIAS_CONFIG

logger = logging.getLogger("PulseFlow.HTFBias")

_BINANCE_KLINES = ("https://fapi.binance.com/fapi/v1/klines"
                   "?symbol={symbol}&interval={interval}&limit={limit}")


class HTFBiasTracker:
    """Satu instance per-symbol. `start()` sekali; `snapshot()` kapan pun.

    `interval` menentukan timeframe bias (default dari HTF_BIAS_CONFIG) dan
    bisa diganti runtime lewat `set_interval()`.
    """

    LIMIT        = 120        # 120 bar HTF (≈ 5 hari @1h, 20 hari @4h)
    EMA_FAST     = 20
    EMA_SLOW     = 50
    ATR_PERIOD   = 14
    MIN_READY    = 55         # bar minimum sebelum bias dianggap valid
    REFRESH_SEC  = 300        # refresh tiap 5 menit (bar HTF berubah lambat)
    FLAT_BAND    = 0.15       # |bias| di bawah ini = FLAT

    def __init__(self, symbol: str = "__default__",
                 interval: Optional[str] = None):
        self.symbol = symbol
        self.interval = (interval or HTF_BIAS_CONFIG["interval"]).lower()
        self._cached: Dict[str, Any] = self._empty()
        self._stop_ev = threading.Event()
        self._wake = threading.Event()   # bangunkan worker (stop / ganti interval)
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"htf-bias-{self.symbol}")
        self._thread.start()

    def stop(self):
        self._stop_ev.set()
        self._wake.set()

    def set_interval(self, interval: str):
        """Ganti timeframe bias runtime; worker langsung refresh ulang."""
        interval = (interval or "").lower()
        if not interval or interval == self.interval:
            return
        self.interval = interval
        # Reset cache supaya konsumen tidak memakai bias interval lama saat
        # data interval baru belum tiba.
        self._cached = self._empty()
        self._wake.set()

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._cached)

    # ── Worker ────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_ev.is_set():
            try:
                self._refresh()
            except Exception as e:
                logger.warning("Bias HTF %s %s gagal refresh: %s",
                               self.interval, self.symbol, e)
            # Tidur sampai REFRESH_SEC atau dibangunkan (stop / ganti interval)
            self._wake.wait(self.REFRESH_SEC)
            self._wake.clear()

    def _refresh(self):
        interval = self.interval   # snapshot: aman bila diganti saat berjalan
        sym = self.symbol.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        url = _BINANCE_KLINES.format(symbol=sym, interval=interval,
                                     limit=self.LIMIT)
        with urllib.request.urlopen(url, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
        # Interval diganti saat fetch berlangsung → buang hasil interval lama
        # supaya tidak menimpa cache kosong yang menunggu interval baru.
        if interval != self.interval:
            return
        # Baris terakhir = bar yang masih terbentuk — tetap dipakai (bias HTF
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
            "interval": interval,
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
            logger.info("Bias HTF %s %s siap: %s (%+.2f)", interval, sym,
                        self._cached["trend"], bias)

    def _empty(self, n_bars: int = 0) -> Dict[str, Any]:
        return {"ready": False, "interval": self.interval, "n_bars": n_bars,
                "bias": 0.0, "trend": "FLAT", "ema_fast": 0.0, "ema_slow": 0.0,
                "atr_4h": 0.0, "last_close": 0.0, "ts": time.time()}
