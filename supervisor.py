"""
PulseFlow Supervisor — laporan berkala + alert instan ke Telegram.

Jalan BERDAMPINGAN dengan run_headless.py (proses terpisah, service kedua).
Read-only terhadap trading: tidak pernah menempatkan/menutup order —
penyetelan bot tetap lewat control.json.

Dua pekerjaan:
  1. REPORT berkala (default tiap 60 menit) → Telegram:
     balance + PnL hari ini, posisi terbuka, statistik trade hari ini
     (per alasan exit), status runner (heartbeat, tick rate), bias 4H
     per symbol, dan isi control.json.
  2. ALERT instan (tail log runner):
     • order LIVE terisi / exit / PARTIAL
     • CRITICAL: DISARMED, DAILY-LIMIT, fail-safe, uncaught exception
     • feed macet, dan RUNNER MATI (heartbeat berhenti > 5 menit)

Setup Telegram (sekali):
  1. Chat @BotFather → /newbot → salin token.
  2. Isi .env:  TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
     Belum tahu chat id? Kirim pesan apa pun ke bot kamu, lalu:
         python supervisor.py --get-chat-id
  3. Tes:  python supervisor.py --once
     (tanpa token, report dicetak ke console — dry-run)

Pakai:
    python supervisor.py                     # report tiap 60 menit + alert
    python supervisor.py --interval 30       # report tiap 30 menit
    python supervisor.py --once              # satu report lalu keluar
    python supervisor.py --manage            # + manajemen rule-based

MODE MANAGE (opt-in, --manage) — menulis control.json tiap 5 menit
berdasarkan aturan eksplisit; setiap tindakan dikirim ke Telegram:
  R1  symbol berdarah   : PnL symbol hari ini ≤ −(pause-symbol-loss% ×
      balance, default 1.5%) → tambah ke symbols_paused
  R2  soft-stop harian  : PnL total hari ini ≤ −(soft-stop-loss% ×
      balance, default 2.5%) → armed=false (lapisan SEBELUM hard-limit
      runner di max_daily_loss_pct)
  R3  chop kambuhan     : chop-cooldown engine terpicu ≥ chop-day-limit
      kali (default 3) di satu symbol hari ini → pause symbol s.d. besok
  Reset harian: ganti hari → manage me-revert HANYA perubahannya sendiri
      (state di supervisor_state.json); pause/disarm manual-mu tak disentuh.

Yang TIDAK PERNAH dilakukan manage: menaikkan risk/leverage/limit,
me-re-arm yang kamu matikan manual, atau menempatkan order.
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("PulseFlow.Supervisor")

RUNNER_LOG = _ROOT / "pulseflow_headless.log"
CONTROL_FILE = _ROOT / "control.json"
LIVE_LOG = _ROOT / "live_trades.json"
PAPER_LOG = _ROOT / "paper_trades.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Heartbeat runner: "💓 ARMED | 9.6 tick/s (target 10) | SYM ... | ..."
_HB_RE = re.compile(
    r"^(?P<ts>\S+ \S+?),\d+ .*💓 (?P<state>[^|]+?) \| "
    r"(?P<rate>[\d.]+) tick/s \(target (?P<target>[\d.]+)\) \| (?P<syms>.+?) \|")
_HB_SYM_RE = re.compile(r"([A-Z0-9]{2,20}USDT) ([\d.,]+) \[")

# Pola alert instan (regex, label, cooldown detik anti-spam per label)
ALERT_PATTERNS = [
    (re.compile(r"🔴 LIVE order terisi: (.+)"), "🔴 LIVE entry: {0}", 0),
    (re.compile(r"🔴 LIVE exit (\S+) \((\w+)\)"), "🏁 LIVE exit {0} ({1})", 0),
    (re.compile(r"🔴 LIVE partial (\S+): (.+)"), "🎯 LIVE partial {0}: {1}", 0),
    (re.compile(r"✅ PAPER order: (.+)"), "📄 Paper entry: {0}", 0),
    (re.compile(r"📄 Paper close (\S+): (.+)"), "📄 Paper close {0}: {1}", 0),
    (re.compile(r"AUTO-ENTRY DINONAKTIFKAN — (.+)"), "🛑 DISARMED: {0}", 600),
    (re.compile(r"🚧 DAILY-LIMIT: (.+)"), "🚧 DAILY-LIMIT: {0}", 600),
    (re.compile(r"KRITIS: (.+)"), "‼️ KRITIS: {0} — CEK POSISI SEKARANG", 60),
    (re.compile(r"\[(\w+)\] (\d+)× FADED beruntun → entry di-pause (\d+) menit"),
     "⏸ {0}: chop terdeteksi ({1}× FADED beruntun) — entry pause {2} menit", 0),
    (re.compile(r"⚠ Feed (\S+) tidak menerima trade ([\d.]+) s"),
     "⚠ Feed {0} macet {1}s", 900),
    (re.compile(r"UNCAUGHT EXCEPTION"), "💥 Uncaught exception di runner — cek log", 600),
    (re.compile(r"⚠ Tick rate ([\d.]+)/s < 80%"), "🐌 Engine keteteran: {0} tick/s", 1800),
]
DEAD_AFTER_SEC = 300      # heartbeat berhenti sekian → runner dianggap mati

MANAGE_INTERVAL_SEC = 300           # siklus evaluasi rule manage
STATE_FILE = _ROOT / "supervisor_state.json"   # jejak perubahan milik manage
_CHOP_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) .*\[(\w+)\] \d+× FADED beruntun → entry di-pause")


# ── Telegram ───────────────────────────────────────────────────────────

def tg_send(text: str) -> bool:
    """Kirim pesan; tanpa token = cetak ke console (dry-run)."""
    if not TG_TOKEN or not TG_CHAT:
        print("─" * 50 + f"\n[DRY-RUN Telegram]\n{text}\n" + "─" * 50)
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT, "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as e:
        logger.error("Kirim Telegram gagal: %s", e)
        return False


def get_chat_id():
    if not TG_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN belum diisi di .env")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    with urllib.request.urlopen(url, timeout=10) as resp:
        updates = json.loads(resp.read().decode()).get("result", [])
    if not updates:
        sys.exit("Belum ada pesan masuk — kirim pesan apa pun ke bot kamu "
                 "di Telegram dulu, lalu jalankan lagi.")
    for u in updates[-5:]:
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
        if chat:
            print(f"chat_id: {chat.get('id')}  "
                  f"({chat.get('first_name') or chat.get('title', '?')})")
    print("\nSalin ke .env:  TELEGRAM_CHAT_ID=<chat_id di atas>")


# ── Sumber data report ─────────────────────────────────────────────────

def last_heartbeat():
    """(age_sec, state, rate, symbols{sym: price}) dari log runner."""
    if not RUNNER_LOG.exists():
        return None
    try:
        lines = RUNNER_LOG.read_text(encoding="utf-8",
                                     errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines[-400:]):
        m = _HB_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        syms = {s: p for s, p in _HB_SYM_RE.findall(m.group("syms"))}
        return ((datetime.now() - ts).total_seconds(),
                m.group("state").strip(), float(m.group("rate")), syms)
    return None


def trades_today(paper_mode: bool):
    """(n, win, loss, net_paper, per_reason) dari jurnal hari ini."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = PAPER_LOG if paper_mode else LIVE_LOG
    closed_status = "PAPER_CLOSED" if paper_mode else "LIVE_CLOSED"
    n = win = loss = 0
    net = 0.0
    reasons = {}
    if path.exists():
        try:
            for t in json.loads(path.read_text(encoding="utf-8")):
                if t.get("status") != closed_status or \
                        not str(t.get("closed_at", "")).startswith(today):
                    continue
                n += 1
                r = t.get("close_reason", "?")
                reasons[r] = reasons.get(r, 0) + 1
                pnl = float(t.get("pnl_usdt", 0.0))   # live: 0 (PnL dari API)
                net += pnl
                if paper_mode:
                    win += pnl > 0
                    loss += pnl <= 0
        except Exception as e:
            logger.warning("Baca jurnal gagal: %s", e)
    return n, win, loss, net, reasons


def build_report() -> str:
    from pulseflow.trading.executor import TradeExecutor
    from pulseflow.analytics.htf_bias import HTFBiasTracker

    ex = TradeExecutor()
    mode = "PAPER" if ex.paper_mode else "🔴 LIVE"
    L = [f"🤖 PulseFlow Supervisor — {datetime.now():%d %b %H:%M}",
         f"Mode: {mode}"]

    # Balance + PnL hari ini
    try:
        bal = ex.get_balance()["balance"]
        pnl = ex.realized_pnl_today()
        pct = f" ({pnl / bal * 100:+.2f}%)" if bal > 0 else ""
        L.append(f"💰 Balance ${bal:,.2f} · PnL hari ini ${pnl:+,.2f}{pct}")
    except Exception as e:
        L.append(f"💰 Balance: gagal diambil ({e})")

    # Status runner dari heartbeat
    hb = last_heartbeat()
    if hb is None:
        L.append("💀 Runner: TIDAK ADA heartbeat di log — bot jalan?")
        syms = {}
    else:
        age, state, rate, syms = hb
        flag = "💀 MATI?" if age > DEAD_AFTER_SEC else "OK"
        L.append(f"⚙️ Runner: {state} · {rate:.1f} tick/s · "
                 f"heartbeat {age:.0f}s lalu ({flag})")

    # Posisi terbuka
    try:
        pos = ex.get_open_positions()
        if pos:
            for p in pos[:6]:
                if ex.paper_mode:
                    L.append(f"📌 {p.get('symbol')} {p.get('side')} "
                             f"qty {p.get('quantity')} @ {p.get('entry')}")
                else:
                    L.append(f"📌 {p['symbol']} {p['direction']} "
                             f"{p['quantity']} @ {p['entry']:,.6g} "
                             f"(PnL ${p['pnl_usdt']:+,.2f})")
        else:
            L.append("📌 Tidak ada posisi terbuka")
    except Exception as e:
        L.append(f"📌 Posisi: gagal diambil ({e})")

    # Trade hari ini per alasan exit
    n, win, loss, net, reasons = trades_today(ex.paper_mode)
    if n:
        rs = " · ".join(f"{k} {v}" for k, v in
                        sorted(reasons.items(), key=lambda kv: -kv[1]))
        wl = f" ({win}W/{loss}L, net ${net:+,.2f})" if ex.paper_mode else ""
        L.append(f"📊 Trade hari ini: {n}{wl} — {rs}")
    else:
        L.append("📊 Belum ada trade hari ini")

    # Bias 4H per symbol yang dilacak runner
    if syms:
        parts = []
        for s in list(syms)[:6]:
            try:
                t = HTFBiasTracker(s)
                t._refresh()
                b = t.snapshot()
                arrow = {"UP": "▲", "DOWN": "▼"}.get(b["trend"], "─")
                parts.append(f"{s.replace('USDT', '')} {arrow}{b['bias']:+.2f}")
            except Exception:
                parts.append(f"{s.replace('USDT', '')} ?")
        L.append("🧭 Bias 4H: " + " · ".join(parts))

    # control.json
    if CONTROL_FILE.exists():
        try:
            c = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
            L.append(f"🎛 Control: armed={c.get('armed')} · "
                     f"arah {str(c.get('direction', 'both')).upper()} · "
                     f"risk {c.get('risk_pct') or 'default'} · "
                     f"pause {c.get('symbols_paused') or '-'} · "
                     f"limit {c.get('max_daily_loss_pct')}%/"
                     f"{c.get('max_trades_per_day')}x")
            if c.get("note"):
                L.append(f"📝 {c['note']}")
        except Exception:
            L.append("🎛 control.json tidak terbaca")
    return "\n".join(L)


# ── Mode MANAGE: rule-based, menulis control.json ──────────────────────

def pnl_today_by_symbol(ex):
    """(total, {symbol: pnl_net}) hari ini. Paper: jurnal. Live: income
    Binance per symbol (REALIZED_PNL + COMMISSION + FUNDING_FEE)."""
    today = datetime.now().strftime("%Y-%m-%d")
    by = {}
    if ex.paper_mode:
        if PAPER_LOG.exists():
            for t in json.loads(PAPER_LOG.read_text(encoding="utf-8")):
                if t.get("status") == "PAPER_CLOSED" and \
                        str(t.get("closed_at", "")).startswith(today):
                    s = t.get("symbol", "?")
                    by[s] = by.get(s, 0.0) + float(t.get("pnl_usdt", 0.0))
    else:
        midnight = datetime.combine(datetime.now().date(),
                                    datetime.min.time())
        batch = ex.client().futures_income_history(
            startTime=int(midnight.timestamp() * 1000), limit=1000)
        for it in batch:
            if it.get("incomeType") in ("REALIZED_PNL", "COMMISSION",
                                        "FUNDING_FEE"):
                s = it.get("symbol") or "?"
                by[s] = by.get(s, 0.0) + float(it.get("income", 0.0))
    return sum(by.values()), by


def chop_pauses_today() -> dict:
    """{symbol: berapa kali chop-cooldown engine terpicu hari ini} dari
    log runner."""
    today = datetime.now().strftime("%Y-%m-%d")
    counts = {}
    if not RUNNER_LOG.exists():
        return counts
    try:
        for line in RUNNER_LOG.read_text(encoding="utf-8",
                                         errors="replace").splitlines():
            m = _CHOP_LOG_RE.match(line)
            if m and m.group(1) == today:
                counts[m.group(2)] = counts.get(m.group(2), 0) + 1
    except Exception:
        pass
    return counts


def _read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "paused_by_manage": {}, "soft_stopped": False}


def _write_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def manage_cycle(args):
    """Satu evaluasi rule. Hanya menulis control.json bila ada perubahan;
    hanya me-revert perubahan milik manage sendiri (supervisor_state.json)."""
    from pulseflow.trading.executor import TradeExecutor

    if not CONTROL_FILE.exists():
        return
    try:
        ctl = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("manage: control.json tidak terbaca (%s)", e)
        return
    st = _read_state()
    today = datetime.now().strftime("%Y-%m-%d")
    paused = {str(s).upper() for s in (ctl.get("symbols_paused") or [])}
    actions = []

    # Reset harian: revert HANYA perubahan milik manage
    if st.get("date") != today:
        for sym in list(st.get("paused_by_manage", {})):
            if sym in paused:
                paused.discard(sym)
                actions.append(f"🌅 unpause {sym} (reset harian)")
        if st.get("soft_stopped") and ctl.get("armed") is False:
            ctl["armed"] = True
            actions.append("🌅 armed=true (reset harian soft-stop)")
        st = {"date": today, "paused_by_manage": {}, "soft_stopped": False}

    ex = TradeExecutor()
    try:
        balance = ex.get_balance()["balance"]
        total, by_sym = pnl_today_by_symbol(ex)
    except Exception as e:
        logger.warning("manage: gagal ambil PnL (%s) — siklus dilewati", e)
        return

    # R1 — symbol berdarah → pause
    if balance > 0:
        sym_limit = balance * args.pause_symbol_loss / 100.0
        for sym, pnl in by_sym.items():
            if pnl <= -sym_limit and sym not in paused:
                paused.add(sym)
                st["paused_by_manage"][sym] = f"PnL ${pnl:+.2f}"
                actions.append(f"⏸ pause {sym} — PnL hari ini ${pnl:+.2f} "
                               f"(≤ −{args.pause_symbol_loss:.1f}% balance)")

    # R2 — soft-stop harian → armed=false (sebelum hard-limit runner)
    if balance > 0 and ctl.get("armed", True) and \
            total <= -balance * args.soft_stop_loss / 100.0:
        ctl["armed"] = False
        st["soft_stopped"] = True
        actions.append(f"🛑 armed=false — PnL total hari ini ${total:+.2f} "
                       f"(≤ −{args.soft_stop_loss:.1f}% balance). Entry stop "
                       f"s.d. besok; posisi terbuka tetap dikelola runner")

    # R3 — chop kambuhan → pause s.d. besok
    for sym, n in chop_pauses_today().items():
        if n >= args.chop_day_limit and sym not in paused:
            paused.add(sym)
            st["paused_by_manage"][sym] = f"chop {n}×"
            actions.append(f"⏸ pause {sym} — chop-cooldown terpicu {n}× "
                           f"hari ini (≥ {args.chop_day_limit})")

    if not actions:
        _write_state(st)
        return
    ctl["symbols_paused"] = sorted(paused)
    ctl["note"] = f"[manage {datetime.now():%H:%M}] " + "; ".join(actions)
    CONTROL_FILE.write_text(json.dumps(ctl, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    _write_state(st)
    msg = "🤖 MANAGE:\n" + "\n".join(actions)
    logger.info(msg.replace("\n", " | "))
    tg_send(msg)


def manage_loop(stop_ev: threading.Event, args):
    while not stop_ev.is_set():
        try:
            manage_cycle(args)
        except Exception as e:
            logger.error("manage cycle error: %s", e)
        stop_ev.wait(MANAGE_INTERVAL_SEC)


# ── Alert: tail log runner ─────────────────────────────────────────────

def alert_loop(stop_ev: threading.Event):
    """Tail pulseflow_headless.log; kirim alert instan sesuai pola.
    Mulai dari akhir file (kejadian lama tidak dikirim ulang)."""
    last_sent = {}          # label pola → ts terakhir (anti-spam)
    pos = RUNNER_LOG.stat().st_size if RUNNER_LOG.exists() else 0
    dead_alerted = False

    while not stop_ev.is_set():
        stop_ev.wait(3.0)
        # Runner mati? (heartbeat tua) — sekali per kejadian
        hb = last_heartbeat()
        if hb is not None:
            age = hb[0]
            if age > DEAD_AFTER_SEC and not dead_alerted:
                tg_send(f"💀 RUNNER MATI? Heartbeat terakhir {age / 60:.0f} "
                        f"menit lalu — cek VPS/service!")
                dead_alerted = True
            elif age <= DEAD_AFTER_SEC and dead_alerted:
                tg_send("✅ Runner hidup lagi — heartbeat kembali normal")
                dead_alerted = False

        if not RUNNER_LOG.exists():
            continue
        try:
            size = RUNNER_LOG.stat().st_size
            if size < pos:               # log rotation → mulai dari awal file
                pos = 0
            if size == pos:
                continue
            with open(RUNNER_LOG, "r", encoding="utf-8",
                      errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except Exception:
            continue

        now = time.time()
        for line in chunk.splitlines():
            for pat, fmt, cooldown in ALERT_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                if cooldown and now - last_sent.get(fmt, 0.0) < cooldown:
                    continue
                last_sent[fmt] = now
                tg_send(fmt.format(*m.groups()))
                break


# ── Main ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Supervisi PulseFlow: report berkala + alert Telegram")
    ap.add_argument("--interval", type=float, default=60.0, metavar="MIN",
                    help="interval report dalam menit (default: 60)")
    ap.add_argument("--once", action="store_true",
                    help="kirim satu report lalu keluar (tes)")
    ap.add_argument("--no-alerts", action="store_true",
                    help="matikan alert instan, report berkala saja")
    ap.add_argument("--get-chat-id", action="store_true",
                    help="bantu cari TELEGRAM_CHAT_ID (kirim pesan ke bot dulu)")
    ap.add_argument("--manage", action="store_true",
                    help="aktifkan manajemen rule-based (menulis control.json "
                         "tiap 5 menit — lihat docstring untuk aturannya)")
    ap.add_argument("--pause-symbol-loss", type=float, default=1.5,
                    metavar="PCT", help="R1: pause symbol bila PnL symbol "
                    "hari ini ≤ −PCT%% balance (default: 1.5)")
    ap.add_argument("--soft-stop-loss", type=float, default=2.5,
                    metavar="PCT", help="R2: armed=false bila PnL total hari "
                    "ini ≤ −PCT%% balance (default: 2.5)")
    ap.add_argument("--chop-day-limit", type=int, default=3, metavar="N",
                    help="R3: pause symbol s.d. besok bila chop-cooldown "
                    "terpicu ≥ N kali hari ini (default: 3)")
    args = ap.parse_args()

    if args.get_chat_id:
        get_chat_id()
        return

    if not TG_TOKEN or not TG_CHAT:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi di "
                       ".env — mode DRY-RUN (report dicetak ke console)")

    if args.once:
        tg_send(build_report())
        return

    stop_ev = threading.Event()
    if not args.no_alerts:
        threading.Thread(target=alert_loop, args=(stop_ev,), daemon=True,
                         name="alert-tail").start()
        logger.info("Alert tail aktif: %s", RUNNER_LOG.name)
    if args.manage:
        threading.Thread(target=manage_loop, args=(stop_ev, args),
                         daemon=True, name="manage").start()
        logger.info("MANAGE aktif: R1 pause symbol ≤ −%.1f%% · R2 soft-stop "
                    "≤ −%.1f%% · R3 chop ≥ %d×/hari (siklus %d s)",
                    args.pause_symbol_loss, args.soft_stop_loss,
                    args.chop_day_limit, MANAGE_INTERVAL_SEC)

    logger.info("Supervisor berjalan — report tiap %.0f menit. Ctrl+C untuk "
                "berhenti.", args.interval)
    tg_send(build_report())          # report pembuka saat start
    try:
        while True:
            time.sleep(args.interval * 60.0)
            try:
                tg_send(build_report())
            except Exception as e:
                logger.error("Build report gagal: %s", e)
    except KeyboardInterrupt:
        stop_ev.set()
        logger.info("Supervisor berhenti.")


if __name__ == "__main__":
    main()
