"""
Macro Liquidity Pool Engine  (PulseFlow v2.2)
=============================================

Berbeda dari micro engine (battlefield 5–30 detik, ±2·ATR), engine ini mencari
**kolam likuiditas BESAR** yang menjadi *magnet harga* dalam horizon panjang
(menit–jam) dan bisa **jauh** dari harga sekarang (mis. harga $20, pool besar di
$17). Targetnya: "di mana kira-kira likuiditas yang besar".

Sumber bukti (multi-source confluence):
- **Deep order book** (REST depth limit=1000) → wall besar resting, bisa jauh
  (efektif untuk ALT; BTC/ETH deep-book sempit).
- **Volume profile** jangka panjang (HVN/POC) dari trade stream — decay LAMBAT.
- **Klaster likuidasi** (forceOrder, per-harga) — magnet kuat di crypto.
- **Round number** (level psikologis).

Pool diberi skor by UKURAN (notional) + **confluence** (berapa sumber setuju di
harga itu) + persistensi. Ini ranking magnitudo, bukan probabilitas microstructure.

Thread-safety: `on_*` (thread feed) append ke deque; `update()` (loop engine)
men-drain & menghitung — single-thread.
"""

import math
import time
from collections import deque, defaultdict
from typing import Dict, Any, List, Optional

import numpy as np

_GREEN = (16, 185, 129)
_RED   = (244, 63, 94)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class MacroLiquidityEngine:
    """Satu instance per-symbol. Panggil `update(price, daily_atr)` tiap tick."""

    TOP_N      = 8
    RANGE_FRAC = 0.20        # pertimbangkan pool dalam ±20% dari harga
    DECAY_VOL  = 0.99998     # half-life ~1 jam @10Hz
    DECAY_LIQ  = 0.99997
    PRUNE_MIN  = 1.0         # buang bin notional < ini setelah decay
    MIN_LIQ_USD = 5_000.0    # ambang minimum klaster likuidasi

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol
        self._trade_q: deque = deque()
        self._liq_q: deque = deque()
        self._deep = None                    # (bids, asks) snapshot terakhir

        self.vol_profile: Dict[float, float] = {}   # round(price) -> notional
        self.liq_cluster: Dict[float, float] = {}   # round(price) -> usd
        self._seen: Dict[float, int] = {}           # pool bin -> first tick (persistensi)

        self._dec: Optional[int] = None
        self._tick = 0
        self._last_price = 0.0

    # ── Ingest (thread feed) ──────────────────────────────────────────

    def on_trade(self, price: float, size: float, is_buyer_maker: bool, ts: float):
        if price > 0 and size > 0:
            self._trade_q.append((price, price * size))

    def on_liquidation(self, usd_value: float, side: str, price: float, ts: float):
        if price > 0 and usd_value > 0:
            self._liq_q.append((price, usd_value))

    def on_deep_depth(self, bids: list, asks: list, ts: float):
        if bids and asks:
            self._deep = (bids, asks)

    def reset(self):
        self._trade_q.clear(); self._liq_q.clear()
        self._deep = None
        self.vol_profile.clear(); self.liq_cluster.clear(); self._seen.clear()
        self._dec = None
        self._tick = 0
        self._last_price = 0.0

    # ── Helpers ───────────────────────────────────────────────────────

    def _key(self, price: float) -> float:
        return round(price, self._dec) if self._dec is not None else price

    def _round_levels(self, price: float, lo: float, hi: float) -> List[float]:
        if price <= 0:
            return []
        step = 10.0 ** (math.floor(math.log10(price)) - 1)   # 1 orde di bawah harga
        levels = []
        k = math.floor(lo / step)
        v = k * step
        while v <= hi and len(levels) < 60:
            if v > 0:
                levels.append(round(v, 12))
            v += step
        return levels

    # ── Entry point ───────────────────────────────────────────────────

    def update(self, price: float, daily_atr: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        self._tick += 1
        empty = {"pools": [], "poc": 0.0, "price": float(price),
                 "ready": False, "ts": time.time()}
        if price <= 0:
            return empty
        self._last_price = price
        if self._dec is None:
            self._dec = max(0, 5 - int(math.floor(math.log10(price))))

        # drain
        while self._trade_q:
            p, n = self._trade_q.popleft()
            k = self._key(p)
            self.vol_profile[k] = self.vol_profile.get(k, 0.0) + n
        while self._liq_q:
            p, u = self._liq_q.popleft()
            k = self._key(p)
            self.liq_cluster[k] = self.liq_cluster.get(k, 0.0) + u

        # decay + prune
        self._decay(self.vol_profile, self.DECAY_VOL)
        self._decay(self.liq_cluster, self.DECAY_LIQ)

        atr_val = 0.0
        if daily_atr:
            atr_val = float(daily_atr.get("atr", daily_atr.get("value", 0.0)) or 0.0)
        binsize = max(price * 0.0025, atr_val / 30.0) if atr_val > 0 else price * 0.0025
        lo, hi = price * (1 - self.RANGE_FRAC), price * (1 + self.RANGE_FRAC)

        pools = self._detect_pools(price, binsize, lo, hi)
        poc = max(self.vol_profile, key=self.vol_profile.get) if self.vol_profile else 0.0

        return {
            "pools": pools,
            "poc": float(poc),
            "price": float(price),
            "ready": bool(pools),
            "ts": time.time(),
        }

    def _decay(self, d: Dict[float, float], f: float):
        dead = []
        for k in d:
            v = d[k] * f
            if v < self.PRUNE_MIN:
                dead.append(k)
            else:
                d[k] = v
        for k in dead:
            del d[k]

    # ── Deteksi pool ──────────────────────────────────────────────────

    def _detect_pools(self, price, binsize, lo, hi) -> List[Dict[str, Any]]:
        # kandidat: (price, source, weight$).
        # Agregasi tiap sumber ke BIN KASAR dulu supaya likuiditas yang menyebar
        # (mis. HVN se-band) menonjol sebagai satu bin, bukan hilang per-tick.
        cands: List[tuple] = []

        def coarse(d_items):
            agg: Dict[float, float] = defaultdict(float)
            for p, w in d_items:
                if lo <= p <= hi:
                    agg[round(p / binsize) * binsize] += w
            return agg

        # HVN dari volume profile: bin dalam 50% dari POC (POC = magnet volume,
        # selalu lolos; tak pakai median karena band HVN yang rata akan tertolak).
        vol_c = coarse(self.vol_profile.items())
        if vol_c:
            poc_v = max(vol_c.values())
            total_v = sum(vol_c.values())
            # signifikan: dekat POC DAN memegang porsi nyata (saring trade tersebar
            # yang membuat tiap bin tampak seperti HVN).
            hvn_thr = max(0.5 * poc_v, 0.10 * total_v)
            for cb, v in vol_c.items():
                if v >= hvn_thr:
                    cands.append((cb, "HVN", v))

        # Deep walls
        if self._deep:
            bids, asks = self._deep
            lv = [(float(p), float(p) * float(q)) for p, q in (bids + asks) if float(q) > 0]
            wall_c = coarse(lv)
            if wall_c:
                arr = np.array(list(wall_c.values()))
                thr = max(float(np.percentile(arr, 92)), float(np.median(arr)) * 2.5)
                for cb, n in wall_c.items():
                    if n >= thr:
                        cands.append((cb, "WALL", n))

        # Klaster likuidasi
        liq_c = coarse(self.liq_cluster.items())
        for cb, v in liq_c.items():
            if v >= self.MIN_LIQ_USD:
                cands.append((cb, "LIQ", v))

        # Round numbers (struktural)
        for rp in self._round_levels(price, lo, hi):
            cands.append((rp, "ROUND", 0.0))

        if not cands:
            return []

        # merge by proximity → pool
        merge_band = binsize * 1.5
        cands.sort(key=lambda c: c[0])
        groups: List[Dict[str, Any]] = []
        for cp, src, w in cands:
            if groups and abs(groups[-1]["_p"] - cp) <= merge_band:
                g = groups[-1]
            else:
                g = {"_p": cp, "_wsum": 0.0, "_pw": 0.0,
                     "vol": 0.0, "wall": 0.0, "liq": 0.0, "round": False}
                groups.append(g)
            if src == "ROUND":
                g["round"] = True
            else:
                key = {"HVN": "vol", "WALL": "wall", "LIQ": "liq"}[src]
                g[key] = max(g[key], w)
                # anchor harga ke sumber $ (rata-rata berbobot notional)
                g["_pw"] += cp * w
                g["_wsum"] += w
            if g["_wsum"] == 0.0:        # pool round-only → anchor ke round price
                g["_p"] = cp

        # finalisasi harga pool
        for g in groups:
            if g["_wsum"] > 0:
                g["_p"] = g["_pw"] / g["_wsum"]

        # normalisasi per sumber (relatif antar pool)
        mv = max((g["vol"] for g in groups), default=0.0) or 1.0
        mw = max((g["wall"] for g in groups), default=0.0) or 1.0
        ml = max((g["liq"] for g in groups), default=0.0) or 1.0

        scored = []
        for g in groups:
            sources = []
            if g["wall"] > 0: sources.append("WALL")
            if g["vol"]  > 0: sources.append("HVN")
            if g["liq"]  > 0: sources.append("LIQ")
            if g["round"]:    sources.append("ROUND")
            if not sources:
                continue
            # pool round-only saja → terlalu lemah, lewati (butuh konfirmasi $)
            if sources == ["ROUND"]:
                continue

            wv, ww, wl = g["vol"] / mv, g["wall"] / mw, g["liq"] / ml
            wr = 1.0 if g["round"] else 0.0
            base = 0.40 * ww + 0.30 * wv + 0.20 * wl + 0.10 * wr
            conf = len(sources)
            mult = 1.0 + 0.20 * (conf - 1)

            pbin = round(g["_p"], max(0, self._dec - 1)) if self._dec else g["_p"]
            first = self._seen.setdefault(pbin, self._tick)
            persist = _clamp((self._tick - first) / 6000.0, 0.0, 1.0)   # ~10 mnt

            raw = base * mult * (0.7 + 0.3 * persist)
            scored.append((g, sources, raw))

        if not scored:
            return []
        maxraw = max(r for _, _, r in scored) or 1.0

        out = []
        for g, sources, raw in scored:
            pp = g["_p"]
            notional = g["vol"] + g["wall"] + g["liq"]
            sell = pp > price
            strength = float(round(100.0 * raw / maxraw, 1))
            out.append({
                "price": float(pp),
                "notional": float(notional),
                "strength": strength,
                "distance_pct": float(round((pp - price) / price * 100.0, 2)),
                "side": "SELL" if sell else "BUY",
                "type": "+".join(sources),
                "sources": sources,
                "label": self._label(sources, sell),
                "color": list(_RED if sell else _GREEN),
            })
        out.sort(key=lambda d: d["strength"], reverse=True)
        out = out[:self.TOP_N]
        # bersihkan _seen yang tak lagi jadi pool (hindari membengkak)
        if len(self._seen) > 400:
            self._seen.clear()
        return out

    @staticmethod
    def _label(sources: List[str], sell: bool) -> str:
        side = "Sell" if sell else "Buy"
        if "LIQ" in sources and len(sources) == 1:
            return "Liquidation Pool"
        if "WALL" in sources:
            return f"Major {side} Wall"
        if "HVN" in sources:
            return "High-Volume Node"
        return f"{side} Liquidity"
