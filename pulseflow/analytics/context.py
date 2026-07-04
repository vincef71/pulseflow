"""
Market Context Tracker — lapisan konteks KLINES untuk Entry Signal Engine.

Masalah yang diselesaikan (analisa v3): semua syarat entry diturunkan dari
jendela tick 5 detik, sehingga keenam check bersifat "burst" berumur detik
dan hampir tidak pernah menyala bersamaan. Tracker ini menyediakan syarat
KONTEKS yang berubah lambat (menit–jam) dari bar 1 menit:

    • bias trend   — EMA20 vs EMA50 + slope, dinormalisasi ATR-1m → [-1, +1]
    • ATR 1m       — skala volatilitas struktural (untuk stop/target/jarak,
                     menggantikan ATR tick 5 detik yang terlalu sempit)
    • swing levels — pivot high/low (fractal) → support/resistance nyata

Sumber bar:
    1. Dibangun live dari trade stream (semua mode feed, termasuk replay).
    2. Seed sejarah via REST Binance /fapi/v1/klines (opsional, sekali di
       start) supaya konteks langsung siap tanpa menunggu ~1 jam warm-up.

Threading: `on_trade` dipanggil dari thread feed dan seed dari thread REST —
keduanya hanya menulis ke deque (thread-safe). Semua mutasi state terjadi di
`snapshot()`, yang dipanggil dari loop engine (satu thread).
"""

import json
import logging
import math
import threading
import time
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PulseFlow.Context")

_BINANCE_KLINES = ("https://fapi.binance.com/fapi/v1/klines"
                   "?symbol={symbol}&interval=1m&limit={limit}")


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _ema_series(vals: List[float], period: int) -> List[float]:
    k = 2.0 / (period + 1.0)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


class MarketContextTracker:
    """Satu instance per-symbol. `on_trade` dari feed, `snapshot` dari loop."""

    BAR_SECONDS   = 60
    EMA_FAST      = 20
    EMA_SLOW      = 50
    ATR_PERIOD    = 14
    MAX_BARS      = 400          # ~6.5 jam bar 1m
    MIN_READY     = 55           # bar minimum sebelum bias/ATR dianggap valid
    SWING_K       = 2            # fractal: pivot vs 2 bar kiri & kanan
    SWING_SCAN    = 180          # hanya scan pivot dari bar terakhir sekian
    SEED_LIMIT    = 400

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol
        self._trade_q: deque = deque(maxlen=60000)   # (ts, price) dari feed
        self._seed_q: deque = deque()                # batch bar dari thread REST

        self._bars: List[Dict[str, float]] = []      # bar 1m closed, urut waktu
        self._live: Optional[Dict[str, float]] = None

        self._cached: Dict[str, Any] = self._empty()

    # ── Ingest (thread feed / thread seed) ────────────────────────────

    def on_trade(self, price: float, size: float, ts: float):
        if price > 0:
            self._trade_q.append((float(ts) if ts and ts > 0 else time.time(),
                                  float(price)))

    def seed_history_async(self):
        """Fetch sejarah bar 1m di background (non-blocking, gagal = silent)."""
        threading.Thread(target=self._seed_history, daemon=True,
                         name=f"ctx-seed-{self.symbol}").start()

    def _seed_history(self):
        sym = self.symbol.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        url = _BINANCE_KLINES.format(symbol=sym, limit=self.SEED_LIMIT)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                rows = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"Context seed 1m gagal untuk {sym}: {e}")
            return
        bars = []
        for row in rows[:-1]:      # baris terakhir = bar yang masih terbentuk
            bars.append({
                "open_time": int(row[0]) // 1000,
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
            })
        if bars:
            self._seed_q.append(bars)
            logger.info(f"Context seed {sym}: {len(bars)} bar 1m siap digabung")

    # ── Snapshot (thread loop engine) ─────────────────────────────────

    def snapshot(self, price: float) -> Dict[str, Any]:
        self._drain_seed()
        self._drain_trades()
        out = dict(self._cached)
        out["price"] = float(price)
        return out

    def _drain_seed(self):
        merged = False
        while self._seed_q:
            seed = self._seed_q.popleft()
            by_time = {b["open_time"]: b for b in seed}
            # Bar hasil live-build menang atas seed (lebih akurat utk overlap)
            for b in self._bars:
                by_time[b["open_time"]] = b
            self._bars = sorted(by_time.values(),
                                key=lambda b: b["open_time"])[-self.MAX_BARS:]
            merged = True
        if merged:
            self._recompute()

    def _drain_trades(self):
        closed_any = False
        while self._trade_q:
            ts, p = self._trade_q.popleft()
            bt = int(ts // self.BAR_SECONDS) * self.BAR_SECONDS
            if self._live is None:
                self._live = {"open_time": bt, "open": p, "high": p,
                              "low": p, "close": p}
            elif bt > self._live["open_time"]:
                self._append_bar(self._live)
                closed_any = True
                self._live = {"open_time": bt, "open": p, "high": p,
                              "low": p, "close": p}
            elif bt == self._live["open_time"]:
                self._live["high"] = max(self._live["high"], p)
                self._live["low"] = min(self._live["low"], p)
                self._live["close"] = p
            # bt < live: trade telat lintas menit — abaikan
        if closed_any:
            self._recompute()

    def _append_bar(self, bar: Dict[str, float]):
        if self._bars and self._bars[-1]["open_time"] == bar["open_time"]:
            self._bars[-1] = dict(bar)
        else:
            self._bars.append(dict(bar))
            if len(self._bars) > self.MAX_BARS:
                self._bars = self._bars[-self.MAX_BARS:]

    # ── Indikator (full recompute — ≤400 bar, murah) ──────────────────

    def _recompute(self):
        bars = self._bars
        if len(bars) < self.EMA_SLOW + 3:
            self._cached = self._empty(n_bars=len(bars))
            return

        closes = [b["close"] for b in bars]
        ema_f = _ema_series(closes, self.EMA_FAST)
        ema_s = _ema_series(closes, self.EMA_SLOW)

        # Wilder ATR 1m
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        p = self.ATR_PERIOD
        if len(trs) <= p:
            atr = sum(trs) / len(trs)
        else:
            atr = sum(trs[:p]) / p
            for tr in trs[p:]:
                atr = (atr * (p - 1) + tr) / p

        # Bias trend: spread EMA + slope EMA-cepat, dinormalisasi ATR
        spread = ema_f[-1] - ema_s[-1]
        slope = ema_f[-1] - ema_f[-4] if len(ema_f) >= 4 else 0.0
        if atr > 0:
            bias = _clamp(0.6 * math.tanh(spread / (0.8 * atr))
                          + 0.4 * math.tanh(slope / (0.6 * atr)), -1.0, 1.0)
        else:
            bias = 0.0

        swing_highs, swing_lows = self._find_swings(bars, atr)

        self._cached = {
            "ready": len(bars) >= self.MIN_READY and atr > 0,
            "n_bars": len(bars),
            "bias": float(round(bias, 3)),
            "trend": ("UP" if bias > 0.15 else
                      "DOWN" if bias < -0.15 else "FLAT"),
            "ema_fast": float(ema_f[-1]),
            "ema_slow": float(ema_s[-1]),
            "atr_1m": float(atr),
            "swing_highs": swing_highs,
            "swing_lows": swing_lows,
            "last_close": float(closes[-1]),
            "ts": time.time(),
        }

    def _find_swings(self, bars, atr):
        """Pivot fractal (k bar kiri-kanan) dari bar terakhir; pivot yang
        berdempetan (< 0.3 ATR) di-merge, yang terbaru menang."""
        k = self.SWING_K
        scan = bars[-self.SWING_SCAN:]
        highs: List[float] = []
        lows: List[float] = []
        for i in range(k, len(scan) - k):
            hs = [scan[j]["high"] for j in range(i - k, i + k + 1)]
            ls = [scan[j]["low"] for j in range(i - k, i + k + 1)]
            if scan[i]["high"] >= max(hs):
                highs.append(scan[i]["high"])
            if scan[i]["low"] <= min(ls):
                lows.append(scan[i]["low"])

        def merge(levels: List[float]) -> List[float]:
            tol = 0.3 * atr if atr > 0 else 0.0
            out: List[float] = []
            for lv in levels:                      # urutan waktu: baru menang
                out = [o for o in out if abs(o - lv) > tol]
                out.append(lv)
            return sorted(out)[-24:]

        return merge(highs), merge(lows)

    @staticmethod
    def _empty(n_bars: int = 0) -> Dict[str, Any]:
        return {
            "ready": False, "n_bars": n_bars, "bias": 0.0, "trend": "FLAT",
            "ema_fast": 0.0, "ema_slow": 0.0, "atr_1m": 0.0,
            "swing_highs": [], "swing_lows": [], "last_close": 0.0,
            "ts": time.time(),
        }
