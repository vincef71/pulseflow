"""
Entry Signal Engine  (PulseFlow v4)
===================================

Lapisan KEPUTUSAN di atas semua engine analitik: menggabungkan battle state,
liquidity node, macro pool, whale flow, ATR, dan (baru di v4) konteks klines
menjadi satu verdict yang bisa dieksekusi:

    LONG / SHORT / WAIT  +  skor confluence 0-100  +  trade plan konkret
    (zona entry, stop loss, TP1/TP2, risk:reward)

Perubahan v4 (perbaikan "sinyal tidak pernah fire" di v3):

1.  **Skor GRADED, bukan biner.** Di v3 tiap check lulus/gagal — check yang
    99% dari ambang menyumbang 0 poin, sehingga 4-5 syarat jendela-5-detik
    harus melewati ambang penuh secara bersamaan (hampir mustahil). Sekarang
    tiap check menyumbang fraksi 0..1 × bobot.

2.  **Dua lapisan kecepatan.** Syarat KONTEKS (structure/room/timing, bobot
    54) diturunkan dari klines 1m + macro pool — berubah lambat dan menyala
    stabil berjam-jam saat kondisi benar. Syarat TRIGGER (flow/battle/whale,
    bobot 46) tetap dari orderflow tick. Trigger sendirian tidak pernah cukup
    untuk fire; konteks sendirian juga tidak — keduanya harus align.

3.  **Jendela whale 60 detik** (akumulasi bucket 5s), bukan rolling 5s yang
    menyala-padam tiap print keluar jendela.

4.  **ATR struktural** (1m, fallback 2.5× ATR tick) untuk jarak stop/target/
    structure/room — ATR tick 5 detik terlalu sempit untuk plan yang hidup.

5.  **Arah tidak lagi rapuh**: tick netral tidak me-reset counter stabilitas
    arah (hanya flip ke arah berlawanan yang me-reset), dan bias klines ikut
    menentukan arah kandidat.

6.  **Instrumentasi**: pass-rate & rata-rata fraksi tiap check di-log INFO
    tiap ~60 detik untuk kalibrasi ambang dari data nyata.

State machine anti-flicker (tetap):
    WAIT → FORMING (skor ≥ 45) → ACTIVE (skor ≥ 65, arah stabil, plan valid
    RR ≥ 1) → keluar via STOP / TP2 / skor drop / flip. Saat ACTIVE, harga
    plan DIBEKUKAN (dipakai overlay chart).

Murni logika (tanpa Qt). Satu instance per-symbol, panggil `update()` tiap
tick 100 ms dari loop engine.
"""

import logging
import math
import time
from collections import deque
from typing import Dict, Any, List, Optional, Tuple

from pulseflow.config.settings import WHALE_THRESHOLDS_USD

logger = logging.getLogger("PulseFlow.Entry")


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _ema(prev: float, new: float, alpha: float) -> float:
    return prev + alpha * (new - prev)


# Bobot checklist confluence (total 100).
# Trigger cepat (tick): flow + battle + whale = 46 → tidak bisa fire sendiri.
# Konteks lambat (klines/pool): structure + room + timing = 54.
WEIGHTS = {
    "flow":      18.0,
    "battle":    14.0,
    "whale":     14.0,
    "structure": 18.0,
    "room":      16.0,
    "timing":    20.0,
}

CHECK_NAMES = {
    "flow":      "FLOW",
    "battle":    "BATTLE",
    "whale":     "WHALE",
    "structure": "STRUKTUR",
    "room":      "RUANG GERAK",
    "timing":    "TREND/TIMING",
}

# Fraksi minimum supaya check ditampilkan ✓ di UI (skor tetap pakai fraksi)
CHECK_OK_FRAC = 0.55

_REGIME_FRAC = {"dead": 0.0, "normal": 0.7, "active": 1.0,
                "aggressive": 1.0, "extreme": 0.85}


class EntrySignalEngine:
    """Satu instance per-symbol. Panggil `update(...)` tiap tick (10 Hz)."""

    SCORE_ALPHA        = 0.25    # smoothing skor (EMA)
    FIRE_SCORE         = 65.0    # skor minimum untuk ACTIVE
    FORMING_SCORE      = 45.0    # skor minimum untuk FORMING
    DROP_SCORE         = 42.0    # ACTIVE gugur bila skor < ini cukup lama
    # Anti-churn (analisa 89 trade live 4-5 Jul): median durasi 14 s, trade
    # <15 s menyumbang −$19.55 dari net −$19.67 — fee 87% kerugian. Grace
    # 3→8 s, hold 8→15 s, cooldown 20→90 s memutus loop entry→FADED→re-entry
    # (siklus minimal ~31 s → ~105 s). STOP/TP2 tidak tersentuh — tetap instan.
    DROP_GRACE_SEC     = 8.0
    SIDE_STABLE_TICKS  = 8       # arah harus stabil ~0.8 s sebelum fire
    MIN_HOLD_SEC       = 15.0    # ACTIVE minimal hidup sekian detik
    REFIRE_COOLDOWN    = 90.0    # jeda setelah setup berakhir sebelum fire lagi
    MIN_RR             = 1.0     # plan valid hanya bila RR TP1 >= ini
    # Stop minimal % dari harga. Dinaikkan 0.05 → 0.5 (analisa paper 4 Jul):
    # stop 0.05% membuat notional ~2000× risk sehingga taker fee round-trip
    # (~0.1% notional) = 2R — mustahil profit. Di 0.5%, fee ≈ 0.2R.
    MIN_RISK_PCT       = 0.5

    # Manajemen posisi (analisa live 5 Jul: 24/47 FADED ditutup saat gross
    # profit; TP2 100% win; posisi >5m dibiarkan ke SL penuh = −$6.64).
    # Profit ≥ 0.5R → event PARTIAL (executor tutup 50% + SL exchange ke BE),
    # stop plan pindah ke entry, lalu sisa di-trail best ± mult×ATR-1m.
    # Setelah BE: FADED diabaikan (exit via trail/TP2/FLIP), status RUNNER.
    PARTIAL_AT_R       = 0.5     # trigger partial di 0.5× risk awal
    TRAIL_ATR_MULT     = 2.0     # trailing stop = best ± 2×s_atr

    WHALE_BUCKET_SEC   = 5.0     # sampling whale_delta_usd_5s → bucket
    WHALE_BUCKETS      = 12      # 12 × 5 s = jendela 60 detik

    DIAG_EVERY_TICKS   = 600     # log instrumentasi tiap ~60 s

    def __init__(self, symbol: str = "__default__"):
        self.symbol = symbol
        self._whale_thr = WHALE_THRESHOLDS_USD["LARGE"].get(
            symbol, WHALE_THRESHOLDS_USD["LARGE"]["__default__"])

        # Skor ter-smooth signed: >0 bias LONG, <0 bias SHORT
        self._sscore = 0.0
        self._side_ticks = 0
        self._last_side: Optional[str] = None

        # Whale flow 60 s (akumulasi bucket 5 s)
        self._whale_buckets: deque = deque(maxlen=self.WHALE_BUCKETS)
        self._whale_bucket_ts = 0.0

        # Setup aktif
        self.phase = "WAIT"                     # WAIT | FORMING | ACTIVE
        self.active_plan: Optional[Dict[str, Any]] = None
        self.active_side: Optional[str] = None
        self.active_setup = ""
        self.active_since = 0.0
        self._drop_since = 0.0
        self._ended_at = 0.0

        # Instrumentasi (pass-rate per check, untuk kalibrasi ambang)
        self._diag_n = 0
        self._diag_sided = 0
        self._diag_frac = {k: 0.0 for k in WEIGHTS}
        self._diag_phase = {"WAIT": 0, "FORMING": 0, "ACTIVE": 0}
        self._diag_fires = 0

    def reset(self):
        self._sscore = 0.0
        self._side_ticks = 0
        self._last_side = None
        self._whale_buckets.clear()
        self._whale_bucket_ts = 0.0
        self.phase = "WAIT"
        self.active_plan = None
        self.active_side = None
        self.active_setup = ""
        self.active_since = 0.0
        self._drop_since = 0.0
        self._ended_at = 0.0

    # ── Entry point ───────────────────────────────────────────────────

    def update(self, metrics: Dict[str, Any], battle: Optional[Dict[str, Any]],
               liquidity: Optional[Dict[str, Any]], macro: Optional[Dict[str, Any]],
               daily_atr: Optional[Dict[str, Any]], price: float,
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = time.time()
        battle = battle or {}
        liquidity = liquidity or {}
        macro = macro or {}
        daily_atr = daily_atr or {}
        context = context or {}

        # ── ATR struktural: 1m (klines) >> ATR tick 5 s ──────────────
        atr = metrics.get("atr", {}) or {}
        tick_atr = float(atr.get("value", 0.0))
        if tick_atr <= 0 and price > 0:
            tick_atr = price * 0.0015
        ctx_ready = bool(context.get("ready", False))
        atr_1m = float(context.get("atr_1m", 0.0))
        s_atr = atr_1m if (ctx_ready and atr_1m > 0) else 2.5 * tick_atr

        # ── Whale flow 60 s ──────────────────────────────────────────
        flow_comp = metrics.get("flow_composition", {})
        whale_5s = float(flow_comp.get("whale_delta_usd_5s", 0.0))
        # Ambang LARGE adaptif per-symbol; nilai init statis jadi fallback
        # sampai metrics membawa ambang dari classifier.
        self._whale_thr = float(flow_comp.get("whale_thr_large", 0.0)) or self._whale_thr
        if now - self._whale_bucket_ts >= self.WHALE_BUCKET_SEC:
            self._whale_buckets.append(whale_5s)
            self._whale_bucket_ts = now
        whale_60s = sum(self._whale_buckets) if self._whale_buckets else whale_5s
        whale_n60 = _clamp(whale_60s / max(self._whale_thr, 1.0), -1.0, 1.0)

        # ── Arah kandidat (konteks klines ikut menentukan) ───────────
        flow_energy = float(liquidity.get("flow_energy", 0.0))
        frontline = float(battle.get("frontline", 0.0))
        fz5 = metrics.get("filtered_z_scores", {}).get("5s", {})
        z5 = metrics.get("z_scores", {}).get("5s", {})
        delta_z = float(fz5.get("delta_velocity_z", 0.0) or z5.get("delta_velocity_z", 0.0))
        ctx_bias = float(context.get("bias", 0.0)) if ctx_ready else 0.0

        dir_raw = (0.30 * flow_energy
                   + 0.20 * (frontline / 100.0)
                   + 0.15 * math.tanh(delta_z / 2.0)
                   + 0.10 * whale_n60
                   + 0.25 * ctx_bias)
        side = "LONG" if dir_raw > 0.10 else "SHORT" if dir_raw < -0.10 else None

        # Stabilitas arah: hanya flip ke arah BERLAWANAN yang me-reset;
        # tick netral (side None) tidak menghukum (perbaikan flicker v3).
        if side is not None:
            if side == self._last_side:
                self._side_ticks += 1
            else:
                self._side_ticks = 0
                self._last_side = side

        # ── Checklist confluence graded untuk arah kandidat ──────────
        checks: List[Dict[str, Any]] = []
        warnings: List[str] = []
        setup_label = ""
        raw_score = 0.0

        if side is not None and price > 0:
            checks, setup_label, warnings = self._run_checks(
                side, metrics, battle, liquidity, macro, daily_atr, context,
                price, s_atr, flow_energy, whale_60s)
            raw_score = sum(c["weight"] * c["frac"] for c in checks)

            # Bonus: conviction battle engine searah & tinggi
            narr = battle.get("narrative", {})
            if (narr.get("conviction_mode") == "BREAKOUT"
                    and int(narr.get("conviction_prob", 0)) >= 60
                    and battle.get("dominant", "") == ("BUYERS" if side == "LONG" else "SELLERS")):
                raw_score = min(100.0, raw_score + 4.0)

        # ── Skor signed ter-smooth (anti flicker + handle flip) ──────
        signed_target = raw_score * (1.0 if side == "LONG" else -1.0 if side == "SHORT" else 0.0)
        self._sscore = _ema(self._sscore, signed_target, self.SCORE_ALPHA)
        smooth_side = "LONG" if self._sscore > 0 else "SHORT"
        score = abs(self._sscore)

        # ── State machine setup ───────────────────────────────────────
        new_fire = False
        status = ""

        if self.phase == "ACTIVE":
            status, ended = self._track_active(price, score, smooth_side, now, s_atr)
            if ended:
                self.phase = "WAIT"
                self.active_plan = None
                self.active_side = None
                self._ended_at = now
        if self.phase != "ACTIVE":
            in_cooldown = (now - self._ended_at) < self.REFIRE_COOLDOWN
            if (score >= self.FIRE_SCORE and side == smooth_side
                    and self._side_ticks >= self.SIDE_STABLE_TICKS
                    and not in_cooldown):
                plan = self._build_plan(smooth_side, price, s_atr, liquidity, macro, context)
                if plan is not None and plan["rr1"] >= self.MIN_RR:
                    self.phase = "ACTIVE"
                    self.active_plan = plan
                    self.active_side = smooth_side
                    self.active_setup = setup_label
                    self.active_since = now
                    self._drop_since = 0.0
                    new_fire = True
                elif plan is not None:
                    warnings.append(f"RR terlalu kecil ({plan['rr1']:.1f}) — plan ditahan")
                    self.phase = "FORMING"
                else:
                    self.phase = "FORMING"
            elif score >= self.FORMING_SCORE and side == smooth_side:
                self.phase = "FORMING"
            else:
                self.phase = "WAIT"

        if not ctx_ready:
            warnings.append(f"Konteks klines warm-up ({int(context.get('n_bars', 0))} bar)")

        self._diag_tick(side, checks, new_fire)

        # ── Output ────────────────────────────────────────────────────
        out_side = self.active_side if self.phase == "ACTIVE" else (smooth_side if score >= self.FORMING_SCORE else None)
        out_setup = self.active_setup if self.phase == "ACTIVE" else setup_label
        grade = self._grade(score)

        return {
            "ready": True,
            "phase": self.phase,
            "side": out_side,
            "score": int(round(score)),
            "grade": grade,
            "setup": out_setup,
            "checks": checks,
            "plan": dict(self.active_plan) if self.active_plan else None,
            "status": status,
            "warnings": warnings,
            "new_fire": new_fire,
            "price": float(price),
            "ts": now,
        }

    # ── Checklist graded ──────────────────────────────────────────────

    def _run_checks(self, side, metrics, battle, liquidity, macro, daily_atr,
                    context, price, s_atr, flow_energy, whale_60s):
        is_long = side == "LONG"
        sgn = 1.0 if is_long else -1.0
        checks: List[Dict[str, Any]] = []
        warnings: List[str] = []
        ctx_ready = bool(context.get("ready", False))

        def add(key, frac, detail):
            frac = _clamp(frac, 0.0, 1.0)
            checks.append({"key": key, "name": CHECK_NAMES[key],
                           "ok": frac >= CHECK_OK_FRAC, "frac": float(round(frac, 3)),
                           "detail": detail, "weight": WEIGHTS[key]})

        # 1. FLOW — flow_energy searah; kredit penuh di 0.20
        # (kalibrasi diag live 2026-07-04: |flow_energy| jarang > 0.2)
        fe = flow_energy * sgn
        add("flow", fe / 0.20, f"energy {flow_energy:+.2f}")

        # 2. BATTLE — state & dominan mendukung (graded per kualitas state)
        state = battle.get("state", "CEASEFIRE")
        dominant = battle.get("dominant", "NEUTRAL")
        my_army = "BUYERS" if is_long else "SELLERS"
        fort = battle.get("fortress", {})
        my_wall = float(fort.get("buy_wall" if is_long else "sell_wall", 0.0))

        setup_label = "MOMENTUM"
        battle_frac = 0.0
        if dominant == my_army and state == "BREAKTHROUGH":
            battle_frac, setup_label = 1.0, "BREAKOUT"
        elif dominant == my_army and state == "ASSAULT":
            battle_frac, setup_label = 0.9, "MOMENTUM"
        elif state == "ABSORPTION" and my_wall > 45.0:
            # lawan menyerang tapi tembok kita menahan → fade/reversal
            battle_frac, setup_label = 0.8, "REVERSAL"
        elif dominant == my_army and state == "BUILDUP":
            battle_frac, setup_label = 0.6, "BUILDUP"
        elif dominant == my_army:
            battle_frac = 0.3
        # Dominant sering NEUTRAL di market tipis → beri kredit parsial dari
        # frontline searah (kalibrasi diag live 2026-07-04)
        frontline = float(battle.get("frontline", 0.0))
        battle_frac = max(battle_frac,
                          0.5 * _clamp((frontline * sgn) / 60.0, 0.0, 1.0))
        add("battle", battle_frac, f"{state} · {dominant} · FL {frontline:+.0f}")

        # 3. WHALE — akumulasi paus 60 s searah; kredit penuh di 1× LARGE
        wn = (whale_60s * sgn) / max(self._whale_thr, 1.0)
        add("whale", wn, f"Δ60s ${whale_60s / 1000:+,.0f}K")

        # 4. STRUKTUR — pelindung di belakang entry: node likuiditas ATAU
        #    swing level klines, graded prob × kedekatan (jarak dlm ATR-1m)
        levels = liquidity.get("levels", []) or []
        protect_side = "BUY" if is_long else "SELL"
        struct_frac, struct_detail = 0.0, "tidak ada pelindung"
        for lv in levels:
            if lv.get("side") != protect_side or lv.get("prob", 0) < 50:
                continue
            d = abs(lv["price"] - price)
            if d > 1.5 * s_atr:
                continue
            f = (lv["prob"] / 100.0) * (1.0 - 0.5 * d / (1.5 * s_atr))
            if f > struct_frac:
                struct_frac = f
                struct_detail = f"{protect_side} node {lv['prob']:.0f}% @ {lv['price']:,.6g}"
        swings = context.get("swing_lows" if is_long else "swing_highs", []) if ctx_ready else []
        for sw in swings:
            d = (price - sw) * sgn
            if d <= 0 or d > 1.5 * s_atr:
                continue
            f = 0.75 * (1.0 - 0.5 * d / (1.5 * s_atr))
            if f > struct_frac:
                struct_frac = f
                struct_detail = f"swing 1m @ {sw:,.6g}"
        add("structure", struct_frac, struct_detail)

        # 5. RUANG GERAK — jarak ke penghalang lawan terdekat (dlm ATR-1m).
        #    Penuh bila jalur bersih ≥ 1 ATR; 0 bila tembok menempel.
        oppose_side = "SELL" if is_long else "BUY"
        nearest = None          # (dist, deskripsi)
        for lv in levels:
            if lv.get("side") != oppose_side or lv.get("prob", 0) < 60:
                continue
            d = (lv["price"] - price) * sgn
            if d > 0 and (nearest is None or d < nearest[0]):
                nearest = (d, f"tembok {oppose_side} {lv['prob']:.0f}% @ {lv['price']:,.6g}")
        for p_ in (macro.get("pools", []) or []):
            if p_.get("side") != oppose_side or p_.get("strength", 0) < 55:
                continue
            d = (p_["price"] - price) * sgn
            if d > 0 and (nearest is None or d < nearest[0]):
                nearest = (d, f"pool macro {oppose_side} @ {p_['price']:,.6g}")
        # Resistance/support klines juga penghalang — kecuali sedang BREAKOUT
        if ctx_ready and setup_label != "BREAKOUT":
            for sw in context.get("swing_highs" if is_long else "swing_lows", []):
                d = (sw - price) * sgn
                if d > 0 and (nearest is None or d < nearest[0]):
                    nearest = (d, f"swing 1m @ {sw:,.6g}")
        if nearest is None:
            add("room", 1.0, "jalur relatif bersih")
        else:
            # Kredit penuh bila jalur bersih ≥ 1 ATR-1m (1.5 terlalu ketat
            # untuk pair yang node-nya rapat — diag live 2026-07-04)
            room_frac = nearest[0] / (1.0 * s_atr)
            add("room", room_frac, nearest[1])
            if room_frac < 0.4:
                warnings.append(f"Penghalang lawan dekat: {nearest[1]}")

        # 6. TREND/TIMING — bias klines searah (50%) + regime hidup (25%)
        #    + range harian belum habis (25%)
        ctx_bias = float(context.get("bias", 0.0)) if ctx_ready else 0.0
        trend_frac = _clamp(0.5 + 1.2 * ctx_bias * sgn, 0.0, 1.0) if ctx_ready else 0.5
        regime = metrics.get("regime", "normal")
        regime_frac = _REGIME_FRAC.get(regime, 0.7)
        range_used = float(daily_atr.get("range_used_pct", 0.0))
        daily_ready = bool(daily_atr.get("ready", False))
        if not daily_ready:
            range_frac = 0.7
        elif range_used < 80.0:
            range_frac = 1.0
        elif range_used <= 120.0:
            range_frac = 1.0 - 0.7 * (range_used - 80.0) / 40.0
        else:
            range_frac = 0.2
        timing_frac = 0.5 * trend_frac + 0.25 * regime_frac + 0.25 * range_frac
        detail_t = f"trend {context.get('trend', '—') if ctx_ready else '—'} · regime {regime.upper()}"
        if daily_ready:
            detail_t += f" · range {range_used:.0f}%"
            if range_used >= 100.0:
                warnings.append(f"Range harian {range_used:.0f}% terpakai — hati-hati chasing")
        add("timing", timing_frac, detail_t)

        if regime == "dead":
            warnings.append("Volume mati — sinyal tidak reliabel")

        return checks, setup_label, warnings

    # ── Trade plan ────────────────────────────────────────────────────

    def _build_plan(self, side, price, s_atr, liquidity, macro,
                    context) -> Optional[Dict[str, Any]]:
        if price <= 0 or s_atr <= 0:
            return None
        is_long = side == "LONG"
        sgn = 1.0 if is_long else -1.0
        levels = liquidity.get("levels", []) or []
        ctx_ready = bool((context or {}).get("ready", False))

        # Anchor stop: pelindung terkuat di belakang harga
        # (node likuiditas prob ≥ 55, atau swing klines bila lebih dekat)
        protect_side = "BUY" if is_long else "SELL"
        anchors = [lv["price"] for lv in levels
                   if lv.get("side") == protect_side
                   and abs(lv["price"] - price) <= 1.5 * s_atr
                   and lv.get("prob", 0) >= 55]
        if ctx_ready:
            anchors += [sw for sw in context.get("swing_lows" if is_long else "swing_highs", [])
                        if 0 < (price - sw) * sgn <= 1.5 * s_atr]
        if anchors:
            anchor = min(anchors, key=lambda a: abs(price - a))  # terdekat di belakang
            stop = anchor - sgn * 0.35 * s_atr
            entry_far = anchor  # boleh isi sampai pelindung
        else:
            stop = price - sgn * 1.1 * s_atr
            entry_far = price - sgn * 0.25 * s_atr

        entry_near = price + sgn * 0.05 * s_atr
        entry_lo = min(entry_far, entry_near)
        entry_hi = max(entry_far, entry_near)
        entry_mid = (entry_lo + entry_hi) / 2.0

        risk = (entry_mid - stop) * sgn
        if risk <= 0:
            return None

        # Stop terlalu rapat tidak ekonomis (fee round-trip) → lebarkan
        risk_floor = price * self.MIN_RISK_PCT / 100.0
        if risk < risk_floor:
            risk = risk_floor
            stop = entry_mid - sgn * risk

        # TP1: node/swing lawan terdekat yang berjarak >= 1R; fallback 1.5 ATR
        oppose_side = "SELL" if is_long else "BUY"
        target_prices = [lv["price"] for lv in levels
                         if lv.get("side") == oppose_side and lv.get("prob", 0) >= 55]
        if ctx_ready:
            target_prices += list(context.get("swing_highs" if is_long else "swing_lows", []))
        targets = sorted([p for p in target_prices
                          if (p - entry_mid) * sgn >= self.MIN_RR * risk],
                         key=lambda p: (p - entry_mid) * sgn)
        tp1 = targets[0] if targets else entry_mid + sgn * max(1.5 * s_atr, 1.2 * risk)

        # TP2: pool macro terkuat searah dalam jangkauan wajar; fallback 2.8 ATR
        pools = [p for p in (macro.get("pools", []) or [])
                 if p.get("side") == oppose_side
                 and (p["price"] - entry_mid) * sgn > (tp1 - entry_mid) * sgn
                 and abs(p.get("distance_pct", 99.0)) <= 6.0
                 and p.get("strength", 0) >= 40]
        tp2 = max(pools, key=lambda p: p["strength"])["price"] if pools \
            else entry_mid + sgn * 2.8 * s_atr
        if (tp2 - tp1) * sgn <= 0:
            tp2 = tp1 + sgn * 1.0 * s_atr

        rr1 = (tp1 - entry_mid) * sgn / risk
        rr2 = (tp2 - entry_mid) * sgn / risk

        return {
            "side": side,
            "entry_lo": float(entry_lo),
            "entry_hi": float(entry_hi),
            "entry": float(entry_mid),
            "stop": float(stop),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "rr1": float(round(rr1, 2)),
            "rr2": float(round(rr2, 2)),
            "risk_pct": float(round(risk / entry_mid * 100.0, 3)),
            "tp1_hit": False,
            # State manajemen posisi (mutasi selama ACTIVE)
            "initial_stop": float(stop),   # untuk hitung R (stop bisa pindah)
            "best": float(entry_mid),      # maximum favorable excursion
            "be_moved": False,             # sudah partial + SL ke breakeven?
        }

    # ── Rebase plan ke harga fill exchange ────────────────────────────

    def rebase_active_plan(self, fill_price: float):
        """Dipanggil executor/runner setelah order live terisi: geser plan
        aktif ke harga fill sebenarnya (market order bisa slippage dari
        harga plan). Seluruh level digeser sebesar delta sehingga jarak
        risk/target (geometri R) dipertahankan — partial 0.5R, trailing,
        dan status STOP/TP dihitung dari entry yang benar-benar terjadi.
        Idempoten & aman dipanggil dari worker thread (assignment float
        atomik di bawah GIL; loop engine membaca nilai konsisten tick
        berikutnya)."""
        plan = self.active_plan
        if self.phase != "ACTIVE" or plan is None or fill_price <= 0:
            return
        delta = float(fill_price) - float(plan["entry"])
        if delta == 0.0:
            return
        for k in ("entry", "entry_lo", "entry_hi", "stop", "tp1", "tp2",
                  "initial_stop", "best"):
            if k in plan:
                plan[k] = float(plan[k]) + delta
        logger.info("[%s] plan di-rebase ke fill %s (slippage %+.6g)",
                    self.symbol, fill_price, delta)

    # ── Tracking setup aktif ──────────────────────────────────────────

    def _track_active(self, price, score, smooth_side, now,
                      s_atr: float = 0.0) -> Tuple[str, bool]:
        """Return (status, ended).

        Manajemen posisi: profit ≥ PARTIAL_AT_R × risk awal → event PARTIAL
        (one-shot, stop pindah ke breakeven), lalu sisa posisi di-trail
        best ± TRAIL_ATR_MULT × s_atr. Setelah BE, FADED tidak menutup —
        exit lewat TRAIL (stop ter-trail tersentuh), TP2, atau FLIP.
        """
        plan = self.active_plan
        if plan is None or price <= 0:
            return "", True
        sgn = 1.0 if plan["side"] == "LONG" else -1.0
        held = now - self.active_since
        be_moved = bool(plan.get("be_moved", False))

        # Best favorable price (MFE) — dasar trailing
        if (price - plan.get("best", price)) * sgn > 0:
            plan["best"] = float(price)

        # Stop tersentuh → keluar. Pasca-BE stop sudah pindah (BE/trail):
        # laporkan TRAIL supaya statistik memisahkannya dari stop awal.
        if (price - plan["stop"]) * sgn <= 0:
            return ("TRAIL" if be_moved else "STOP"), True

        # TP tracking
        if (price - plan["tp2"]) * sgn >= 0:
            return "TP2", True
        if not plan["tp1_hit"] and (price - plan["tp1"]) * sgn >= 0:
            plan["tp1_hit"] = True

        # Partial TP + breakeven (one-shot; SEBELUM trailing — skill
        # position-manager: jangan trail sebelum posisi aman di BE)
        risk = abs(plan["entry"] - plan.get("initial_stop", plan["stop"]))
        if (not be_moved and risk > 0
                and (plan["best"] - plan["entry"]) * sgn >= risk * self.PARTIAL_AT_R):
            plan["be_moved"] = True
            plan["stop"] = float(plan["entry"])   # SL → breakeven
            self._drop_since = 0.0
            return "PARTIAL", False

        # Trailing stop — hanya setelah BE; stop hanya bergerak searah profit
        if be_moved and s_atr > 0:
            trail = plan["best"] - sgn * self.TRAIL_ATR_MULT * s_atr
            if (trail - plan["stop"]) * sgn > 0:
                plan["stop"] = float(trail)

        # Arah flip keras — tetap menutup (pembalikan nyata, pre/pasca BE)
        if smooth_side != plan["side"] and score >= 40.0 and held >= self.MIN_HOLD_SEC:
            return "FLIP", True

        # Skor layu terlalu lama — hanya pre-BE (setup gagal, cut cepat).
        # Pasca-BE posisi runner: biarkan trail yang memutuskan exit.
        if be_moved:
            self._drop_since = 0.0
        elif score < self.DROP_SCORE:
            if self._drop_since == 0.0:
                self._drop_since = now
            elif now - self._drop_since > self.DROP_GRACE_SEC and held >= self.MIN_HOLD_SEC:
                return "FADED", True
        else:
            self._drop_since = 0.0

        if be_moved:
            return "RUNNER", False
        return "TP1" if plan["tp1_hit"] else "", False

    # ── Instrumentasi ─────────────────────────────────────────────────

    def _diag_tick(self, side, checks, new_fire):
        self._diag_n += 1
        self._diag_phase[self.phase] = self._diag_phase.get(self.phase, 0) + 1
        if new_fire:
            self._diag_fires += 1
        if side is not None and checks:
            self._diag_sided += 1
            for c in checks:
                self._diag_frac[c["key"]] += c["frac"]

        if self._diag_n >= self.DIAG_EVERY_TICKS:
            n, ns = self._diag_n, max(self._diag_sided, 1)
            fr = " ".join(f"{k}={self._diag_frac[k] / ns:.2f}" for k in WEIGHTS)
            logger.info(
                f"[{self.symbol}] entry-diag: sided {100 * self._diag_sided / n:.0f}% | "
                f"{fr} | forming {100 * self._diag_phase.get('FORMING', 0) / n:.0f}% "
                f"active {100 * self._diag_phase.get('ACTIVE', 0) / n:.0f}% "
                f"fires {self._diag_fires}")
            self._diag_n = 0
            self._diag_sided = 0
            self._diag_frac = {k: 0.0 for k in WEIGHTS}
            self._diag_phase = {"WAIT": 0, "FORMING": 0, "ACTIVE": 0}
            self._diag_fires = 0

    @staticmethod
    def _grade(score: float) -> str:
        if score >= 85.0:
            return "A+"
        if score >= 75.0:
            return "A"
        if score >= 65.0:
            return "B"
        if score >= 50.0:
            return "C"
        return "—"
