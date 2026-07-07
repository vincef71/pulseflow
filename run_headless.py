"""
PulseFlow Headless — runner auto-trade tanpa GUI, untuk VPS/server.

Menjalankan PulseEngine penuh (feed live + seluruh pipeline analitik:
velocity, battle, liquidity, macro, entry) lalu mereplikasi logika
auto-trade dashboard tanpa PyQt6:

  • entry.new_fire                     → prepare_order + execute
  • entry.status PARTIAL               → tutup 50% + SL → breakeven,
        sisa posisi di-trail engine (best ± 2×ATR-1m)
  • entry.status STOP/TP2/FLIP/FADED/TRAIL → tutup posisi symbol itu
        paper : close_paper_trades (PnL net fee + partial_pnl dicatat)
        live  : close_live_trade — HANYA posisi yang dibuka sesi ini

Pengaman (pengganti dialog konfirmasi GUI):
  • PAPER_MODE dibaca dari .env. Bila LIVE (PAPER_MODE=false), runner
    MENOLAK start tanpa flag --live. Flag --paper memaksa paper mode
    apa pun isi .env.
  • Warm-up: fire pada N detik pertama dilewati (konteks klines 1m
    masih seeding; default 90 s, atur via --warmup).
  • Satu posisi per symbol — fire saat posisi masih terbuka dilewati.
  • Circuit breaker: 3 error eksekusi beruntun → entry baru dinonaktif-
    kan (DISARMED). Manajemen exit posisi yang sudah terbuka TETAP jalan.
  • SL fail-safe kritis (posisi gagal ditutup) → langsung DISARMED.

Contoh:
    python run_headless.py                                # paper, symbol default
    python run_headless.py --symbols BTCUSDT ETHUSDT      # paper, pilih symbol
    python run_headless.py --symbols BTCUSDT --live       # LIVE (uang nyata!)
    python run_headless.py --risk 0.5 --heartbeat 30      # override risk & log

Jalankan permanen di VPS via systemd/tmux — lihat HEADLESS.md.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Console Windows default cp1252 tidak bisa meng-encode emoji di log —
# paksa UTF-8 (no-op di Linux).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from pulseflow.config.settings import DEFAULT_SYMBOLS, TICK_INTERVAL_MS
from pulseflow.core.engine import PulseEngine
from pulseflow.trading.executor import TradeExecutor

_LOG_FILE = Path(__file__).resolve().parent / "pulseflow_headless.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(_LOG_FILE, maxBytes=10_000_000, backupCount=5,
                            encoding="utf-8"),
    ],
)
logger = logging.getLogger("PulseFlow.Headless")

# ── Penangkap exception yang tidak bisa gagal (sama seperti run.py) ────

def _log_uncaught(exc_type, exc_value, exc_tb):
    try:
        logger.critical("UNCAUGHT EXCEPTION",
                        exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        pass

def _log_thread_uncaught(args):
    try:
        logger.critical("UNCAUGHT EXCEPTION di thread %r",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception:
        pass

def _log_unraisable(unraisable):
    try:
        logger.error("UNRAISABLE EXCEPTION di %r: %s: %s",
                     unraisable.object,
                     getattr(unraisable.exc_type, "__name__", "?"),
                     unraisable.exc_value)
    except Exception:
        pass

sys.excepthook = _log_uncaught
threading.excepthook = _log_thread_uncaught
sys.unraisablehook = _log_unraisable


END_STATUSES = ("STOP", "TP2", "FLIP", "FADED", "TRAIL")
MAX_CONSEC_EXEC_ERRORS = 3

CONTROL_FILE = Path(__file__).resolve().parent / "control.json"


def parse_trading_hours(spec: str):
    """'07-11,19-23' atau '07:30-11:00' (jam LOKAL) → [(mulai, akhir)] dalam
    jam desimal. Rentang menyeberang tengah malam ('22-02') didukung.
    String kosong = trading 24 jam. Format salah → ValueError."""
    ranges = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        def to_h(s):
            s = s.strip()
            if ":" in s:
                h, m = s.split(":")
                return int(h) % 24 + int(m) / 60.0
            return int(s) % 24
        ranges.append((to_h(a), to_h(b)))
    return ranges


def in_trading_hours(ranges, now: float) -> bool:
    """True bila `now` berada di salah satu rentang (kosong = selalu)."""
    if not ranges:
        return True
    lt = time.localtime(now)
    h = lt.tm_hour + lt.tm_min / 60.0
    for a, b in ranges:
        if a <= b:
            if a <= h < b:
                return True
        elif h >= a or h < b:      # menyeberang tengah malam
            return True
    return False


class HeadlessTrader:
    """Pengganti logika auto-trade dashboard: konsumsi tick engine dan
    eksekusi/tutup posisi via TradeExecutor. Berlaku untuk SEMUA symbol
    yang dilacak (dashboard hanya symbol fokus)."""

    def __init__(self, executor: TradeExecutor, symbols, warmup_sec: float,
                 rebase_cb=None):
        self.executor = executor
        self.symbols = list(symbols)
        # rebase_cb(symbol, fill_price): geser plan engine ke harga fill
        # exchange (slippage market order) — geometri R tetap konsisten.
        self._rebase_cb = rebase_cb
        self.armed = True                      # circuit breaker (reset = restart)
        self.warmup_until = time.time() + warmup_sec

        # Kontrol runtime (control.json, hot-reload) + batas pengaman harian.
        # Batas harian di-hardcode di runner — TIDAK bisa dilonggarkan oleh
        # sesi supervisi melebihi angka di file kontrol yang kamu set.
        self.control_armed = True              # false = pause entry via control
        self.symbols_paused: set = set()
        self.max_daily_loss_pct = 3.0          # loss hari ini ≥ % balance → blok
        self.max_trades_per_day = 20
        self.max_slippage_pct = 0.15           # avg slippage symbol > ini → skip
                                               # sinyal (0 = guard mati)
        self.trading_hours = []                # [(mulai,akhir)] jam lokal;
                                               # kosong = 24 jam. Hanya menahan
                                               # ENTRY — posisi terbuka tetap
                                               # dikelola di luar jam.
        self.trading_hours_spec = ""
        self.daily_trades = 0
        self._daily_date = time.strftime("%Y-%m-%d")
        self._daily_block = False
        self._busy = {s: False for s in self.symbols}
        self._lock = threading.Lock()
        self._consec_errors = 0
        self.trades_opened = 0
        self.trades_closed = 0
        # Snapshot entry terakhir per symbol — dibaca heartbeat (read-only)
        self.last_entry = {}

    # Dipanggil dari thread engine setiap 100 ms per symbol — harus cepat;
    # semua network I/O dilempar ke worker thread.
    def on_tick(self, symbol: str, metrics: dict, signals: list):
        entry = metrics.get("entry") or {}
        self.last_entry[symbol] = entry

        if entry.get("new_fire"):
            self._on_fire(symbol, entry)

        status = entry.get("status")
        if status in END_STATUSES:
            self._on_setup_end(symbol, entry)
        elif status == "PARTIAL":
            self._on_partial(symbol, entry)

    # ── Entry baru ────────────────────────────────────────────────────

    def _on_fire(self, symbol: str, entry: dict):
        plan = entry.get("plan")
        if not plan:
            return
        desc = (f"{plan.get('side')} {symbol} @ ~{entry.get('price', 0):,.6g} "
                f"(skor {entry.get('score')}, setup {entry.get('setup')})")
        if time.time() < self.warmup_until:
            logger.info("⏳ WARM-UP: sinyal dilewati — %s", desc)
            return
        if not self.armed:
            logger.warning("🚫 DISARMED: sinyal dilewati — %s", desc)
            return
        if not self.control_armed:
            logger.info("⏸ CONTROL OFF: sinyal dilewati — %s", desc)
            return
        if not in_trading_hours(self.trading_hours, time.time()):
            logger.info("⏰ Di luar trading hours (%s): sinyal dilewati — %s",
                        self.trading_hours_spec, desc)
            return
        if symbol in self.symbols_paused:
            logger.info("⏸ %s di-pause via control — sinyal dilewati", symbol)
            return
        self._roll_day()
        if self._daily_block:
            logger.warning("🚧 DAILY-LIMIT aktif: sinyal dilewati — %s", desc)
            return
        if self.max_trades_per_day > 0 and self.daily_trades >= self.max_trades_per_day:
            self._daily_block = True
            logger.critical("🚧 DAILY-LIMIT: %d trade/hari tercapai — entry "
                            "baru diblokir sampai ganti hari",
                            self.max_trades_per_day)
            return
        with self._lock:
            if self._busy.get(symbol):
                logger.info("⏭ Eksekusi %s masih berjalan — sinyal dilewati", symbol)
                return
            self._busy[symbol] = True

        plan = dict(plan)
        context = {k: entry.get(k) for k in ("setup", "score", "grade")}
        logger.info("🔥 FIRE: %s — menyiapkan order…", desc)

        def work():
            try:
                if self.executor.has_open_position(symbol):
                    logger.info("⏭ Posisi %s masih terbuka — sinyal dilewati", symbol)
                    return
                # Guard slippage: coin ilikuid membayar 'pajak' per trade
                # yang tak terkalahkan (VANRY 6 Jul: 0.35%/sisi ≈ 0.8R
                # bolak-balik). Live only — paper tidak punya slippage.
                if self.max_slippage_pct > 0 and not self.executor.paper_mode:
                    sp = self.executor.avg_slippage_pct(symbol)
                    if sp > self.max_slippage_pct:
                        logger.warning(
                            "⏭ %s slippage rata-rata %.3f%% > %.2f%% — "
                            "sinyal dilewati (coin terlalu tipis untuk "
                            "market order)", symbol, sp, self.max_slippage_pct)
                        return
                # Guard max loss harian — dicek di worker (network call live)
                if self.max_daily_loss_pct > 0:
                    pnl_today = self.executor.realized_pnl_today()
                    bal = self.executor.get_balance()["balance"]
                    if bal > 0 and pnl_today <= -bal * self.max_daily_loss_pct / 100.0:
                        self._daily_block = True
                        logger.critical(
                            "🚧 DAILY-LIMIT: loss hari ini $%.2f ≥ %.1f%% "
                            "balance ($%.2f) — entry baru diblokir sampai "
                            "ganti hari", -pnl_today, self.max_daily_loss_pct, bal)
                        return
                prepared = self.executor.prepare_order(symbol, plan)
                res = self.executor.execute(prepared, context)
                if res.get("ok"):
                    self._consec_errors = 0
                    self.trades_opened += 1
                    self.daily_trades += 1
                    # Slippage: sinkronkan plan engine ke harga fill aktual
                    fill = float(res.get("fill_price", 0.0) or 0.0)
                    if fill > 0 and self._rebase_cb:
                        try:
                            self._rebase_cb(symbol, fill)
                        except Exception as e:
                            logger.warning("Rebase plan %s gagal: %s", symbol, e)
                    logger.info(
                        "✅ %s order: %s %s qty %s @ ~%s (SL %s · TP1 %s · "
                        "risk $%s · notional $%s)",
                        res.get("mode"), prepared["side"], prepared["symbol"],
                        prepared["quantity"], f"{prepared['entry']:,.6g}",
                        f"{prepared['stop']:,.6g}", f"{prepared['tp1']:,.6g}",
                        prepared["risk_usdt"], prepared["notional_usdt"])
                else:
                    # Live: entry terisi tapi SL gagal → executor sudah
                    # menjalankan fail-safe; catat dan hitung sebagai error.
                    self._record_error(
                        f"eksekusi {symbol} gagal: {res.get('error', res)}")
                    if "KRITIS" in str(res.get("failsafe", "")):
                        self._disarm("fail-safe KRITIS — cek posisi manual SEKARANG")
            except Exception as e:
                self._record_error(f"eksekusi {symbol} error: {e}")
            finally:
                with self._lock:
                    self._busy[symbol] = False
        threading.Thread(target=work, daemon=True,
                         name=f"exec-{symbol}").start()

    # ── Partial TP: profit ≥ 0.5R → tutup 50% + SL ke breakeven ───────

    def _on_partial(self, symbol: str, entry: dict):
        plan = entry.get("plan") or {}
        price = float(entry.get("price", 0.0))
        be_stop = float(plan.get("stop", 0.0))   # engine: entry − buffer napas
        if price <= 0 or be_stop <= 0:
            return
        if not self.executor.paper_mode and not self.executor.is_tracked_live(symbol):
            return   # posisi bukan milik sesi ini — jangan disentuh

        def work():
            try:
                res = self.executor.partial_close(symbol, price, be_stop)
                if res.get("ok"):
                    logger.info("🎯 PARTIAL %s: %s — sisa posisi trailing",
                                symbol, res.get("note") or
                                f"tutup {res.get('closed_qty', res.get('closed', ''))} @ ~{price:,.6g}, SL → BE")
                else:
                    self._record_error(
                        f"partial {symbol} gagal: {res.get('error', res)}")
                    if "KRITIS" in str(res.get("failsafe", "")):
                        self._disarm("fail-safe KRITIS saat partial — cek posisi manual")
            except Exception as e:
                self._record_error(f"partial {symbol} error: {e}")
        threading.Thread(target=work, daemon=True,
                         name=f"partial-{symbol}").start()

    # ── Setup berakhir → sinkronkan posisi ────────────────────────────

    def _on_setup_end(self, symbol: str, entry: dict):
        reason = entry.get("status", "")
        price = float(entry.get("price", 0.0))

        if self.executor.paper_mode:
            def work():
                try:
                    n = self.executor.close_paper_trades(symbol, price, reason)
                    if n:
                        self.trades_closed += n
                        logger.info("📄 Paper close %s: %d posisi (%s)",
                                    symbol, n, reason)
                except Exception as e:
                    logger.error("Paper close %s gagal: %s", symbol, e)
        else:
            if not self.executor.is_tracked_live(symbol):
                return
            def work():
                try:
                    res = self.executor.close_live_trade(symbol, reason)
                    self.trades_closed += 1
                    logger.info("🔴 LIVE exit %s (%s): %s",
                                symbol, reason, res.get("note"))
                except Exception as e:
                    logger.critical("⚠ LIVE exit %s GAGAL: %s — CEK POSISI "
                                    "MANUAL!", symbol, e)
        threading.Thread(target=work, daemon=True,
                         name=f"setup-end-{symbol}").start()

    # ── Batas harian ──────────────────────────────────────────────────

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self.daily_trades = 0
            if self._daily_block:
                logger.info("🌅 Ganti hari — daily limit direset, entry aktif lagi")
            self._daily_block = False

    def state_label(self, now: float) -> str:
        if now < self.warmup_until:
            return f"WARM-UP {self.warmup_until - now:.0f}s"
        if not self.armed:
            return "🛑 DISARMED"
        if not self.control_armed:
            return "⏸ OFF (control)"
        if not in_trading_hours(self.trading_hours, now):
            return f"⏰ OFF-HOURS (aktif {self.trading_hours_spec})"
        self._roll_day()
        if self._daily_block:
            return "🚧 DAILY-LIMIT"
        return "ARMED"

    # ── Circuit breaker ───────────────────────────────────────────────

    def _record_error(self, msg: str):
        self._consec_errors += 1
        logger.error("❌ %s (error beruntun %d/%d)",
                     msg, self._consec_errors, MAX_CONSEC_EXEC_ERRORS)
        if self._consec_errors >= MAX_CONSEC_EXEC_ERRORS:
            self._disarm(f"{MAX_CONSEC_EXEC_ERRORS} error eksekusi beruntun")

    def _disarm(self, why: str):
        if self.armed:
            self.armed = False
            logger.critical("🛑 AUTO-ENTRY DINONAKTIFKAN — %s. Posisi terbuka "
                            "tetap dikelola (SL/TP/exit setup). Restart runner "
                            "untuk mengaktifkan kembali.", why)


class ControlFile:
    """control.json — setelan runtime yang bisa diubah TANPA restart runner
    (hot-reload: mtime dicek tiap 5 s). Diedit manual, atau oleh sesi
    supervisi terjadwal (Claude) yang menyetel bot mengikuti kondisi market.

    Field:
        armed              false = pause SEMUA entry baru (exit tetap dikelola)
        direction          both | long | short | auto (bias 4H)
        risk_pct           null = pakai .env/CLI; angka = override risk %
        symbols_paused     entry symbol tertentu di-pause, mis. ["LABUSDT"]
        max_daily_loss_pct loss hari ini ≥ % balance → blok entry s.d. besok
        max_trades_per_day jumlah entry maksimum per hari
        max_slippage_pct   avg slippage symbol (%harga) > ini → skip sinyal
                           (live only; 0 = mati; default 0.15)
        trading_hours      jendela ENTRY jam lokal, mis. "07-11,19-23" atau
                           "07:30-11:00"; menyeberang tengah malam boleh
                           ("22-02"); kosong = 24 jam. Posisi terbuka tetap
                           dikelola di luar jam (exit tidak diblokir).
        note               catatan bebas — kenapa disetel begini (masuk log)
    """

    def __init__(self, path: Path, engine: PulseEngine,
                 trader: HeadlessTrader, executor: TradeExecutor):
        self.path = Path(path)
        self.engine = engine
        self.trader = trader
        self.executor = executor
        self._mtime = 0.0
        # risk saat runner start (.env/CLI) — risk_pct null = kembali ke ini
        self._default_risk = executor.risk_pct

    def ensure_exists(self, direction: str):
        """Buat file dari nilai CLI bila belum ada. Bila sudah ada, isi
        file yang menang atas CLI (di-apply lewat poll(force=True))."""
        if self.path.exists():
            return
        cfg = {
            "armed": True,
            "direction": direction,
            "risk_pct": None,
            "symbols_paused": [],
            "max_daily_loss_pct": 3.0,
            "max_trades_per_day": 20,
            "max_slippage_pct": 0.15,
            "trading_hours": "",
            "note": "dibuat otomatis oleh run_headless — edit kapan saja, "
                    "reload otomatis tanpa restart",
        }
        self.path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        logger.info("control.json dibuat: %s", self.path)

    def poll(self, force: bool = False):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            cfg = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("control.json tidak valid (%s) — setelan terakhir "
                         "tetap dipakai", e)
            return
        self._apply(cfg)

    def _apply(self, cfg: dict):
        t = self.trader
        t.control_armed = bool(cfg.get("armed", True))

        d = str(cfg.get("direction") or "both").lower()
        dval = d.upper() if d in ("long", "short", "auto") else "BOTH"
        for eng in self.engine.entry_engines.values():
            eng.direction_filter = dval

        risk = cfg.get("risk_pct")
        if risk is None:
            self.executor.risk_pct = self._default_risk   # null = default .env/CLI
        else:
            try:
                self.executor.risk_pct = max(0.1, float(risk))
            except (TypeError, ValueError):
                logger.warning("control.json: risk_pct %r tidak valid", risk)

        t.symbols_paused = {str(s).upper() for s in
                            (cfg.get("symbols_paused") or [])}
        try:
            t.max_daily_loss_pct = float(cfg.get("max_daily_loss_pct", 3.0))
            t.max_trades_per_day = int(cfg.get("max_trades_per_day", 20))
            t.max_slippage_pct = float(cfg.get("max_slippage_pct", 0.15))
        except (TypeError, ValueError):
            logger.warning("control.json: batas harian tidak valid — "
                           "nilai lama dipertahankan")

        spec = str(cfg.get("trading_hours") or "")
        try:
            t.trading_hours = parse_trading_hours(spec)
            t.trading_hours_spec = spec
        except (ValueError, TypeError):
            logger.warning("control.json: trading_hours %r tidak valid "
                           "(format: \"07-11,19-23\") — nilai lama dipertahankan",
                           spec)
        # Blok harian dievaluasi ulang terhadap batas baru pada fire
        # berikutnya — reload control = kesempatan unblock yang disengaja
        if t._daily_block:
            t._daily_block = False
            logger.info("⚙ CONTROL: daily-limit di-reset — dievaluasi ulang "
                        "dengan batas baru di sinyal berikutnya")

        note = str(cfg.get("note") or "")
        logger.info(
            "⚙ CONTROL reload: armed=%s · arah=%s · risk %.2f%% · pause=%s · "
            "max loss %.1f%%/hari · max %d trade/hari · max slip %.2f%% · "
            "jam %s%s",
            t.control_armed, dval, self.executor.risk_pct,
            sorted(t.symbols_paused) or "-",
            t.max_daily_loss_pct, t.max_trades_per_day, t.max_slippage_pct,
            t.trading_hours_spec or "24j",
            f" · note: {note}" if note else "")

    async def watch(self, interval: float = 5.0):
        while True:
            await asyncio.sleep(interval)
            try:
                self.poll()
            except Exception as e:
                logger.warning("Control watch error: %s", e)


async def _heartbeat(engine: PulseEngine, trader: HeadlessTrader,
                     interval: float):
    """Log ringkas kondisi tiap symbol + status runner secara berkala."""
    # Diagnostik tick rate: target = 1000/TICK_INTERVAL_MS (10/s default).
    # Jauh di bawah target = loop engine tidak mengejar (CPU lemah / steal
    # vCPU / swap RAM) → sinyal telat lahir & telat mati.
    target_rate = 1000.0 / TICK_INTERVAL_MS
    last_ticks, last_t = engine.tick_count, time.time()
    while True:
        await asyncio.sleep(interval)
        now = time.time()
        ticks = engine.tick_count
        rate = (ticks - last_ticks) / max(now - last_t, 1e-9)
        last_ticks, last_t = ticks, now
        parts = []
        for sym in engine.symbols:
            ticker = engine.tickers[sym]
            price = ticker.last_price
            ent = trader.last_entry.get(sym) or {}
            count, last_t = engine.get_feed_stats(sym)
            side = ent.get("side") or "-"
            # Ambang whale LARGE efektif ("*" = adaptif, tanpa = statis)
            wthr = ticker.whale_large_threshold
            wmark = "*" if ticker.whale_adaptive else ""
            # Bias 4H: ▲ UP / ▼ DOWN / ─ FLAT (? = belum siap)
            b4 = engine.htf_bias[sym].snapshot()
            if b4.get("ready"):
                b4s = {"UP": "▲", "DOWN": "▼"}.get(b4["trend"], "─") + f"{b4['bias']:+.2f}"
            else:
                b4s = "?"
            chop_left = engine.entry_engines[sym].chop_pause_until - now
            chop_s = f" ⏸chop {chop_left / 60:.0f}m" if chop_left > 0 else ""
            parts.append(f"{sym} {price:,.6g} [{ent.get('phase', '?')} "
                         f"{side} {ent.get('score', 0)}] 4h {b4s} "
                         f"wh ${wthr / 1000:,.3g}K{wmark}{chop_s}")
            if count > 0 and last_t > 0 and now - last_t > 120:
                logger.warning("⚠ Feed %s tidak menerima trade %.0f s — "
                               "cek koneksi", sym, now - last_t)
        logger.info("💓 %s | %.1f tick/s (target %.0f) | %s | open %d · "
                    "closed %d · hari ini %d/%d",
                    trader.state_label(now), rate, target_rate,
                    " · ".join(parts), trader.trades_opened,
                    trader.trades_closed, trader.daily_trades,
                    trader.max_trades_per_day)
        if rate < target_rate * 0.8:
            logger.warning(
                "⚠ Tick rate %.1f/s < 80%% target — engine keteteran: cek CPU "
                "(steal vCPU?) / RAM swap, atau kurangi jumlah symbol", rate)


async def _amain(args, executor: TradeExecutor):
    engine = PulseEngine(mode=args.mode, symbols=args.symbols)
    for eng in engine.entry_engines.values():
        eng.direction_filter = args.direction.upper() if args.direction != "both" else "BOTH"
    trader = HeadlessTrader(
        executor, args.symbols, args.warmup,
        rebase_cb=lambda sym, px: engine.entry_engines[sym].rebase_active_plan(px))

    # control.json — hot-reload setelan runtime (file menang atas CLI)
    ctrl = ControlFile(CONTROL_FILE, engine, trader, executor)
    ctrl.ensure_exists(args.direction)
    ctrl.poll(force=True)

    engine.register_ui_callback(trader.on_tick)
    engine.register_feed_status_callback(
        lambda sym, feed, st, msg: logger.info(
            "Feed %s/%s: %s — %s", sym, feed,
            getattr(st, "value", st), msg))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows: KeyboardInterrupt ditangani di main()

    engine.start()
    hb_task = asyncio.create_task(_heartbeat(engine, trader, args.heartbeat))
    ctrl_task = asyncio.create_task(ctrl.watch())
    logger.info("Runner headless berjalan. Ctrl+C / SIGTERM untuk berhenti.")
    try:
        await stop_event.wait()
        logger.info("Sinyal berhenti diterima — shutdown…")
    finally:
        hb_task.cancel()
        ctrl_task.cancel()
        await engine.stop()
        logger.info("Engine berhenti. Posisi TIDAK ditutup otomatis saat "
                    "shutdown — SL/TP di exchange tetap terpasang (live), "
                    "posisi paper tetap tercatat open.")


def _parse_args():
    p = argparse.ArgumentParser(
        description="PulseFlow headless auto-trade runner (VPS, tanpa GUI)")
    p.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS),
                   metavar="SYM", help="symbol yang dilacak & ditradingkan "
                   f"(default: {' '.join(DEFAULT_SYMBOLS)})")
    p.add_argument("--mode", choices=["binance", "hyperliquid"],
                   default="binance",
                   help="sumber data feed (eksekusi selalu Binance Futures)")
    p.add_argument("--live", action="store_true",
                   help="konfirmasi eksplisit mode LIVE (wajib bila "
                        "PAPER_MODE=false di .env)")
    p.add_argument("--paper", action="store_true",
                   help="paksa paper mode, abaikan PAPER_MODE di .env")
    p.add_argument("--direction", choices=["both", "long", "short", "auto"],
                   default="both",
                   help="filter arah entry: long/short only, atau auto = "
                        "hanya searah bias trend 4H (default: both)")
    p.add_argument("--risk", type=float, default=None, metavar="PCT",
                   help="override risk %% per trade (default: RISK_PCT .env)")
    p.add_argument("--warmup", type=float, default=90.0, metavar="SEC",
                   help="detik awal tanpa eksekusi — tunggu konteks penuh "
                        "(default: 90)")
    p.add_argument("--heartbeat", type=float, default=60.0, metavar="SEC",
                   help="interval log status berkala (default: 60)")
    return p.parse_args()


def main():
    args = _parse_args()
    args.symbols = [s.upper() for s in args.symbols]

    executor = TradeExecutor(paper_mode=True if args.paper else None)
    if args.risk is not None:
        executor.risk_pct = args.risk

    # Gerbang keselamatan LIVE — pengganti dialog konfirmasi GUI.
    if not executor.paper_mode:
        if not args.live:
            logger.error(
                "PAPER_MODE=false di .env (mode LIVE — uang nyata) tetapi "
                "flag --live tidak diberikan. Tambahkan --live untuk "
                "konfirmasi, atau jalankan dengan --paper.")
            sys.exit(2)
        chk = executor.verify_connection()
        if not chk.get("ok"):
            logger.error("Verifikasi API Binance gagal: %s", chk.get("error"))
            sys.exit(2)
        logger.warning("🔴 MODE LIVE dikonfirmasi (--live). Balance USDT: $%s",
                       f"{chk['usdt_balance']:,.2f}")
    else:
        bal = executor.get_balance()
        logger.info("📄 Paper mode. Balance simulasi: $%s",
                    f"{bal['balance']:,.2f}")

    logger.info(
        "PulseFlow HEADLESS start — mode data: %s · symbols: %s · exec: %s · "
        "risk %.2f%%/trade · leverage %dx · warmup %.0fs · arah: %s",
        args.mode, ", ".join(args.symbols),
        "PAPER" if executor.paper_mode else "🔴 LIVE",
        executor.risk_pct, executor.leverage, args.warmup,
        args.direction.upper())

    try:
        asyncio.run(_amain(args, executor))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — runner berhenti.")


if __name__ == "__main__":
    main()
