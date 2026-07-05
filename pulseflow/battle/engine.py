"""
Battle State Engine
===================

Mengubah metrik mikrostruktur (delta, aggression, velocity, whale, liquidation)
menjadi sebuah *Battlefield Object Model* — representasi pertempuran antara
pasukan Buyers dan Sellers yang langsung bisa dibaca dalam satu detik.

    Raw Market Data → Microstructure Engine → [Battle State Engine] → Visual Renderer

Engine ini murni logika (tanpa Qt). Stateful per-symbol: ia menghaluskan nilai,
menjaga momentum, menjalankan state machine pertempuran, dan men-spawn event
spesial (whale / likuidasi). Output adalah satu dict yang dikonsumsi UI.

Catatan desain: PulseFlow tidak menyimpan kedalaman orderbook mentah, jadi
"Fortress" (tembok likuiditas) diturunkan dari ABSORPSI — yaitu selisih antara
tekanan order flow dengan pergerakan harga aktual. Ketika satu sisi menyerang
agresif tapi harga tidak bergerak, sisi lawan dianggap sedang menahan tembok.
"""

import math
import time
from enum import Enum
from typing import Dict, Any, List, Optional

from pulseflow.config.settings import WHALE_THRESHOLDS_USD, THRESHOLDS


# ── State pertempuran ─────────────────────────────────────────────────────────

class BattleState(Enum):
    CEASEFIRE    = "CEASEFIRE"      # pasar sepi, gencatan senjata
    BUILDUP      = "BUILDUP"        # 🟡 velocity rendah, delta meningkat
    ASSAULT      = "ASSAULT"        # 🟠 aggression tinggi, delta tinggi
    ABSORPTION   = "ABSORPTION"     # 🔵 aggression tinggi, harga tidak bergerak
    BREAKTHROUGH = "BREAKTHROUGH"   # 🟢 tembok hancur, ekspansi harga
    EXHAUSTION   = "EXHAUSTION"     # 🔴 aggression turun, volume turun


STATE_COLORS = {
    BattleState.CEASEFIRE:    "#7d7d8e",
    BattleState.BUILDUP:      "#f5d020",
    BattleState.ASSAULT:      "#f59e0b",
    BattleState.ABSORPTION:   "#3b82f6",
    BattleState.BREAKTHROUGH: "#10b981",
    BattleState.EXHAUSTION:   "#f43f5e",
}

STATE_EMOJI = {
    BattleState.CEASEFIRE:    "⬜",
    BattleState.BUILDUP:      "🟡",
    BattleState.ASSAULT:      "🟠",
    BattleState.ABSORPTION:   "🔵",
    BattleState.BREAKTHROUGH: "🟢",
    BattleState.EXHAUSTION:   "🔴",
}


# ── Helper numerik ────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _ema(prev: float, new: float, alpha: float) -> float:
    return prev + alpha * (new - prev)


class BattleStateEngine:
    """
    Satu instance per-symbol. Panggil `update(metrics, price)` tiap tick (10 Hz).
    Mengembalikan Battlefield Object Model.
    """

    # Tuning konstanta (lihat docstring tiap blok)
    FRONTLINE_STEP  = 1.2     # gain dorongan per tick
    FRONTLINE_DECAY = 0.99    # reversi pelan ke netral (territory perlahan pulih)
    STRENGTH_ALPHA  = 0.22    # smoothing kekuatan pasukan
    STRENGTH_SLOW   = 0.04    # EMA lambat untuk menghitung momentum
    WALL_ALPHA      = 0.16    # smoothing tembok benteng
    EVENT_TTL       = 3.0     # umur event spesial (detik) sebelum hilang
    WHALE_COOLDOWN  = 2.0
    LIQ_COOLDOWN    = 2.0

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol

        # Kekuatan pasukan (0-100) dan EMA lambat untuk momentum
        self.buyer_strength = 0.0
        self.seller_strength = 0.0
        self._buyer_slow = 0.0
        self._seller_slow = 0.0

        # Frontline integrator (-100 seller .. +100 buyer)
        self.frontline = 0.0
        self._prev_frontline = 0.0

        # Benteng (tembok likuiditas via absorpsi)
        self.buy_wall = 0.0
        self.sell_wall = 0.0

        # Price EMA untuk momentum harga ter-arah (signed)
        self._price_fast: Optional[float] = None
        self._price_slow: Optional[float] = None

        # State machine
        self.state = BattleState.CEASEFIRE
        self.state_ticks = 0
        self.peak_agg = 0.0

        # Event spesial
        self.events: List[Dict[str, Any]] = []
        self._last_whale_ts = 0.0
        self._last_liq_ts = 0.0
        self._last_whale_mag = 0.0

    # ── Entry point ───────────────────────────────────────────────────────────

    def update(self, metrics: Dict[str, Any], price: float = 0.0) -> Dict[str, Any]:
        now = time.time()

        agg = float(metrics.get("aggression_score", 0.0))
        fz5 = metrics.get("filtered_z_scores", {}).get("5s", {})
        fz30 = metrics.get("filtered_z_scores", {}).get("30s", {})
        z5 = metrics.get("z_scores", {}).get("5s", {})
        ext = metrics.get("extended", {})
        flow = metrics.get("flow_composition", {})

        delta_z = float(fz5.get("delta_velocity_z", 0.0))          # signed
        vol_z30 = abs(float(fz30.get("volume_velocity_z", 0.0)))   # magnitude
        intensity = agg / 100.0

        # ── Dorongan order flow & harga (signed, -1..1) ───────────────────────
        flow_push = math.tanh(delta_z / 1.5)
        price_push = self._price_momentum(price)
        combined = 0.6 * flow_push + 0.4 * price_push   # arah pertempuran

        # ── Frontline (Layer 2 — fitur inti) ──────────────────────────────────
        # Integrator: tekanan satu arah yang berkelanjutan mendorong garis dan
        # menahannya (territory). Pasar seimbang menjaga garis di tengah.
        self._prev_frontline = self.frontline
        self.frontline = _clamp(
            self.frontline * self.FRONTLINE_DECAY + combined * intensity * self.FRONTLINE_STEP * 100.0,
            -100.0, 100.0,
        )
        frontline_speed = abs(self.frontline - self._prev_frontline)

        # ── Kekuatan pasukan (Layer 1) ────────────────────────────────────────
        balance = math.tanh(delta_z / 1.5)              # -1 seller .. +1 buyer
        buyer_target = agg * (0.5 + 0.5 * balance)
        seller_target = agg * (0.5 - 0.5 * balance)
        self.buyer_strength = _ema(self.buyer_strength, buyer_target, self.STRENGTH_ALPHA)
        self.seller_strength = _ema(self.seller_strength, seller_target, self.STRENGTH_ALPHA)
        self._buyer_slow = _ema(self._buyer_slow, self.buyer_strength, self.STRENGTH_SLOW)
        self._seller_slow = _ema(self._seller_slow, self.seller_strength, self.STRENGTH_SLOW)

        buyer_mom = _clamp((self.buyer_strength - self._buyer_slow) * 5.0, -100.0, 100.0)
        seller_mom = _clamp((self.seller_strength - self._seller_slow) * 5.0, -100.0, 100.0)

        # ── Whale support → morale ────────────────────────────────────────────
        whale_delta = float(flow.get("whale_delta_usd_5s", 0.0))
        # Ambang adaptif per-symbol dari classifier (persentil rolling);
        # tabel statis hanya fallback saat metrics belum membawa nilainya.
        whale_thr = float(flow.get("whale_thr_large", 0.0)) or \
            WHALE_THRESHOLDS_USD["LARGE"].get(self.symbol, WHALE_THRESHOLDS_USD["LARGE"]["__default__"])
        whale_buy_support = 1.0 if whale_delta > whale_thr * 0.25 else 0.0
        whale_sell_support = 1.0 if whale_delta < -whale_thr * 0.25 else 0.0

        buyer_morale = _clamp(50.0 + self.frontline * 0.4 + whale_buy_support * 12.0 - whale_sell_support * 8.0, 0.0, 100.0)
        seller_morale = _clamp(50.0 - self.frontline * 0.4 + whale_sell_support * 12.0 - whale_buy_support * 8.0, 0.0, 100.0)

        # ── Benteng / tembok likuiditas (Layer 3, via absorpsi) ───────────────
        buy_intent = max(0.0, flow_push)
        sell_intent = max(0.0, -flow_push)
        price_up = max(0.0, price_push)
        price_dn = max(0.0, -price_push)
        # Buyers menyerang (buy_intent) tapi harga tak naik → sellers menahan sell wall
        sell_wall_target = _clamp((buy_intent - price_up) * intensity * 160.0, 0.0, 100.0)
        buy_wall_target = _clamp((sell_intent - price_dn) * intensity * 160.0, 0.0, 100.0)
        self.sell_wall = _ema(self.sell_wall, sell_wall_target, self.WALL_ALPHA)
        self.buy_wall = _ema(self.buy_wall, buy_wall_target, self.WALL_ALPHA)
        wall_max = max(self.buy_wall, self.sell_wall)
        price_move = abs(price_push)

        # ── State machine pertempuran ─────────────────────────────────────────
        self._advance_state(agg, abs(delta_z), wall_max, price_move, frontline_speed)

        # ── Event spesial (Layer 4) ───────────────────────────────────────────
        self._spawn_events(now, flow, ext, whale_thr)
        self.events = [e for e in self.events if now - e["ts"] <= self.EVENT_TTL]

        # ── Sisi dominan & narasi (Layer 5) ───────────────────────────────────
        score = 0.6 * balance + 0.4 * (self.frontline / 100.0)
        if score > 0.12:
            dominant = "BUYERS"
        elif score < -0.12:
            dominant = "SELLERS"
        else:
            dominant = "NEUTRAL"

        narrative = self._build_narrative(
            dominant, agg, buyer_mom, seller_mom,
            whale_buy_support, whale_sell_support, price,
        )

        return {
            "buyer":  {"strength": self.buyer_strength, "momentum": buyer_mom, "morale": buyer_morale},
            "seller": {"strength": self.seller_strength, "momentum": seller_mom, "morale": seller_morale},
            "frontline": self.frontline,
            "fortress": {"buy_wall": self.buy_wall, "sell_wall": self.sell_wall},
            "state": self.state.value,
            "state_emoji": STATE_EMOJI[self.state],
            "state_color": STATE_COLORS[self.state],
            "dominant": dominant,
            "events": list(self.events),
            "narrative": narrative,
            "price": price,
            "aggression": agg,
            "ts": now,
        }

    # ── Momentum harga ter-arah ─────────────────────────────────────────────-

    def _price_momentum(self, price: float) -> float:
        """EMA cepat vs lambat → momentum harga signed, dinormalisasi tanh."""
        if price <= 0.0:
            return 0.0
        if self._price_fast is None:
            self._price_fast = price
            self._price_slow = price
            return 0.0
        self._price_fast = _ema(self._price_fast, price, 0.12)   # ~1 s
        self._price_slow = _ema(self._price_slow, price, 0.012)  # ~8 s
        if self._price_slow <= 0.0:
            return 0.0
        pct = (self._price_fast - self._price_slow) / self._price_slow
        return math.tanh(pct * 250.0)   # 0.4% drift ≈ tanh(1.0)

    # ── State machine ─────────────────────────────────────────────────────────

    def _advance_state(self, agg: float, dz: float, wall_max: float,
                       price_move: float, frontline_speed: float):
        prev = self.state
        self.state_ticks += 1
        S = BattleState

        if self.state == S.CEASEFIRE:
            if agg > 35.0:
                self.state = S.BUILDUP
                self.peak_agg = agg

        elif self.state == S.BUILDUP:
            self.peak_agg = max(self.peak_agg, agg)
            if agg > 62.0 and dz > 1.8:
                self.state = S.ASSAULT
            elif agg < 22.0:
                self.state = S.CEASEFIRE
                self.peak_agg = 0.0

        elif self.state == S.ASSAULT:
            self.peak_agg = max(self.peak_agg, agg)
            if wall_max > 55.0 and price_move < 0.25 and agg > 55.0:
                self.state = S.ABSORPTION
            elif abs(self.frontline) > 60.0 and price_move > 0.40:
                self.state = S.BREAKTHROUGH
            elif agg < self.peak_agg * 0.6 and agg < 50.0:
                self.state = S.EXHAUSTION

        elif self.state == S.ABSORPTION:
            if price_move > 0.45 and abs(self.frontline) > 55.0:
                self.state = S.BREAKTHROUGH
            elif agg < self.peak_agg * 0.55:
                self.state = S.EXHAUSTION
            elif wall_max < 32.0 and agg > 55.0:
                self.state = S.ASSAULT

        elif self.state == S.BREAKTHROUGH:
            if self.state_ticks > 20 and (agg < self.peak_agg * 0.6 or price_move < 0.20):
                self.state = S.EXHAUSTION

        elif self.state == S.EXHAUSTION:
            if agg < 20.0:
                self.state = S.CEASEFIRE
                self.peak_agg = 0.0
            elif agg > 60.0 and dz > 2.0:
                self.state = S.ASSAULT
                self.peak_agg = agg
            elif agg > 42.0:
                self.state = S.BUILDUP
                self.peak_agg = agg

        if self.state != prev:
            self.state_ticks = 0

    # ── Event spesial ─────────────────────────────────────────────────────────

    def _spawn_events(self, now: float, flow: Dict[str, Any],
                      ext: Dict[str, Any], whale_thr: float):
        # Whale
        whale_delta = float(flow.get("whale_delta_usd_5s", 0.0))
        mag = abs(whale_delta)
        if (mag > whale_thr and now - self._last_whale_ts > self.WHALE_COOLDOWN
                and mag > self._last_whale_mag * 1.1):
            side = "BUY" if whale_delta > 0 else "SELL"
            self.events.append({
                "kind": "WHALE", "side": side, "icon": "🐋",
                "label": f"WHALE {side}", "usd": mag, "ts": now,
            })
            self._last_whale_ts = now
        self._last_whale_mag = max(mag, self._last_whale_mag * 0.9)

        # Likuidasi
        short_liq = float(ext.get("short_liq_usd_5s", 0.0))
        long_liq = float(ext.get("long_liq_usd_5s", 0.0))
        liq_thr = THRESHOLDS.get("liquidation_cascade_usd", 50000.0)
        if now - self._last_liq_ts > self.LIQ_COOLDOWN:
            if short_liq > liq_thr and short_liq >= long_liq:
                self.events.append({
                    "kind": "LIQ", "side": "SHORT", "icon": "☠",
                    "label": "SHORT LIQUIDATION", "usd": short_liq, "ts": now,
                })
                self._last_liq_ts = now
            elif long_liq > liq_thr:
                self.events.append({
                    "kind": "LIQ", "side": "LONG", "icon": "☠",
                    "label": "LONG LIQUIDATION", "usd": long_liq, "ts": now,
                })
                self._last_liq_ts = now

    # ── Narasi ────────────────────────────────────────────────────────────────

    def _build_narrative(self, dominant: str, agg: float,
                         buyer_mom: float, seller_mom: float,
                         whale_buy: float, whale_sell: float, price: float) -> Dict[str, Any]:
        S = BattleState
        attacker = dominant
        defender = "SELLERS" if dominant == "BUYERS" else "BUYERS" if dominant == "SELLERS" else "—"

        if self.state == S.CEASEFIRE:
            headline = "MARKET QUIET"
        elif self.state == S.BUILDUP:
            headline = f"{attacker} BUILDING UP" if attacker != "NEUTRAL" else "PRESSURE BUILDING"
        elif self.state == S.ASSAULT:
            headline = f"{attacker} CHARGING" if attacker != "NEUTRAL" else "TWO-SIDED ASSAULT"
        elif self.state == S.ABSORPTION:
            headline = f"{defender} ABSORBING" if defender != "—" else "ABSORPTION"
        elif self.state == S.BREAKTHROUGH:
            headline = f"{attacker} BREAKTHROUGH" if attacker != "NEUTRAL" else "BREAKOUT"
        else:  # EXHAUSTION
            headline = f"{attacker} EXHAUSTION" if attacker != "NEUTRAL" else "MOMENTUM FADING"

        if agg < 25:
            pressure = "LOW"
        elif agg < 50:
            pressure = "MEDIUM"
        elif agg < 75:
            pressure = "HIGH"
        else:
            pressure = "EXTREME"

        dom_mom = buyer_mom if dominant == "BUYERS" else seller_mom if dominant == "SELLERS" else 0.0
        if dom_mom > 6:
            momentum = "RISING"
        elif dom_mom < -6:
            momentum = "FADING"
        else:
            momentum = "STEADY"

        if dominant == "BUYERS":
            whale_support = "YES" if whale_buy else "NO"
        elif dominant == "SELLERS":
            whale_support = "YES" if whale_sell else "NO"
        else:
            whale_support = "—"

        if defender == "SELLERS":
            target = f"Sell Fortress {price:,.4g}" if price else "Sell Fortress"
        elif defender == "BUYERS":
            target = f"Buy Fortress {price:,.4g}" if price else "Buy Fortress"
        else:
            target = "—"

        # ── Conviction: breakout vs reversal probability ─────────────────────
        # Derived from the same battlefield state so the UI shows ONE number
        # instead of a wall of raw metrics.
        agg_n  = agg / 100.0
        fl     = abs(self.frontline) / 100.0                 # territory held
        wall   = max(self.buy_wall, self.sell_wall) / 100.0  # defensive wall
        whale_yes = 1.0 if whale_support == "YES" else 0.0
        mom_pos = min(1.0, max(0.0, dom_mom) / 18.0)

        if self.state in (S.ABSORPTION, S.EXHAUSTION):
            # Defender holding / attacker fading → odds favour a turn
            conviction_mode = "REVERSAL"
            conviction_prob = _clamp(
                100.0 * (0.40 * wall + 0.25 * agg_n + 0.20 * (1.0 - fl) + 0.15 * whale_yes),
                0.0, 92.0,
            )
        else:
            # Buildup / assault / breakthrough → odds favour continuation
            conviction_mode = "BREAKOUT"
            conviction_prob = _clamp(
                100.0 * (0.32 * agg_n + 0.30 * fl + 0.20 * whale_yes + 0.18 * mom_pos),
                0.0, 95.0,
            )

        return {
            "headline": headline,
            "pressure": pressure,
            "pressure_pct": int(round(agg)),
            "momentum": momentum,
            "whale_support": whale_support,
            "target": target,
            "color": STATE_COLORS[self.state],
            "conviction_mode": conviction_mode,
            "conviction_prob": int(round(conviction_prob)),
        }
