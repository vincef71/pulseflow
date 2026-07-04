"""
Liquidity Memory Engine  (PulseFlow v2.1)
=========================================

Evolusi dari liquidity-detector spasial menjadi **memory engine**: tiap level
harga bukan lagi angka, melainkan **entitas hidup** (`LiquidityNode`) yang punya
sejarah — lahir, dikuatkan, **bermigrasi**, ditekan (pressure), melemah, dan mati.
Probabilitas dinilai dari **evolusi** node, bukan snapshot-nya.

    Raw (depth + trades) → [Liquidity Memory Engine: nodes] → ranked levels + field

Komponen utama:
- **LiquidityNode**: depth/replenishment/volume/absorption + memori temporal
  (touch, failed_breakout, reaction, pressure) + migration (anchor bergerak) +
  negative-evidence (phantom/fake-breakout).
- **flow_energy** (skalar global): momentum × direction-persistence dari delta/
  trade velocity, price-accel, aggression-slope, OI — menjawab "apakah market
  BENAR-BENAR menuju level itu".
- **Scoring net-evidence × agreement**: positive − negative, dikali koherensi bukti
  → kurangi false positive (bukan lagi Σ WᵢXᵢ linear).

Catatan: "probability" = confidence weight (heuristik), belum kalibrasi statistik.
Depth = snapshot 20-level @100ms (bukan order-by-order) → "spoof" hanya proxy
heuristik (phantom liquidity / fake breakout), dibobot konservatif.

Thread-safety: `on_trade`/`on_depth` (thread feed) hanya append ke deque; semua
hitungan node diproses single-thread di `update()` (loop engine).
"""

import math
import time
from collections import deque
from typing import Dict, Any, List, Optional

import numpy as np


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _ema(prev: float, new: float, alpha: float) -> float:
    return prev + alpha * (new - prev)


_GREEN = (16, 185, 129)
_RED   = (244, 63, 94)


class LiquidityNode:
    """Sebuah level likuiditas sebagai entitas hidup dengan sejarah."""

    __slots__ = (
        "id", "price", "birth_tick", "age", "last_reinforced",
        "depth", "replenishment", "volume", "absorption",
        "touch_count", "failed_breakout", "failed_rejection", "reaction",
        "pressure", "phantom", "fake_breakout",
        "_price_slow", "migration_velocity",
        "_depth_prev", "_fresh", "_in_band", "_entry_side", "strength",
    )

    def __init__(self, node_id: int, price: float, tick: int):
        self.id = node_id
        self.price = price
        self.birth_tick = tick
        self.age = 0
        self.last_reinforced = tick

        # Bukti (decay tiap tick)
        self.depth = 0.0
        self.replenishment = 0.0
        self.volume = 0.0
        self.absorption = 0.0

        # Memori temporal
        self.touch_count = 0
        self.failed_breakout = 0
        self.failed_rejection = 0
        self.reaction = 0.0
        self.pressure = 0.0

        # Negative evidence
        self.phantom = 0.0
        self.fake_breakout = 0.0

        # Migration
        self._price_slow = price
        self.migration_velocity = 0.0   # price-units / detik (ter-smooth)

        # Internal
        self._depth_prev = 0.0
        self._fresh = 0.0               # volume yang masuk tick ini (reset tiap tick)
        self._in_band = False
        self._entry_side = 0
        self.strength = 0.0

    def replenishment_rate(self) -> float:
        return self.replenishment / max(1.0, self.age / 10.0 + self.touch_count)


class LiquidityProbabilityEngine:
    """Satu instance per-symbol. Panggil `update(...)` tiap tick (10 Hz)."""

    # ── Grid tampilan ──────────────────────────────────────────────────
    NBINS     = 100
    SPAN_ATR  = 2.0
    REACH_ATR = 1.6
    PRICE_HIST = 600
    TOP_K     = 8
    MAX_NODES = 120

    # ── Decay bukti node (per tick) ────────────────────────────────────
    DEPTH_ALPHA   = 0.30
    DECAY_DEPTH   = 0.92
    DECAY_VOL     = 0.99
    DECAY_REB     = 0.985
    DECAY_ABSORB  = 0.985
    DECAY_PHANTOM = 0.97
    DECAY_FAKE    = 0.99
    DECAY_REACT   = 0.997
    PRESSURE_DECAY = 0.99
    PRESSURE_STEP  = 12.0
    BREAK_PENALTY  = 0.45
    MIGRATE_ALPHA  = 0.20
    SLOW_ALPHA     = 0.02
    SPAWN_DEPTH_MULT = 1.8

    # ── Bobot scoring ──────────────────────────────────────────────────
    WP = {"depth": 1.0, "reb": 1.5, "react": 0.7, "absorb": 0.7, "pressure": 0.8}
    WN = {"phantom": 1.1, "fake": 1.2, "exhaust": 0.8}

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol
        self._trade_q: deque = deque()
        self._depth_q: deque = deque(maxlen=4)

        self.nodes: List[LiquidityNode] = []
        self._next_id = 1

        self.prices: deque = deque(maxlen=self.PRICE_HIST)
        self._quantum = 0.0
        self._depth_ref = 0.0
        self._last_price = 0.0
        self._tick = 0
        self._ref: Dict[str, float] = {}        # running ref per dimensi (norm)

        # flow_energy history
        self._dz_hist: deque = deque(maxlen=30)   # delta-z signed
        self._agg_hist: deque = deque(maxlen=30)
        self._pv_hist: deque = deque(maxlen=30)
        self.flow_energy = 0.0

    # ── Ingest (thread feed) ──────────────────────────────────────────

    def on_trade(self, price: float, size: float, is_buyer_maker: bool, ts: float):
        if price > 0 and size > 0:
            self._trade_q.append((price, size, is_buyer_maker))

    def on_depth(self, bids: list, asks: list, ts: float):
        if bids and asks:
            self._depth_q.append((bids, asks))

    def reset(self):
        self._trade_q.clear()
        self._depth_q.clear()
        self.nodes = []
        self._next_id = 1
        self.prices.clear()
        self._quantum = 0.0
        self._depth_ref = 0.0
        self._last_price = 0.0
        self._tick = 0
        self._ref = {}
        self._dz_hist.clear()
        self._agg_hist.clear()
        self._pv_hist.clear()
        self.flow_energy = 0.0

    # ── Helpers ───────────────────────────────────────────────────────

    def _q(self, price: float) -> float:
        q = self._quantum if self._quantum > 0 else max(price * 1e-5, 1e-9)
        return round(price / q) * q

    def _infer_quantum(self, bids: list):
        if self._quantum > 0 or len(bids) < 2:
            return
        ps = sorted({float(p) for p, _ in bids}, reverse=True)
        diffs = [abs(ps[i] - ps[i + 1]) for i in range(len(ps) - 1) if ps[i] != ps[i + 1]]
        if diffs:
            self._quantum = min(diffs)

    def _nearest(self, price: float, band: float) -> Optional[LiquidityNode]:
        best, bestd = None, band
        for n in self.nodes:
            d = abs(n.price - price)
            if d <= bestd:
                best, bestd = n, d
        return best

    def _spawn(self, price: float) -> LiquidityNode:
        n = LiquidityNode(self._next_id, price, self._tick)
        self._next_id += 1
        self.nodes.append(n)
        return n

    # ── Ingest depth → nodes ──────────────────────────────────────────

    def _ingest_depth(self, bids: list, asks: list, band: float):
        self._infer_quantum(bids)
        levels = [(float(p), float(s)) for p, s in (bids + asks) if float(s) > 0]
        if not levels:
            return
        sizes = [s for _, s in levels]
        med = float(np.median(sizes))
        mean_sz = float(np.mean(sizes))
        self._depth_ref = (0.9 * self._depth_ref + 0.1 * mean_sz) if self._depth_ref else mean_sz

        for pl, sz in levels:
            node = self._nearest(pl, band)
            if node is None:
                # hanya spawn dari level yang MENONJOL (wall), bukan depth seragam
                if sz > med * self.SPAWN_DEPTH_MULT and len(self.nodes) < self.MAX_NODES:
                    node = self._spawn(pl)
                else:
                    continue
            node.depth = _ema(node.depth, sz, self.DEPTH_ALPHA)
            node.last_reinforced = self._tick
            # migrasi anchor menuju level depth yang menyuplai, DIBOBOT ukuran:
            # wall besar menarik anchor kuat, level seragam kecil nyaris tak →
            # anchor mengikuti benteng yang bergerak, bukan rata-rata noise.
            aw = self.MIGRATE_ALPHA * sz / (sz + max(node.depth, 1e-9))
            node.price = _ema(node.price, pl, min(aw, 0.5))

    # ── Ingest trade → nodes ──────────────────────────────────────────

    def _ingest_trade(self, price: float, size: float, band: float):
        notional = price * size
        node = self._nearest(price, band)
        if node is None:
            if len(self.nodes) >= self.MAX_NODES:
                return
            node = self._spawn(price)
        node.volume += notional
        node._fresh += notional
        node.absorption += notional        # volume diserap di level (decay cepat)
        node.last_reinforced = self._tick
        # replenishment/iceberg: trade menembus level yang depth-nya "tinggi"
        if self._depth_ref > 0 and node.depth > self._depth_ref:
            node.replenishment += notional * min(2.0, node.depth / self._depth_ref)

    # ── Migration, touch/breakout, decay, merge, prune ────────────────

    def _maintain_nodes(self, price: float, band: float, merge_dist: float):
        survivors: List[LiquidityNode] = []
        for n in self.nodes:
            n.age += 1

            # migration velocity (price-units/detik)
            n._price_slow = _ema(n._price_slow, n.price, self.SLOW_ALPHA)
            inst_mig = (n.price - n._price_slow) * 10.0   # per detik
            n.migration_velocity = _ema(n.migration_velocity, inst_mig, 0.2)

            # phantom: likuiditas yang PERGI tanpa dimakan trade (depth turun
            # sementara hampir tak ada eksekusi di level itu) → proxy spoof/pull.
            decline = n._depth_prev - n.depth
            if decline > 0 and n._fresh < 0.15 * self._depth_ref * max(price, 1.0):
                n.phantom += decline
            n._depth_prev = n.depth

            # touch / breakout state machine
            self._touch_logic(n, price, band)

            # decay
            n.depth         *= self.DECAY_DEPTH
            n.volume        *= self.DECAY_VOL
            n.replenishment *= self.DECAY_REB
            n.absorption    *= self.DECAY_ABSORB
            n.reaction      *= self.DECAY_REACT
            n.phantom       *= self.DECAY_PHANTOM
            n.fake_breakout *= self.DECAY_FAKE
            n.pressure      *= self.PRESSURE_DECAY
            n._fresh = 0.0

            # strength (untuk prune & field)
            n.strength = (n.depth + 1.2 * n.replenishment + 0.5 * n.absorption
                          + 0.3 * n.volume + 0.4 * n.reaction)
            survivors.append(n)

        # merge node yang berdekatan
        survivors.sort(key=lambda n: n.price)
        merged: List[LiquidityNode] = []
        for n in survivors:
            if merged and abs(merged[-1].price - n.price) < merge_dist:
                self._merge_into(merged[-1], n)
            else:
                merged.append(n)

        # prune: buang yang sangat lemah & lama tak di-reinforce
        kept = [n for n in merged
                if not (n.strength < 1e-9 and (self._tick - n.last_reinforced) > 30)]
        if len(kept) > self.MAX_NODES:
            kept.sort(key=lambda n: n.strength, reverse=True)
            kept = kept[:self.MAX_NODES]
        self.nodes = kept

    def _touch_logic(self, n: LiquidityNode, price: float, band: float):
        side = 1 if price > n.price else -1   # +1 harga di atas node
        in_band = abs(price - n.price) <= band
        if in_band and not n._in_band:
            n._in_band = True
            n.touch_count += 1
            n._entry_side = side
        elif (not in_band) and n._in_band:
            n._in_band = False
            if side == n._entry_side:
                # ditolak / ditahan → tensi naik
                n.reaction += 1.0
                n.pressure = min(100.0, n.pressure + self.PRESSURE_STEP)
                if n._entry_side < 0:
                    n.failed_breakout += 1   # uji dari atas, gagal turun tembus
                else:
                    n.failed_rejection += 1
            else:
                # menyeberang → tembus
                if n.depth > self._depth_ref > 0:
                    n.fake_breakout += n.depth   # depth tebal tapi mudah tembus
                n.strength *= self.BREAK_PENALTY
                n.pressure *= 0.3

    def _merge_into(self, dst: LiquidityNode, src: LiquidityNode):
        ws, wd = max(src.strength, 1e-9), max(dst.strength, 1e-9)
        dst.price = (dst.price * wd + src.price * ws) / (wd + ws)
        dst.depth = max(dst.depth, src.depth)
        dst.replenishment += src.replenishment
        dst.volume += src.volume
        dst.absorption += src.absorption
        dst.reaction = max(dst.reaction, src.reaction)
        dst.pressure = max(dst.pressure, src.pressure)
        dst.phantom += src.phantom
        dst.fake_breakout += src.fake_breakout
        dst.touch_count = max(dst.touch_count, src.touch_count)
        dst.failed_breakout = max(dst.failed_breakout, src.failed_breakout)
        dst.last_reinforced = max(dst.last_reinforced, src.last_reinforced)
        dst.birth_tick = min(dst.birth_tick, src.birth_tick)

    # ── flow_energy ───────────────────────────────────────────────────

    def _update_flow_energy(self, metrics: Dict[str, Any]):
        z5 = metrics.get("z_scores", {}).get("5s", {}) if metrics else {}
        dz = float(z5.get("delta_velocity_z", 0.0))      # signed
        tz = abs(float(z5.get("trade_velocity_z", 0.0)))
        agg = float(metrics.get("aggression_score", 0.0)) if metrics else 0.0
        pv = float(metrics.get("instantaneous", {}).get("price_velocity", 0.0)) if metrics else 0.0
        oiz = abs(float(metrics.get("extended", {}).get("oi_velocity_z", 0.0))) if metrics else 0.0

        self._dz_hist.append(dz)
        self._agg_hist.append(agg)
        self._pv_hist.append(pv)

        # slopes
        agg_slope = (self._agg_hist[-1] - self._agg_hist[0]) / max(1, len(self._agg_hist)) if len(self._agg_hist) > 3 else 0.0
        price_accel = (self._pv_hist[-1] - self._pv_hist[0]) / max(1, len(self._pv_hist)) if len(self._pv_hist) > 3 else 0.0

        momentum = (0.40 * math.tanh(abs(dz) / 2.0) +
                    0.18 * math.tanh(tz / 2.0) +
                    0.16 * math.tanh(abs(agg_slope) / 6.0) +
                    0.14 * math.tanh(abs(price_accel) * 5.0) +
                    0.12 * math.tanh(oiz / 2.0))

        signs = [1 if v > 0 else -1 if v < 0 else 0 for v in self._dz_hist]
        persistence = abs(sum(signs) / len(signs)) if signs else 0.0
        direction = 1.0 if sum(self._dz_hist) > 0 else -1.0 if sum(self._dz_hist) < 0 else 0.0

        target = direction * momentum * (0.4 + 0.6 * persistence)
        self.flow_energy = _clamp(_ema(self.flow_energy, target, 0.25), -1.0, 1.0)

    # ── Entry point ───────────────────────────────────────────────────

    def update(self, metrics: Dict[str, Any], battle: Optional[Dict[str, Any]],
               price: float, atr: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        self._tick += 1
        if price > 0:
            self._last_price = price
            self.prices.append(price)

        atr_val = float(atr.get("value", 0.0)) if atr else 0.0
        if atr_val <= 0:
            atr_val = max(price * 0.0015, 1e-9)
        band = max(2.0 * (self._quantum or price * 1e-5), 0.05 * atr_val)
        merge_dist = max(3.0 * (self._quantum or price * 1e-5), 0.08 * atr_val)

        while self._depth_q:
            b, a = self._depth_q.popleft()
            self._ingest_depth(b, a, band)
        while self._trade_q:
            p, s, _bm = self._trade_q.popleft()
            self._ingest_trade(p, s, band)

        # node di harga sekarang (active) + swing/pivot reaction
        if price > 0:
            self._update_swings(band)

        self._maintain_nodes(price, band, merge_dist)
        self._update_flow_energy(metrics)

        return self._compute_output(price, atr_val, atr, battle, metrics, band)

    def _update_swings(self, band: float):
        if len(self.prices) < 3:
            return
        a, b, c = self.prices[-3], self.prices[-2], self.prices[-1]
        pivot = b if (b > a and b > c) or (b < a and b < c) else None
        if pivot is None:
            return
        node = self._nearest(pivot, band)
        if node is None and len(self.nodes) < self.MAX_NODES:
            node = self._spawn(pivot)
        if node is not None:
            node.reaction += 1.0
            node.last_reinforced = self._tick

    # ── Scoring & output ──────────────────────────────────────────────

    def _norm(self, name: str, arr: np.ndarray) -> np.ndarray:
        if arr.size == 0:
            return arr
        cur = float(arr.max())
        ref = max(cur, self._ref.get(name, 0.0) * 0.999)
        self._ref[name] = ref
        if ref <= 1e-12:
            return np.zeros_like(arr)
        return np.clip(arr / ref, 0.0, 1.5)

    def _compute_output(self, price, atr_val, atr, battle, metrics, band) -> Dict[str, Any]:
        empty = {"levels": [], "field": {"prices": [], "probs": []},
                 "bias": "NEUTRAL", "flow_energy": float(self.flow_energy),
                 "price": float(price), "ready": False, "ts": time.time()}
        if price <= 0 or len(self.prices) < 5 or not self.nodes:
            return empty

        regime = atr.get("regime", "normal") if atr else "normal"
        nodes = self.nodes

        depth_a = np.array([n.depth for n in nodes])
        reb_a   = np.array([n.replenishment for n in nodes])   # decayed = recent
        react_a = np.array([n.reaction for n in nodes])
        absb_a  = np.array([n.absorption for n in nodes])
        prs_a   = np.array([n.pressure for n in nodes])
        phan_a  = np.array([n.phantom for n in nodes])
        fake_a  = np.array([n.fake_breakout for n in nodes])

        nd = self._norm("depth", depth_a)
        nr = self._norm("reb", reb_a)
        nrc = self._norm("react", react_a)
        nab = self._norm("absorb", absb_a)
        npr = self._norm("press", prs_a)
        nph = self._norm("phantom", phan_a)
        nfk = self._norm("fake", fake_a)

        positive = (self.WP["depth"] * nd + self.WP["reb"] * nr +
                    self.WP["react"] * nrc + self.WP["absorb"] * nab +
                    self.WP["pressure"] * npr)
        # exhaustion: tua + sering disentuh tapi replenishment rendah
        age_n = np.clip(np.array([n.age for n in nodes]) / 300.0, 0.0, 1.0)
        exhaust = np.clip(age_n - nr, 0.0, 1.0)
        negative = (self.WN["phantom"] * nph + self.WN["fake"] * nfk +
                    self.WN["exhaust"] * exhaust)

        net = np.clip(positive - negative, 0.0, None)

        # agreement: koherensi bukti positif (berapa dimensi aktif & searah)
        active = (nd > 0.25).astype(float) + (nr > 0.25).astype(float) + \
                 (nrc > 0.25).astype(float) + (nab > 0.25).astype(float) + \
                 (npr > 0.25).astype(float)
        agreement = 0.4 + 0.6 * (active / 5.0)
        score = net * agreement

        # flow gate: node searah flow_energy & dalam jangkauan → boost
        prices_arr = np.array([n.price for n in nodes])
        if abs(self.flow_energy) > 0.12:
            direction = 1.0 if self.flow_energy > 0 else -1.0
            reach = self.REACH_ATR * atr_val
            dist = (prices_arr - price) * direction
            within = np.clip(1.0 - np.abs(prices_arr - price) / max(reach, 1e-9), 0.0, 1.0)
            boost = np.where(dist > 0, within, 0.0)
            damp = np.where(dist < 0, within, 0.0)
            score = score * (1.0 + 0.5 * abs(self.flow_energy) * boost
                             - 0.2 * abs(self.flow_energy) * damp)

        agg = float(metrics.get("aggression_score", 30.0)) if metrics else 30.0
        regime_mu = {"calm": 0.85, "warming": 1.0, "normal": 1.0,
                     "elevated": 1.12, "explosive": 1.25}.get(regime, 1.0)
        mu = 1.05 * regime_mu - 0.25 * (agg / 100.0)
        sigma = 0.65
        probs = 100.0 * np.array([_normal_cdf((s - mu) / sigma) for s in score])

        # combine flow_energy + frontline → bias
        frontline = float(battle.get("frontline", 0.0)) if battle else 0.0
        combined = 0.6 * self.flow_energy + 0.4 * (frontline / 100.0)
        bias = "UP" if combined > 0.15 else "DOWN" if combined < -0.15 else "NEUTRAL"

        # ── levels = node (top-K) ──────────────────────────────────────
        idx = [i for i in range(len(nodes)) if probs[i] >= 45.0
               and abs(nodes[i].price - price) > band]
        idx.sort(key=lambda i: probs[i], reverse=True)
        idx = idx[:self.TOP_K]
        levels = []
        for i in idx:
            n = nodes[i]
            pr = float(min(95.0, round(probs[i])))
            sell = n.price > price
            mig = _clamp(n.migration_velocity / max(0.2 * atr_val, 1e-9), -1.0, 1.0)
            levels.append({
                "price": float(n.price),
                "prob": pr,
                "side": "SELL" if sell else "BUY",
                "label": self._label(pr, sell),
                "color": list(_RED if sell else _GREEN),
                "migration": float(round(mig, 3)),
                "pressure": float(round(min(100.0, n.pressure), 1)),
                "touch": int(n.touch_count),
            })
        levels.sort(key=lambda d: d["price"], reverse=True)

        # ── field (gaussian bump per node, utk overlay heatmap) ────────
        half = self.SPAN_ATR * atr_val
        pmin, pmax = price - half, price + half
        binsize = (pmax - pmin) / self.NBINS
        centers = pmin + (np.arange(self.NBINS) + 0.5) * binsize
        field = np.zeros(self.NBINS)
        sig_bins = 1.5
        for i, n in enumerate(nodes):
            if probs[i] < 20:
                continue
            idxf = (n.price - pmin) / binsize
            if -3 < idxf < self.NBINS + 3:
                field += probs[i] * np.exp(-0.5 * ((np.arange(self.NBINS) - idxf) / sig_bins) ** 2)
        field = np.clip(field, 0.0, 100.0)

        return {
            "levels": levels,
            "field": {"prices": [float(c) for c in centers],
                      "probs":  [float(v) for v in field]},
            "bias": bias,
            "regime": regime,
            "flow_energy": float(round(self.flow_energy, 3)),
            "price": float(price),
            "node_count": len(nodes),
            "ready": True,
            "ts": time.time(),
        }

    @staticmethod
    def _label(prob: float, sell: bool) -> str:
        if prob >= 75:
            return f"Strong {'Sell' if sell else 'Buy'} Liquidity"
        if prob >= 55:
            return "Moderate Resistance" if sell else "Moderate Support"
        return "Weak Interest"

    # ── Logging snapshot (utk kalibrasi parquet) ──────────────────────

    def snapshot_nodes(self, top_k: int = 12) -> List[Dict[str, Any]]:
        """Ringkasan node terkuat untuk di-log (fitur, outcome dilabeli offline)."""
        ns = sorted(self.nodes, key=lambda n: n.strength, reverse=True)[:top_k]
        out = []
        for n in ns:
            out.append({
                "node_id": n.id,
                "price": round(n.price, 8),
                "age": n.age,
                "strength": round(n.strength, 4),
                "depth": round(n.depth, 4),
                "replenishment_rate": round(n.replenishment_rate(), 4),
                "absorption": round(n.absorption, 4),
                "reaction": round(n.reaction, 3),
                "touch_count": n.touch_count,
                "failed_breakout": n.failed_breakout,
                "pressure": round(n.pressure, 2),
                "phantom": round(n.phantom, 4),
                "migration_velocity": round(n.migration_velocity, 8),
            })
        return out
