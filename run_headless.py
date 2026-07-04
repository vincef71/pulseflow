"""
PulseFlow Headless — runner auto-trade tanpa GUI, untuk VPS/server.

Menjalankan PulseEngine penuh (feed live + seluruh pipeline analitik:
velocity, battle, liquidity, macro, entry) lalu mereplikasi logika
auto-trade dashboard tanpa PyQt6:

  • entry.new_fire                     → prepare_order + execute
  • entry.status STOP/TP2/FLIP/FADED   → tutup posisi symbol itu
        paper : close_paper_trades (PnL net fee dicatat)
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

from pulseflow.config.settings import DEFAULT_SYMBOLS
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


END_STATUSES = ("STOP", "TP2", "FLIP", "FADED")
MAX_CONSEC_EXEC_ERRORS = 3


class HeadlessTrader:
    """Pengganti logika auto-trade dashboard: konsumsi tick engine dan
    eksekusi/tutup posisi via TradeExecutor. Berlaku untuk SEMUA symbol
    yang dilacak (dashboard hanya symbol fokus)."""

    def __init__(self, executor: TradeExecutor, symbols, warmup_sec: float):
        self.executor = executor
        self.symbols = list(symbols)
        self.armed = True                      # entry baru diizinkan
        self.warmup_until = time.time() + warmup_sec
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

        if entry.get("status") in END_STATUSES:
            self._on_setup_end(symbol, entry)

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
                prepared = self.executor.prepare_order(symbol, plan)
                res = self.executor.execute(prepared, context)
                if res.get("ok"):
                    self._consec_errors = 0
                    self.trades_opened += 1
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


async def _heartbeat(engine: PulseEngine, trader: HeadlessTrader,
                     interval: float):
    """Log ringkas kondisi tiap symbol + status runner secara berkala."""
    while True:
        await asyncio.sleep(interval)
        now = time.time()
        parts = []
        for sym in engine.symbols:
            price = engine.tickers[sym].last_price
            ent = trader.last_entry.get(sym) or {}
            count, last_t = engine.get_feed_stats(sym)
            side = ent.get("side") or "-"
            parts.append(f"{sym} {price:,.6g} [{ent.get('phase', '?')} "
                         f"{side} {ent.get('score', 0)}]")
            if count > 0 and last_t > 0 and now - last_t > 120:
                logger.warning("⚠ Feed %s tidak menerima trade %.0f s — "
                               "cek koneksi", sym, now - last_t)
        state = "ARMED" if trader.armed else "🛑 DISARMED"
        if now < trader.warmup_until:
            state = f"WARM-UP {trader.warmup_until - now:.0f}s"
        logger.info("💓 %s | %s | open %d · closed %d",
                    state, " · ".join(parts),
                    trader.trades_opened, trader.trades_closed)


async def _amain(args, executor: TradeExecutor):
    engine = PulseEngine(mode=args.mode, symbols=args.symbols)
    trader = HeadlessTrader(executor, args.symbols, args.warmup)

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
    logger.info("Runner headless berjalan. Ctrl+C / SIGTERM untuk berhenti.")
    try:
        await stop_event.wait()
        logger.info("Sinyal berhenti diterima — shutdown…")
    finally:
        hb_task.cancel()
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
        "risk %.2f%%/trade · leverage %dx · warmup %.0fs",
        args.mode, ", ".join(args.symbols),
        "PAPER" if executor.paper_mode else "🔴 LIVE",
        executor.risk_pct, executor.leverage, args.warmup)

    try:
        asyncio.run(_amain(args, executor))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — runner berhenti.")


if __name__ == "__main__":
    main()
