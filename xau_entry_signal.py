"""
XAU Entry Signal Scanner — XAUUSDT Binance Futures (TF 1H)
==========================================================

Pencari sinyal entry khusus copy-trading ke akun analyst TradersFamily:
  • Order type : LIMIT ONLY (entry di zona pullback, tidak pernah market)
  • Stop Loss  : 300 pips  (default 1 pip = $0.10  →  $30)
  • Take Profit: 500 pips  (default 1 pip = $0.10  →  $50, RR 1:1.67)

Metodologi (skill trading-technical-analysis):
  1. HMM regime detection multi-timeframe (1D / 4H / 1H, weighted voting)
  2. Guard: confidence >= 0.55, TF agreement >= 0.50, regime != Sideways
  3. Trigger 1H: WaveTrend cross dari zona ekstrem, displacement candle,
     atau retest order block — searah regime
  4. Entry limit di zona pullback (order block / EMA20 / offset ATR),
     SL & TP fixed pips dari harga entry
  5. Volatility guard: sinyal DITOLAK bila SL 300 pips < 1.0x ATR(1H)
     (stop pasti tersapu noise), warning bila < 1.5x ATR

Pemakaian:
    python xau_entry_signal.py                 # scan sekali
    python xau_entry_signal.py --watch         # rescan tiap candle 1H close
    python xau_entry_signal.py --pip-size 0.1 --sl-pips 300 --tp-pips 500
    python xau_entry_signal.py --test-telegram # uji koneksi Telegram
    python xau_entry_signal.py --no-telegram   # scan tanpa notifikasi

Telegram: sinyal LONG/SHORT otomatis dikirim (config dari env
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID atau telegram_config.json;
fallback ke F:\tradingbot\XAU\telegram_config.json yang sudah ada).

Output: laporan markdown di console + reports/xau_signal_*.md
        + riwayat sinyal reports/xau_signals.jsonl
"""

import argparse
import json
import logging
import math
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")                       # pandas / sklearn warnings
logging.getLogger("hmmlearn").setLevel(logging.ERROR)   # log "not converging"

# Console Windows default cp1252 tidak bisa print emoji laporan
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Konfigurasi ────────────────────────────────────────────────────────────

SYMBOL     = "XAUUSDT"
FAPI       = "https://fapi.binance.com"
ENTRY_TF   = "1h"
TICK_SIZE  = 0.01

# Syarat utama copy-trading (bisa dioverride via CLI)
PIP_SIZE   = 0.10        # 1 pip emas = $0.10  →  300 pips = $30
SL_PIPS    = 300
TP_PIPS    = 500

# Bobot HMM multi-timeframe (skill: 1d 0.40 / 4h 0.35 / 1h 0.15, dinormalisasi)
TF_WEIGHTS = {"1d": 0.40, "4h": 0.35, "1h": 0.15}
TF_LIMIT   = {"1d": 400, "4h": 400, "1h": 400}

MIN_CONFIDENCE = 0.55
MIN_AGREEMENT  = 0.50

# Limit order placement
PULLBACK_MIN_ATR = 0.10   # entry minimal sekian ATR di bawah/atas harga (biar benar2 limit)
PULLBACK_MAX_ATR = 1.00   # jangan lebih jauh dari ini (biar realistis terisi)
EXPIRY_CANDLES   = 6      # batalkan limit bila tak terisi dalam 6 candle (6 jam)

REPORT_DIR = Path(__file__).resolve().parent / "reports"

# Telegram: env var → telegram_config.json di sebelah script → config project XAU lama
TELEGRAM_CONFIG_PATHS = [
    Path(__file__).resolve().parent / "telegram_config.json",
    Path(r"F:\tradingbot\XAU\telegram_config.json"),
]
TELEGRAM_RESEND_COOLDOWN_H = 4   # --watch: jangan kirim ulang sinyal sisi sama < 4 jam


# ── Data ───────────────────────────────────────────────────────────────────

def get_ohlcv(symbol: str, interval: str, limit: int = 400) -> pd.DataFrame:
    """Klines USDS-M Futures (public endpoint, tanpa API key)."""
    r = requests.get(f"{FAPI}/fapi/v1/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=15)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df.set_index("timestamp", inplace=True)
    # buang candle yang masih berjalan → analisa hanya pakai candle CLOSED
    return df.iloc[:-1]


# ── Indikator (implementasi mandiri, tanpa pandas-ta) ──────────────────────

def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()   # Wilder


def wavetrend(df: pd.DataFrame, n1=10, n2=21) -> pd.DataFrame:
    ap = (df["high"] + df["low"] + df["close"]) / 3
    esa = ap.ewm(span=n1).mean()
    d = (ap - esa).abs().ewm(span=n1).mean()
    ci = (ap - esa) / (0.015 * d)
    df["wt1"] = ci.ewm(span=n2).mean()
    df["wt2"] = df["wt1"].rolling(4).mean()
    df["wt_cross_up"] = (df["wt1"] > df["wt2"]) & (df["wt1"].shift(1) <= df["wt2"].shift(1))
    df["wt_cross_down"] = (df["wt1"] < df["wt2"]) & (df["wt1"].shift(1) >= df["wt2"].shift(1))
    return df


def detect_displacement(df: pd.DataFrame, atr_multiplier=1.8) -> pd.DataFrame:
    """Displacement = body candle > N x ATR(14)."""
    body = (df["close"] - df["open"]).abs()
    df["is_displacement"] = body > atr_multiplier * atr(df, 14)
    df["displacement_bull"] = df["is_displacement"] & (df["close"] > df["open"])
    df["displacement_bear"] = df["is_displacement"] & (df["close"] < df["open"])
    return df


def detect_order_blocks(df: pd.DataFrame, lookback=5) -> pd.DataFrame:
    """Bullish OB = candle merah terakhir sebelum impulse naik (dan sebaliknya)."""
    n = len(df)
    ob_bull = np.zeros(n, bool)
    ob_bear = np.zeros(n, bool)
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    for i in range(lookback, n - 1):
        fut = c[i + 1:i + 1 + lookback]
        if len(fut) == 0:
            continue
        if c[i] < o[i] and fut.max() > h[i] * 1.002:
            ob_bull[i] = True
        if c[i] > o[i] and fut.min() < l[i] * 0.998:
            ob_bear[i] = True
    df["ob_bull"] = ob_bull
    df["ob_bear"] = ob_bear
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"])
    df["macd"], df["macd_sig"], df["macd_hist"] = macd(df["close"])
    df["atr"] = atr(df)
    df = wavetrend(df)
    df = detect_displacement(df)
    df = detect_order_blocks(df)
    return df


# ── HMM Regime (multi-timeframe, sesuai skill) ─────────────────────────────

def build_hmm_features(df: pd.DataFrame) -> np.ndarray:
    returns = df["close"].pct_change().fillna(0)
    log_vol = np.log1p(df["volume"]).diff().fillna(0)
    hl_range = (df["high"] - df["low"]) / df["close"]
    close_pos = (df["close"] - df["low"]) / (df["high"] - df["low"] + 1e-9)
    return np.column_stack([returns, log_vol, hl_range, close_pos])


def fit_hmm_single(df: pd.DataFrame, n_states=3, lookback=400) -> dict:
    from hmmlearn import hmm
    df = df.tail(lookback)
    feats = build_hmm_features(df)
    model = hmm.GaussianHMM(n_components=n_states, covariance_type="full",
                            n_iter=300, random_state=42, tol=1e-4)
    model.fit(feats)
    states = model.predict(feats)
    post = model.predict_proba(feats)

    ret = feats[:, 0]
    mean_ret = {s: ret[states == s].mean() if (states == s).any() else 0.0
                for s in range(n_states)}
    order = sorted(mean_ret, key=mean_ret.get)
    label = {order[0]: "Bearish", order[1]: "Sideways", order[2]: "Bullish"}

    last = post[-1]
    return {
        "regime_now": label[states[-1]],
        "regime_prob": {label[s]: round(float(last[s]), 4) for s in range(n_states)},
        "confidence": round(float(last.max()), 4),
    }


def detect_hmm_mtf(tf_data: dict) -> dict:
    per_tf, total_w = {}, 0.0
    scores = {"Bullish": 0.0, "Bearish": 0.0, "Sideways": 0.0}
    for tf, df in tf_data.items():
        if df is None or len(df) < 50:
            continue
        w = TF_WEIGHTS.get(tf, 0.10)
        res = fit_hmm_single(df)
        per_tf[tf] = res
        for reg, p in res["regime_prob"].items():
            scores[reg] += w * p
        total_w += w
    if total_w > 0:
        scores = {k: round(v / total_w, 4) for k, v in scores.items()}
    final = max(scores, key=scores.get)
    agree = (sum(1 for r in per_tf.values() if r["regime_now"] == final)
             / len(per_tf)) if per_tf else 0.0
    return {"regime": final, "confidence": scores[final],
            "agreement": round(agree, 2), "per_tf": per_tf,
            "weighted_score": scores}


# ── Sinyal + limit order plan ──────────────────────────────────────────────

def round_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


def find_ob_zone(df: pd.DataFrame, side: str, max_age=20):
    """Zona order block searah terdekat (untuk anchor limit entry)."""
    recent = df.iloc[-max_age:]
    col = "ob_bull" if side == "LONG" else "ob_bear"
    obs = recent[recent[col]]
    if obs.empty:
        return None
    row = obs.iloc[-1]
    # LONG: limit di high OB (batas atas zona); SHORT: di low OB
    return float(row["high"]) if side == "LONG" else float(row["low"])


def build_trigger(df: pd.DataFrame, regime: str) -> dict:
    """Evaluasi trigger 1H searah regime. Minimal 1 trigger utama harus nyala."""
    last = df.iloc[-1]
    trig = {"side": None, "primary": [], "supporting": [], "score": 0}

    if regime == "Bullish":
        side = "LONG"
        if bool(last["wt_cross_up"]) and last["wt1"] < -30:
            trig["primary"].append(f"WaveTrend cross UP dari oversold (WT1 {last['wt1']:.0f})")
        if bool(last["displacement_bull"]):
            trig["primary"].append("Displacement candle bullish")
        ob = find_ob_zone(df, "LONG")
        if (ob is not None and last["close"] > last["ema50"]
                and 0 <= last["close"] - ob <= 1.0 * last["atr"]):
            trig["primary"].append(f"Retest zona bullish order block @ {ob:,.2f}")
        if last["close"] > last["ema200"]:
            trig["supporting"].append("Harga di atas EMA200")
        if last["macd_hist"] > 0:
            trig["supporting"].append("MACD histogram positif")
        if 35 <= last["rsi"] <= 60:
            trig["supporting"].append(f"RSI {last['rsi']:.0f} — zona pullback sehat")
    elif regime == "Bearish":
        side = "SHORT"
        if bool(last["wt_cross_down"]) and last["wt1"] > 30:
            trig["primary"].append(f"WaveTrend cross DOWN dari overbought (WT1 {last['wt1']:.0f})")
        if bool(last["displacement_bear"]):
            trig["primary"].append("Displacement candle bearish")
        ob = find_ob_zone(df, "SHORT")
        if (ob is not None and last["close"] < last["ema50"]
                and 0 <= ob - last["close"] <= 1.0 * last["atr"]):
            trig["primary"].append(f"Retest zona bearish order block @ {ob:,.2f}")
        if last["close"] < last["ema200"]:
            trig["supporting"].append("Harga di bawah EMA200")
        if last["macd_hist"] < 0:
            trig["supporting"].append("MACD histogram negatif")
        if 40 <= last["rsi"] <= 65:
            trig["supporting"].append(f"RSI {last['rsi']:.0f} — zona pullback sehat")
    else:
        return trig

    if trig["primary"]:
        trig["side"] = side
    trig["score"] = len(trig["primary"]) * 2 + len(trig["supporting"])
    return trig


def build_limit_plan(df: pd.DataFrame, side: str,
                     pip_size: float, sl_pips: int, tp_pips: int) -> dict:
    """Susun limit order: entry pullback + SL/TP fixed pips dari entry."""
    last = df.iloc[-1]
    price = float(last["close"])
    atr_v = float(last["atr"])
    sgn = 1.0 if side == "LONG" else -1.0

    # Kandidat zona pullback: order block > EMA20 > offset 0.35 ATR
    candidates = []
    ob = find_ob_zone(df, side)
    if ob is not None:
        candidates.append(("Order block", ob))
    candidates.append(("EMA20", float(last["ema20"])))
    candidates.append(("Offset 0.35xATR", price - sgn * 0.35 * atr_v))

    lo = price - sgn * PULLBACK_MAX_ATR * atr_v   # batas terjauh
    hi = price - sgn * PULLBACK_MIN_ATR * atr_v   # batas terdekat
    valid = [(name, p) for name, p in candidates
             if (p - lo) * sgn >= 0 and (hi - p) * sgn >= 0]
    if valid:
        # pilih pullback TERDEKAT dari harga (peluang fill terbesar)
        anchor_name, entry = max(valid, key=lambda c: c[1] * sgn)
    else:
        anchor_name, entry = "Offset 0.35xATR", price - sgn * 0.35 * atr_v

    entry = round_tick(entry)
    sl_dist = sl_pips * pip_size
    tp_dist = tp_pips * pip_size
    stop = round_tick(entry - sgn * sl_dist)
    target = round_tick(entry + sgn * tp_dist)

    return {
        "side": side,
        "order_type": "LIMIT",
        "entry": entry,
        "entry_anchor": anchor_name,
        "stop": stop,
        "target": target,
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
        "sl_usd": round(sl_dist, 2),
        "tp_usd": round(tp_dist, 2),
        "rr": round(tp_dist / sl_dist, 2),
        "atr_1h": round(atr_v, 2),
        "sl_atr_ratio": round(sl_dist / atr_v, 2) if atr_v > 0 else 0.0,
        "distance_to_entry_usd": round(abs(price - entry), 2),
        "expiry_hours": EXPIRY_CANDLES,
    }


def scan(pip_size: float, sl_pips: int, tp_pips: int) -> dict:
    """Satu siklus scan penuh. Return dict hasil (untuk laporan + JSONL)."""
    tf_data = {tf: get_ohlcv(SYMBOL, tf, TF_LIMIT[tf]) for tf in TF_WEIGHTS}
    hmm_res = detect_hmm_mtf(tf_data)

    df1h = add_indicators(tf_data["1h"].copy())
    last = df1h.iloc[-1]
    price = float(last["close"])
    atr_v = float(last["atr"])

    out = {
        "symbol": SYMBOL, "tf": ENTRY_TF,
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "price": price, "atr_1h": round(atr_v, 2),
        "hmm": hmm_res,
        "indicators": {
            "rsi": round(float(last["rsi"]), 1),
            "wt1": round(float(last["wt1"]), 1),
            "wt2": round(float(last["wt2"]), 1),
            "macd_hist": round(float(last["macd_hist"]), 3),
            "ema20": round(float(last["ema20"]), 2),
            "ema50": round(float(last["ema50"]), 2),
            "ema200": round(float(last["ema200"]), 2),
        },
        "signal": "HOLD", "reasons": [], "warnings": [], "plan": None,
    }

    # ── Guard MTF ──────────────────────────────────────────────────
    conf, agree, regime = hmm_res["confidence"], hmm_res["agreement"], hmm_res["regime"]
    if regime == "Sideways":
        out["reasons"].append("Regime final Sideways — tidak entry, tunggu breakout")
        return out
    if conf < MIN_CONFIDENCE or agree < MIN_AGREEMENT:
        out["reasons"].append(
            f"Konflik antar TF (conf {conf:.0%}, agreement {agree:.0%}) — HOLD")
        return out

    # ── Trigger 1H ─────────────────────────────────────────────────
    trig = build_trigger(df1h, regime)
    if trig["side"] is None:
        out["reasons"].append(
            f"Regime {regime} (conf {conf:.0%}) tapi belum ada trigger 1H — tunggu")
        return out

    # ── Volatility guard vs SL fixed pips ─────────────────────────
    sl_dist = sl_pips * pip_size
    if atr_v > 0 and sl_dist < 1.0 * atr_v:
        out["reasons"].append(
            f"DITOLAK: SL {sl_pips} pips (${sl_dist:.0f}) < 1x ATR 1H (${atr_v:.0f}) — "
            "stop pasti tersapu noise, tunggu volatilitas turun")
        return out
    if atr_v > 0 and sl_dist < 1.5 * atr_v:
        out["warnings"].append(
            f"SL ${sl_dist:.0f} hanya {sl_dist / atr_v:.1f}x ATR — agak ketat, "
            "volatilitas sedang tinggi")

    # ── Susun limit order ──────────────────────────────────────────
    plan = build_limit_plan(df1h, trig["side"], pip_size, sl_pips, tp_pips)
    out["signal"] = trig["side"]
    out["plan"] = plan
    out["reasons"] = ([f"HMM MTF {regime} conf {conf:.0%}, agreement {agree:.0%}"]
                      + [f"Trigger: {t}" for t in trig["primary"]]
                      + [f"Support: {t}" for t in trig["supporting"]])
    return out


# ── Laporan ────────────────────────────────────────────────────────────────

def render_report(r: dict) -> str:
    hmm_res = r["hmm"]
    ind = r["indicators"]
    lines = [
        f"# 🥇 XAU Entry Signal — {r['symbol']} ({r['tf'].upper()})",
        f"**Waktu:** {r['time_utc']} UTC   |   **Harga:** {r['price']:,.2f}   |   "
        f"**ATR 1H:** ${r['atr_1h']:,.2f}",
        "",
        "## 🧠 HMM Regime — Multi-Timeframe",
        "| TF | Regime | Confidence |",
        "|----|--------|------------|",
    ]
    for tf in ("1d", "4h", "1h"):
        p = hmm_res["per_tf"].get(tf)
        if p:
            lines.append(f"| {tf.upper()} | {p['regime_now']} | {p['confidence']:.0%} |")
    ws = hmm_res["weighted_score"]
    lines += [
        "",
        f"**➤ Regime Final: {hmm_res['regime']}** — conf {hmm_res['confidence']:.0%}, "
        f"agreement {hmm_res['agreement']:.0%}",
        f"Weighted: Bullish={ws['Bullish']:.2f} | Bearish={ws['Bearish']:.2f} | "
        f"Sideways={ws['Sideways']:.2f}",
        "",
        "## 📊 Indikator 1H",
        f"- RSI(14): {ind['rsi']}  ·  WT1/WT2: {ind['wt1']}/{ind['wt2']}  ·  "
        f"MACD hist: {ind['macd_hist']}",
        f"- EMA 20/50/200: {ind['ema20']:,.2f} / {ind['ema50']:,.2f} / {ind['ema200']:,.2f}",
        "",
    ]

    plan = r.get("plan")
    if r["signal"] in ("LONG", "SHORT") and plan:
        icon = "🟢" if r["signal"] == "LONG" else "🔴"
        lines += [
            f"## 🎯 SINYAL: {icon} {r['signal']} — LIMIT ORDER",
            "",
            "| Field | Nilai |",
            "|-------|-------|",
            f"| Order | **{plan['side']} LIMIT** @ **{plan['entry']:,.2f}** |",
            f"| Anchor entry | {plan['entry_anchor']} "
            f"({plan['distance_to_entry_usd']:.2f} USD dari harga sekarang) |",
            f"| Stop Loss | **{plan['stop']:,.2f}**  ({plan['sl_pips']} pips = "
            f"${plan['sl_usd']:.2f}) |",
            f"| Take Profit | **{plan['target']:,.2f}**  ({plan['tp_pips']} pips = "
            f"${plan['tp_usd']:.2f}) |",
            f"| Risk:Reward | 1 : {plan['rr']:.2f} |",
            f"| SL vs ATR | {plan['sl_atr_ratio']:.1f}x ATR 1H |",
            f"| Expiry | batalkan bila belum fill dalam {plan['expiry_hours']} jam "
            f"atau harga lari > 1x ATR dari entry |",
            "",
        ]
    else:
        lines += ["## 🎯 SINYAL: ⏳ HOLD — tidak ada entry", ""]

    if r["reasons"]:
        lines += ["**Alasan:**"] + [f"- {x}" for x in r["reasons"]] + [""]
    if r["warnings"]:
        lines += ["**⚠ Peringatan:**"] + [f"- ⚠ {x}" for x in r["warnings"]] + [""]
    lines.append("> Disclaimer: alat bantu analisa, bukan saran finansial. "
                 "Selalu cek ulang sebelum menempatkan order di akun analyst.")
    return "\n".join(lines)


def save_outputs(r: dict, report: str):
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    (REPORT_DIR / f"xau_signal_{stamp}.md").write_text(report, encoding="utf-8")
    slim = dict(r)
    slim["hmm"] = {"regime": r["hmm"]["regime"], "confidence": r["hmm"]["confidence"],
                   "agreement": r["hmm"]["agreement"],
                   "weighted_score": r["hmm"]["weighted_score"]}
    with open(REPORT_DIR / "xau_signals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(slim) + "\n")


def ping():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ── Telegram ───────────────────────────────────────────────────────────────

def load_telegram_config() -> dict | None:
    """Env var TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID > telegram_config.json."""
    import os
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        return {"telegram_bot_token": token, "telegram_chat_id": chat}
    for p in TELEGRAM_CONFIG_PATHS:
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
                    return cfg
            except Exception:
                continue
    return None


def send_telegram(text: str) -> bool:
    cfg = load_telegram_config()
    if cfg is None:
        print("[WARN] Telegram config tidak ditemukan — sinyal tidak terkirim. "
              "Set env TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID atau buat telegram_config.json.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
            json={"chat_id": cfg["telegram_chat_id"], "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            print(f"[WARN] Telegram menolak pesan: {r.status_code} {r.text[:200]}")
        return ok
    except requests.RequestException as e:
        print(f"[WARN] Gagal kirim Telegram: {e}")
        return False


def format_telegram_signal(r: dict) -> str:
    """Pesan sinyal ringkas & siap eksekusi untuk ponsel."""
    plan = r["plan"]
    hmm_res = r["hmm"]
    icon = "🟢" if plan["side"] == "LONG" else "🔴"
    reasons = "\n".join(f"• {x}" for x in r["reasons"])
    warns = ("\n" + "\n".join(f"⚠ {x}" for x in r["warnings"])) if r["warnings"] else ""
    return (
        f"{icon} <b>XAUUSDT {plan['side']} — LIMIT ORDER</b>  (TF 1H)\n"
        f"🕐 {r['time_utc']} UTC · harga {r['price']:,.2f}\n"
        f"\n"
        f"📌 Entry : <b>{plan['entry']:,.2f}</b>  ({plan['entry_anchor']})\n"
        f"🛑 SL    : <b>{plan['stop']:,.2f}</b>  ({plan['sl_pips']} pips / ${plan['sl_usd']:.0f})\n"
        f"🎯 TP    : <b>{plan['target']:,.2f}</b>  ({plan['tp_pips']} pips / ${plan['tp_usd']:.0f})\n"
        f"⚖ RR 1:{plan['rr']:.2f} · SL {plan['sl_atr_ratio']:.1f}x ATR\n"
        f"⏳ Batalkan bila belum fill dalam {plan['expiry_hours']} jam "
        f"atau harga lari &gt; 1x ATR\n"
        f"\n"
        f"🧠 HMM {hmm_res['regime']} · conf {hmm_res['confidence']:.0%} · "
        f"agree {hmm_res['agreement']:.0%}\n"
        f"{reasons}{warns}"
    )


_last_tg_sent: dict = {}   # side -> timestamp (dedup di mode --watch)


def notify_telegram(r: dict) -> None:
    if r["signal"] not in ("LONG", "SHORT") or not r.get("plan"):
        return
    now = time.time()
    prev = _last_tg_sent.get(r["signal"], 0.0)
    if now - prev < TELEGRAM_RESEND_COOLDOWN_H * 3600:
        print(f"[INFO] Sinyal {r['signal']} sudah dikirim "
              f"{(now - prev) / 3600:.1f} jam lalu — skip resend (cooldown).")
        return
    if send_telegram(format_telegram_signal(r)):
        _last_tg_sent[r["signal"]] = now
        print("[INFO] Sinyal terkirim ke Telegram ✓")


# ── Main ───────────────────────────────────────────────────────────────────

def seconds_to_next_hour_close(buffer_sec: int = 20) -> float:
    now = time.time()
    nxt = (math.floor(now / 3600) + 1) * 3600 + buffer_sec
    return nxt - now


def main():
    ap = argparse.ArgumentParser(description="XAUUSDT 1H limit-order entry scanner")
    ap.add_argument("--watch", action="store_true",
                    help="rescan otomatis tiap candle 1H close")
    ap.add_argument("--pip-size", type=float, default=PIP_SIZE,
                    help=f"nilai 1 pip dalam USD (default {PIP_SIZE} → 300 pips = $30)")
    ap.add_argument("--sl-pips", type=int, default=SL_PIPS)
    ap.add_argument("--tp-pips", type=int, default=TP_PIPS)
    ap.add_argument("--no-telegram", action="store_true",
                    help="jangan kirim sinyal ke Telegram")
    ap.add_argument("--test-telegram", action="store_true",
                    help="kirim pesan uji ke Telegram lalu keluar")
    args = ap.parse_args()

    if args.test_telegram:
        ok = send_telegram("✅ <b>XAU Entry Scanner</b> — koneksi Telegram OK.\n"
                           "Sinyal entry XAUUSDT 1H akan dikirim ke chat ini.")
        print("Test Telegram:", "TERKIRIM ✓" if ok else "GAGAL ✗")
        return

    while True:
        try:
            result = scan(args.pip_size, args.sl_pips, args.tp_pips)
            report = render_report(result)
            print("\n" + report + "\n")
            save_outputs(result, report)
            if result["signal"] in ("LONG", "SHORT"):
                ping()
                if not args.no_telegram:
                    notify_telegram(result)
        except requests.RequestException as e:
            print(f"[ERROR] Gagal ambil data Binance: {e}")
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {e}")

        if not args.watch:
            break
        wait = seconds_to_next_hour_close()
        print(f"--watch aktif: scan berikutnya dalam {wait / 60:.0f} menit "
              f"(setelah candle 1H close)…")
        time.sleep(wait)


if __name__ == "__main__":
    main()
