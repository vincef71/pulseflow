"""Runner live/paper stupidbot — polling candle closed.

Alur keputusan IDENTIK dengan backtester: candle Daily yang sudah tuntas →
bias, candle TF entry closed → sinyal, lalu lapisan proteksi akun
(AdaptiveRisk, EquityGuard, TradeThrottle) sebelum eksekusi.

Mode:
- PAPER (default) — order disimulasikan penuh dari candle closed; tidak ada
  satu pun request order ke exchange. Jurnal: logs/paper_live_trades.jsonl.
- LIVE — entry market + SL/TP conditional DI EXCHANGE via StupidbotExecutor.
  Posisi selalu terlindungi SL exchange: bot mati pun SL tetap hidup.
  Partial TP, BE, dan ATR trailing disinkronkan tiap candle close.

Keamanan LIVE (double opt-in): flag --live DAN PAPER_MODE=false di ../.env.
State (posisi, tier risiko, guard, kuota bulanan) dipersist ke
state/live_state.json agar restart aman.
"""
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from backtester.backtest import DAY_MS, close_position
from config.settings import Settings
from core.models import Candle, Direction, Signal
from daily_bias.bias import DailyBiasEngine
from data.binance import get_recent, interval_ms
from entry_engine.engine import EntryEngine
from logger.trade_log import TradeLogger
from position_manager.manager import Fill, Position, PositionManager
from risk_manager.risk import AdaptiveRisk, EquityGuard, TradeThrottle, position_size

logger = logging.getLogger("stupidbot.live")

STATE_FILE = Path("state/live_state.json")
CANDLE_CLOSE_BUFFER_MS = 15_000  # tunggu 15 dtk setelah boundary agar candle final


class LiveTrader:
    def __init__(self, cfg: Settings, symbols: list[str], entry_tf: str,
                 live: bool, executor=None, paper_balance: float = 10_000.0):
        self.cfg = cfg
        self.symbols = symbols
        self.tf = entry_tf
        self.step = interval_ms(entry_tf)
        self.live = live
        self.executor = executor  # hanya untuk LIVE
        if live and executor is None:
            raise ValueError("Mode LIVE butuh executor")

        self.bias = {s: DailyBiasEngine(cfg) for s in symbols}
        self.entry = {s: EntryEngine(cfg) for s in symbols}
        self.pm = PositionManager(cfg)
        self.positions: dict[str, Position] = {}
        self.pending_daily: dict[str, list[Candle]] = {s: [] for s in symbols}
        self.last_daily_ts = {s: 0 for s in symbols}
        self.last_entry_ts = {s: 0 for s in symbols}

        self.paper_balance = paper_balance
        self.started_ts = int(time.time() * 1000)
        self.adaptive: AdaptiveRisk | None = None
        self.guard: EquityGuard | None = None
        self.throttle = TradeThrottle(cfg)
        self.journal = TradeLogger(
            f"logs/{'live' if live else 'paper_live'}_trades_{entry_tf}.jsonl")
        self._bal_cache: float | None = None

    # ── Balance ───────────────────────────────────────────────────────
    def _balance(self, refresh: bool = False) -> float:
        if not self.live:
            return self.paper_balance
        if self._bal_cache is None or refresh:
            self._bal_cache = float(self.executor.get_balance()["available"])
        return self._bal_cache

    # ── Warmup & state ────────────────────────────────────────────────
    def warmup(self) -> None:
        """Bangun struktur dari history — TIDAK ada entry dari candle lama."""
        for s in self.symbols:
            self.pending_daily[s] = get_recent(s, "1d", 400)
            if self.pending_daily[s]:
                self.last_daily_ts[s] = self.pending_daily[s][-1].ts
            for c in get_recent(s, self.tf, 600):
                self._feed_daily(s, c.ts)
                self.entry[s].update(c)
                self.last_entry_ts[s] = c.ts
            logger.info("Warmup %s selesai (Daily s/d %s)", s, self.last_daily_ts[s])

        bal = self._balance(refresh=True)
        self.adaptive = AdaptiveRisk(self.cfg, bal)
        self.guard = EquityGuard(self.cfg, bal)
        self._load_state()

    def _feed_daily(self, s: str, before_ts: int) -> None:
        buf = self.pending_daily[s]
        while buf and buf[0].ts + DAY_MS <= before_ts:
            self.bias[s].update(buf.pop(0))

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("State rusak, mulai segar: %s", e)
            return
        if st.get("mode") != ("LIVE" if self.live else "PAPER"):
            logger.warning("State mode %s != mode sekarang — state diabaikan",
                           st.get("mode"))
            return
        self.paper_balance = st.get("paper_balance", self.paper_balance)
        a = st.get("adaptive", {})
        self.adaptive.idx = a.get("idx", self.adaptive.idx)
        self.adaptive.peak = a.get("peak", self.adaptive.peak)
        g = st.get("guard", {})
        self.guard.peak = g.get("peak", self.guard.peak)
        self.guard.block_until_ts = g.get("block_until_ts", 0)
        t = st.get("throttle", {})
        self.throttle.month = tuple(t["month"]) if t.get("month") else None
        self.throttle.count = t.get("count", 0)
        self.throttle.last_entry_ts = t.get("last_entry_ts")
        for s, p in st.get("positions", {}).items():
            if s not in self.symbols:
                logger.warning("Posisi %s di state tapi tidak di --symbols — "
                               "tetap dimuat agar dikelola", s)
                continue
            sig_d = dict(p["signal"])
            sig = Signal(direction=Direction(sig_d.pop("direction")), **sig_d)
            pos = Position(signal=sig, qty=p["qty"], init_qty=p["init_qty"],
                           risk_amount=p["risk_amount"], risk_pct=p["risk_pct"])
            pos.sl = p["sl"]
            pos.partial_done = p["partial_done"]
            pos.mfe_r = p["mfe_r"]
            pos.mae_r = p["mae_r"]
            pos.fills = [Fill(**f) for f in p.get("fills", [])]
            self.positions[s] = pos
        logger.info("State dimuat: %d posisi, tier risiko %.2f%%, kuota bulan %d",
                    len(self.positions), self.adaptive.current_pct, self.throttle.count)

    def _save_state(self) -> None:
        st = {
            "mode": "LIVE" if self.live else "PAPER",
            "paper_balance": self.paper_balance,
            "adaptive": {"idx": self.adaptive.idx, "peak": self.adaptive.peak},
            "guard": {"peak": self.guard.peak,
                      "block_until_ts": self.guard.block_until_ts},
            "throttle": {"month": list(self.throttle.month) if self.throttle.month else None,
                         "count": self.throttle.count,
                         "last_entry_ts": self.throttle.last_entry_ts},
            "positions": {
                s: {"signal": asdict(p.signal), "qty": p.qty,
                    "init_qty": p.init_qty, "risk_amount": p.risk_amount,
                    "risk_pct": p.risk_pct, "sl": p.sl,
                    "partial_done": p.partial_done, "mfe_r": p.mfe_r,
                    "mae_r": p.mae_r, "fills": [asdict(f) for f in p.fills]}
                for s, p in self.positions.items()},
        }
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")

    # ── Siklus utama ──────────────────────────────────────────────────
    def cycle(self) -> None:
        self._bal_cache = None
        fresh: dict[str, Candle] = {}

        for s in self.symbols:
            try:
                for c in get_recent(s, "1d", 5):
                    if c.ts > self.last_daily_ts[s]:
                        self.pending_daily[s].append(c)
                        self.last_daily_ts[s] = c.ts
                for c in get_recent(s, self.tf, 10):
                    if c.ts <= self.last_entry_ts[s]:
                        continue
                    self._process_candle(s, c)
                    fresh[s] = c
            except Exception as e:
                logger.error("Cycle %s gagal: %s", s, e)

        self._maybe_enter(fresh)
        self._save_state()

    def _process_candle(self, s: str, candle: Candle) -> None:
        self._feed_daily(s, candle.ts)
        self.entry[s].update(candle)
        self.last_entry_ts[s] = candle.ts
        self.guard.on_candle(candle.ts, self._balance())

        pos = self.positions.get(s)
        if pos is not None and candle.ts > pos.signal.ts:
            if self.live:
                self._manage_live(s, pos, candle)
            else:
                self._manage_paper(s, pos, candle)

    # ── Manajemen posisi PAPER (simulasi penuh, sama dengan backtest) ──
    def _manage_paper(self, s: str, pos: Position, candle: Candle) -> None:
        closed = self.pm.on_candle(pos, candle, self.entry[s].atr.value)
        if not closed:
            return
        trade, pnl = close_position(self.cfg, s, pos, candle.ts)
        self.paper_balance += pnl
        self.adaptive.on_trade_close(self.paper_balance)
        self.guard.on_trade_close(candle.ts, self.paper_balance)
        self.journal.append(trade)
        del self.positions[s]
        logger.info("PAPER exit %s: %s pnl %+.2f (balance %.2f)",
                    s, trade.exit_reason, pnl, self.paper_balance)

    # ── Manajemen posisi LIVE ─────────────────────────────────────────
    def _manage_live(self, s: str, pos: Position, candle: Candle) -> None:
        d = 1 if pos.direction == Direction.LONG else -1
        stop = pos.stop_dist
        adverse = candle.low if d == 1 else candle.high
        favorable = candle.high if d == 1 else candle.low
        pos.mae_r = max(pos.mae_r, d * (pos.entry - adverse) / stop)
        pos.mfe_r = max(pos.mfe_r, d * (favorable - pos.entry) / stop)

        amt = self.executor.position_amount(s)
        if amt == 0:
            # ditutup exchange (SL / BE / TRAIL / TP terisi)
            if d * (adverse - pos.sl) <= 0:
                px, reason = pos.sl, ("BE" if pos.sl == pos.entry
                                      else "TRAIL" if d * (pos.sl - pos.entry) > 0
                                      else "SL")
            elif d * (favorable - pos.signal.tp) >= 0:
                px, reason = pos.signal.tp, "TP"
            else:
                px, reason = candle.close, "EXCHANGE"
            pos.fills.append(Fill(px, pos.qty, reason, candle.ts))
            pos.qty = 0.0
            trade, _ = close_position(self.cfg, s, pos, candle.ts)
            self.journal.append(trade)  # estimasi; PnL resmi = data exchange
            self.executor.cancel_protection(s)  # bersihkan algo order yatim
            self.executor.mark_closed(s, reason)
            bal = self._balance(refresh=True)
            self.adaptive.on_trade_close(bal)
            self.guard.on_trade_close(candle.ts, bal)
            del self.positions[s]
            logger.info("🔴 LIVE exit %s: %s (balance %.2f)", s, reason, bal)
            return

        dirty = False
        # partial TP di +partial_tp_r → SL ke BE
        partial_px = pos.entry + d * self.cfg.partial_tp_r * stop
        if (not pos.partial_done and self.cfg.partial_fraction > 0
                and d * (favorable - partial_px) >= 0):
            q = self.executor.reduce_position(
                s, pos.direction.value, pos.qty * self.cfg.partial_fraction)
            pos.partial_done = True
            if q > 0:
                pos.fills.append(Fill(partial_px, q, "PARTIAL_TP", candle.ts))
                pos.qty -= q
            if self.cfg.be_after_partial and d * (pos.entry - pos.sl) > 0:
                pos.sl = pos.entry
            dirty = True

        # ATR trailing hanya setelah +trail_start_r
        atr = self.entry[s].atr.value
        r_close = d * (candle.close - pos.entry) / stop
        if r_close >= self.cfg.trail_start_r and atr:
            new_sl = candle.close - d * self.cfg.trail_atr_mult * atr
            if d * (new_sl - pos.sl) > 0:
                pos.sl = new_sl
                dirty = True

        if dirty:
            res = self.executor.sync_protection(
                s, pos.direction.value, pos.sl, pos.signal.tp)
            if not res.get("ok"):
                # fail-safe executor sudah menutup posisi
                logger.error("Proteksi %s gagal — posisi ditutup fail-safe", s)
                pos.fills.append(Fill(candle.close, pos.qty, "FAILSAFE", candle.ts))
                pos.qty = 0.0
                trade, _ = close_position(self.cfg, s, pos, candle.ts)
                self.journal.append(trade)
                self.executor.mark_closed(s, "FAILSAFE")
                del self.positions[s]

    # ── Entry ─────────────────────────────────────────────────────────
    def _maybe_enter(self, fresh: dict[str, Candle]) -> None:
        """Kumpulkan kandidat dari candle yang baru close di cycle ini,
        ranking pakai structure_score, isi slot yang tersedia."""
        slots = self.cfg.max_open_positions - len(self.positions)
        if slots <= 0 or not fresh:
            return
        now_ts = max(c.ts for c in fresh.values())
        if not self.guard.allowed(now_ts) or not self.throttle.allowed(now_ts):
            return

        candidates = []
        for s, candle in fresh.items():
            if s in self.positions or candle.ts + self.step <= self.started_ts:
                continue  # jangan entry dari candle sebelum runner hidup
            bias, reason = self.bias[s].bias()
            if bias == Direction.NEUTRAL:
                continue
            sig = self.entry[s].check(bias, reason, self.bias[s])
            if sig is None:
                continue
            score = self.bias[s].structure_score()
            if len(self.symbols) > 1 and score < self.cfg.min_structure_score:
                continue
            candidates.append((score, s, sig))

        candidates.sort(key=lambda x: x[0], reverse=True)
        for score, s, sig in candidates[:slots]:
            if not self.throttle.allowed(sig.ts):
                break
            try:
                self._open(s, sig, score)
            except Exception as e:
                logger.error("Entry %s gagal: %s", s, e)

    def _open(self, s: str, sig: Signal, score: float) -> None:
        risk_pct = self.adaptive.current_pct

        if self.live:
            plan = {"side": sig.direction.value, "entry": sig.entry,
                    "stop": sig.sl, "tp1": sig.tp, "tp2": sig.tp}
            prepared = self.executor.prepare_order(s, plan, risk_pct)
            logger.info(
                "🔴 LIVE order %s %s | entry %.6g SL %.6g TP %.6g | qty %s "
                "notional $%.2f risk $%.2f (%.2f%%) RR %.2f | %s",
                prepared["side"], prepared["symbol"], prepared["entry"],
                prepared["stop"], prepared["tp1"], prepared["quantity"],
                prepared["notional_usdt"], prepared["risk_usdt"], risk_pct,
                prepared["rr1"], sig.reason)
            res = self.executor.execute(
                prepared, context={"setup": f"stupidbot:{sig.pattern}",
                                   "score": round(score, 1)})
            if not res.get("ok"):
                logger.error("Eksekusi LIVE %s gagal: %s", s, res.get("error"))
                return
            # samakan state dengan fill aktual — SL/TP di exchange sudah
            # digeser sebesar slippage oleh executor
            fill = float(res.get("fill_price", sig.entry) or sig.entry)
            slip = fill - sig.entry
            sig = Signal(direction=sig.direction, ts=sig.ts, entry=fill,
                         sl=sig.sl + slip, tp=sig.tp + slip, rr=sig.rr,
                         atr=sig.atr, pattern=sig.pattern, reason=sig.reason)
            qty = prepared["quantity"]
            risk_amount = prepared["risk_usdt"]
        else:
            qty, risk_amount = position_size(self._balance(), risk_pct,
                                             sig.entry, sig.sl)
            if qty <= 0:
                return
            logger.info("PAPER order %s %s | entry %.6g SL %.6g TP %.6g | "
                        "qty %.6g risk $%.2f (%.2f%%) | %s",
                        sig.direction.value, s, sig.entry, sig.sl, sig.tp,
                        qty, risk_amount, risk_pct, sig.reason)

        self.positions[s] = Position(signal=sig, qty=qty, init_qty=qty,
                                     risk_amount=risk_amount, risk_pct=risk_pct)
        self.throttle.on_entry(sig.ts)

    # ── Loop ──────────────────────────────────────────────────────────
    def run(self, once: bool = False) -> None:
        mode = "🔴 LIVE (uang nyata)" if self.live else "📄 PAPER (simulasi)"
        logger.info("stupidbot runner mulai — mode %s, %s %s, risk tier %s%%",
                    mode, "+".join(self.symbols), self.tf,
                    "/".join(str(t) for t in self.cfg.risk_tiers_pct))
        self.warmup()
        while True:
            try:
                self.cycle()
            except Exception as e:
                logger.exception("Cycle error: %s", e)
            if once:
                logger.info("--once: satu siklus selesai, keluar.")
                return
            now = int(time.time() * 1000)
            nxt = (now // self.step + 1) * self.step + CANDLE_CLOSE_BUFFER_MS
            wait_s = max((nxt - now) / 1000.0, 5.0)
            logger.info("Tidur %.0f dtk sampai candle %s berikutnya close…",
                        wait_s, self.tf)
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                self._save_state()
                logger.info("Dihentikan — state tersimpan. Posisi LIVE tetap "
                            "terlindungi SL/TP di exchange.")
                return
