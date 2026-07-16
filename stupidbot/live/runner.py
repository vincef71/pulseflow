"""Runner live/paper stupidbot — polling candle closed, NO MARKET ORDER.

Alur keputusan IDENTIK dengan backtester: candle Daily yang sudah tuntas →
bias, candle TF entry closed → sinyal, lapisan proteksi akun, lalu order
LIMIT entry di bekas level SL (bawah/atas wick rejection — area stop-hunt).
Order hidup sampai terisi atau zona/struktur rusak (bias flip, trend TF
entry berubah, atau harga breakout tanpa mengisi order).

Mode:
- PAPER (default) — simulasi penuh dari candle closed; jurnal
  logs/paper_live_trades_{tf}.jsonl.
- LIVE — LIMIT entry + STOP_MARKET protektif dipasang SERENTAK (posisi
  terlindungi sejak detik pertama terisi; SL yang trigger tanpa posisi
  hangus tanpa efek). Setelah terisi: TP dipasang sebagai LIMIT reduce-only
  (maker) dua leg — partial di +1.5R dan sisa di TP. BE dan ATR trailing
  menggeser STOP_MARKET tiap candle close.

Keamanan LIVE (double opt-in): flag --live DAN PAPER_MODE=false di ../.env.
State dipersist ke state/live_state.json agar restart aman.
"""
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from backtester.backtest import (DAY_MS, PendingEntry, close_position,
                                 fill_pending, pending_action)
from config.settings import Settings
from core.models import Candle, Direction, Signal
from daily_bias.bias import DailyBiasEngine
from data.binance import get_recent, interval_ms
from entry_engine.engine import EntryEngine
from logger.market_status import format_market_status
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
        self.pendings: dict[str, PendingEntry] = {}
        self.pending_oid: dict[str, int] = {}  # LIVE: orderId limit entry
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

    @staticmethod
    def _signal_from_dict(d: dict) -> Signal:
        d = dict(d)
        return Signal(direction=Direction(d.pop("direction")), **d)

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
            pos = Position(signal=self._signal_from_dict(p["signal"]),
                           qty=p["qty"], init_qty=p["init_qty"],
                           risk_amount=p["risk_amount"], risk_pct=p["risk_pct"],
                           opened_ts=p.get("opened_ts", 0))
            pos.sl = p["sl"]
            pos.partial_done = p["partial_done"]
            pos.mfe_r = p["mfe_r"]
            pos.mae_r = p["mae_r"]
            pos.fills = [Fill(**f) for f in p.get("fills", [])]
            self.positions[s] = pos
        for s, p in st.get("pendings", {}).items():
            self.pendings[s] = PendingEntry(
                signal=self._signal_from_dict(p["signal"]), qty=p["qty"],
                risk_amount=p["risk_amount"], risk_pct=p["risk_pct"],
                placed_ts=p["placed_ts"])
            if p.get("order_id"):
                self.pending_oid[s] = p["order_id"]
        logger.info("State dimuat: %d posisi, %d pending, tier risiko %.2f%%, "
                    "kuota bulan %d", len(self.positions), len(self.pendings),
                    self.adaptive.current_pct, self.throttle.count)

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
                    "risk_pct": p.risk_pct, "opened_ts": p.opened_ts,
                    "sl": p.sl, "partial_done": p.partial_done,
                    "mfe_r": p.mfe_r, "mae_r": p.mae_r,
                    "fills": [asdict(f) for f in p.fills]}
                for s, p in self.positions.items()},
            "pendings": {
                s: {"signal": asdict(p.signal), "qty": p.qty,
                    "risk_amount": p.risk_amount, "risk_pct": p.risk_pct,
                    "placed_ts": p.placed_ts,
                    "order_id": self.pending_oid.get(s)}
                for s, p in self.pendings.items()},
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
        self._print_status()

    def _print_status(self) -> None:
        """Blok analisa market per simbol — untuk verifikasi manual user."""
        now = int(time.time() * 1000)
        header = (f"── STATUS MARKET — balance {self._balance():,.2f} | "
                  f"tier risiko {self.adaptive.current_pct}% | "
                  f"kuota bulan {self.throttle.count}/{self.cfg.max_trades_per_month}")
        if not self.guard.allowed(now):
            from datetime import datetime, timezone
            until = datetime.fromtimestamp(self.guard.block_until_ts / 1000,
                                           tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            header += f" | ⛔ EQUITY PROTECTION aktif s/d {until} UTC"
        logger.info(header)
        for s in self.symbols:
            candle = self.entry[s]._cur
            if candle is None:
                continue
            note = ""
            if s not in self.positions and s not in self.pendings:
                bias, bias_reason = self.bias[s].bias()
                if bias == Direction.NEUTRAL:
                    note = f"TIDAK ENTRY — {bias_reason}"
                else:
                    sig = self.entry[s].check(bias, bias_reason, self.bias[s])
                    if sig is not None:
                        note = ("SINYAL VALID — menunggu slot/gate "
                                "(guard/kuota/cooldown/slot penuh)")
                    else:
                        note = f"TIDAK ENTRY — {self.entry[s].last_reject}"
            for line in format_market_status(
                    s, self.tf, self.cfg, candle, self.bias[s], self.entry[s],
                    position=self.positions.get(s),
                    pending=self.pendings.get(s), status_note=note):
                logger.info(line)

    def _process_candle(self, s: str, candle: Candle) -> None:
        self._feed_daily(s, candle.ts)
        self.entry[s].update(candle)
        self.last_entry_ts[s] = candle.ts
        self.guard.on_candle(candle.ts, self._balance())

        if s in self.positions:
            pos = self.positions[s]
            if candle.ts > pos.opened_ts:
                if self.live:
                    self._manage_live(s, pos, candle)
                else:
                    self._manage_paper(s, pos, candle)
        elif s in self.pendings:
            if self.live:
                self._check_pending_live(s, candle)
            else:
                self._check_pending_paper(s, candle)

    # ── Pending limit — PAPER ─────────────────────────────────────────
    def _check_pending_paper(self, s: str, candle: Candle) -> None:
        pen = self.pendings[s]
        if not self.guard.allowed(candle.ts):
            self._cancel_pending(s, "equity protection")
            return
        bias, _ = self.bias[s].bias()
        act = pending_action(pen, candle, bias, self.entry[s].tracker.trend)
        if act == "CANCEL":
            self._cancel_pending(s, "zona/struktur rusak")
        elif act == "FILL":
            del self.pendings[s]
            pos, stopped = fill_pending(pen, candle)
            if stopped:
                self._record_paper_close(s, pos, candle.ts)
            else:
                self.positions[s] = pos
                logger.info("PAPER limit terisi %s %s @ %.6g (SL %.6g TP %.6g)",
                            pos.direction.value, s, pos.entry, pos.sl,
                            pos.signal.tp)

    # ── Pending limit — LIVE ──────────────────────────────────────────
    def _check_pending_live(self, s: str, candle: Candle) -> None:
        pen = self.pendings[s]
        oid = self.pending_oid.get(s)
        st = self.executor.order_status(s, oid) if oid else {"status": "NEW"}

        if st["status"] == "FILLED":
            self._on_live_fill(s, pen, st["avg_price"] or pen.signal.entry,
                               candle.ts, pen.qty)
            return
        if st["status"] in ("CANCELED", "EXPIRED", "REJECTED"):
            logger.warning("Limit entry %s berstatus %s di exchange", s, st["status"])
            self.executor.cancel_protection(s)  # tarik SL protektif
            self._cancel_pending(s, f"exchange {st['status']}", cancel_order=False)
            return

        bias, _ = self.bias[s].bias()
        act = pending_action(pen, candle, bias, self.entry[s].tracker.trend)
        if act == "FILL":
            # candle menyentuh limit tapi status belum FILLED (partial/latency)
            # — biarkan satu candle lagi; status exchange adalah kebenaran.
            act = "WAIT"
        if act == "CANCEL" or not self.guard.allowed(candle.ts):
            self.executor.cancel_order(s, oid)
            amt = abs(self.executor.position_amount(s))
            if amt > 0:
                # terisi sebagian tepat saat dibatalkan → kelola sisa sebagai posisi
                logger.info("Limit %s terisi sebagian %.6g saat cancel — "
                            "dikelola sebagai posisi", s, amt)
                del self.pendings[s]
                self.pending_oid.pop(s, None)
                self._on_live_fill(s, pen, pen.signal.entry, candle.ts, amt)
            else:
                self.executor.cancel_protection(s)
                self._cancel_pending(s, "zona/struktur rusak", cancel_order=False)

    def _cancel_pending(self, s: str, why: str, cancel_order: bool = True) -> None:
        if cancel_order and self.live and s in self.pending_oid:
            self.executor.cancel_order(s, self.pending_oid[s])
            self.executor.cancel_protection(s)
        self.pendings.pop(s, None)
        self.pending_oid.pop(s, None)
        self.throttle.on_cancel()
        logger.info("Pending %s dibatalkan (%s)", s, why)

    def _on_live_fill(self, s: str, pen: PendingEntry, fill_px: float,
                      ts: int, qty: float) -> None:
        self.pendings.pop(s, None)
        self.pending_oid.pop(s, None)
        sig = pen.signal
        pos = Position(signal=sig, qty=qty, init_qty=qty,
                       risk_amount=pen.risk_amount, risk_pct=pen.risk_pct,
                       opened_ts=ts)
        self.positions[s] = pos
        # TP sebagai LIMIT reduce-only (maker): partial +1.5R + sisa di TP.
        # SL stop-market sudah resting sejak penempatan entry.
        d = 1 if sig.direction == Direction.LONG else -1
        stop = pos.stop_dist
        legs = []
        q_partial = qty * self.cfg.partial_fraction
        if self.cfg.partial_fraction > 0:
            legs.append((sig.entry + d * self.cfg.partial_tp_r * stop, q_partial))
        legs.append((sig.tp, qty - q_partial if self.cfg.partial_fraction > 0 else qty))
        self.executor.place_reduce_limits(s, sig.direction.value, legs)
        self.executor.journal_open({
            "symbol": s, "side": sig.direction.value, "quantity": qty,
            "entry_plan": sig.entry, "entry_fill": fill_px,
            "stop": sig.sl, "tp1": sig.tp, "risk_usdt": pen.risk_amount,
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "opened_ts": ts, "setup": f"stupidbot:{sig.pattern}",
        })
        logger.info("🔴 LIVE limit terisi %s %s @ %.6g (SL %.6g, TP legs %s)",
                    sig.direction.value, s, fill_px, sig.sl,
                    [(round(p, 6), round(q, 6)) for p, q in legs])

    # ── Manajemen posisi PAPER ────────────────────────────────────────
    def _record_paper_close(self, s: str, pos: Position, ts: int) -> None:
        trade, pnl = close_position(self.cfg, s, pos, ts)
        self.paper_balance += pnl
        self.adaptive.on_trade_close(self.paper_balance)
        self.guard.on_trade_close(ts, self.paper_balance)
        self.journal.append(trade)
        logger.info("PAPER exit %s: %s pnl %+.2f (balance %.2f)",
                    s, trade.exit_reason, pnl, self.paper_balance)

    def _manage_paper(self, s: str, pos: Position, candle: Candle) -> None:
        closed = self.pm.on_candle(pos, candle, self.entry[s].atr.value)
        if closed:
            del self.positions[s]
            self._record_paper_close(s, pos, candle.ts)

    # ── Manajemen posisi LIVE ─────────────────────────────────────────
    def _manage_live(self, s: str, pos: Position, candle: Candle) -> None:
        d = 1 if pos.direction == Direction.LONG else -1
        stop = pos.stop_dist
        adverse = candle.low if d == 1 else candle.high
        favorable = candle.high if d == 1 else candle.low
        pos.mae_r = max(pos.mae_r, d * (pos.entry - adverse) / stop)
        pos.mfe_r = max(pos.mfe_r, d * (favorable - pos.entry) / stop)

        amt = abs(self.executor.position_amount(s))
        if amt == 0:
            # ditutup exchange (SL stop-market / TP limit terisi)
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
            self.executor.cancel_protection(s)  # SL algo + limit TP yatim
            self.executor.mark_closed(s, reason)
            bal = self._balance(refresh=True)
            self.adaptive.on_trade_close(bal)
            self.guard.on_trade_close(candle.ts, bal)
            del self.positions[s]
            logger.info("🔴 LIVE exit %s: %s (balance %.2f)", s, reason, bal)
            return

        dirty = False
        # leg partial TP (limit) terisi → qty exchange berkurang → SL ke BE
        if not pos.partial_done and amt < pos.qty * (1 - 1e-6):
            filled_q = pos.qty - amt
            partial_px = pos.entry + d * self.cfg.partial_tp_r * stop
            pos.fills.append(Fill(partial_px, filled_q, "PARTIAL_TP", candle.ts))
            pos.qty = amt
            pos.partial_done = True
            if self.cfg.be_after_partial and d * (pos.entry - pos.sl) > 0:
                pos.sl = pos.entry
                dirty = True
            logger.info("🔴 LIVE partial terisi %s: %.6g @ ~%.6g → SL ke BE",
                        s, filled_q, partial_px)

        # ATR trailing hanya setelah +trail_start_r
        atr = self.entry[s].atr.value
        r_close = d * (candle.close - pos.entry) / stop
        if r_close >= self.cfg.trail_start_r and atr:
            new_sl = candle.close - d * self.cfg.trail_atr_mult * atr
            if d * (new_sl - pos.sl) > 0:
                pos.sl = new_sl
                dirty = True

        if dirty:
            # hanya STOP_MARKET yang digeser; limit TP tidak tersentuh
            res = self.executor.sync_protection(s, pos.direction.value, pos.sl)
            if not res.get("ok"):
                logger.error("Proteksi %s gagal — posisi ditutup fail-safe", s)
                pos.fills.append(Fill(candle.close, pos.qty, "FAILSAFE", candle.ts))
                pos.qty = 0.0
                trade, _ = close_position(self.cfg, s, pos, candle.ts)
                self.journal.append(trade)
                self.executor.cancel_protection(s)
                self.executor.mark_closed(s, "FAILSAFE")
                del self.positions[s]

    # ── Entry: pasang order limit baru ────────────────────────────────
    def _maybe_enter(self, fresh: dict[str, Candle]) -> None:
        """Kandidat dari candle yang baru close, ranking structure_score,
        isi slot yang tersedia (posisi + pending menghitung slot)."""
        slots = (self.cfg.max_open_positions - len(self.positions)
                 - len(self.pendings))
        if slots <= 0 or not fresh:
            return
        now_ts = max(c.ts for c in fresh.values())
        if not self.guard.allowed(now_ts) or not self.throttle.allowed(now_ts):
            return

        candidates = []
        for s, candle in fresh.items():
            if s in self.positions or s in self.pendings:
                continue
            if candle.ts + self.step <= self.started_ts:
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
                self._place(s, sig, score)
            except Exception as e:
                logger.error("Penempatan limit %s gagal: %s", s, e)

    def _place(self, s: str, sig: Signal, score: float) -> None:
        risk_pct = self.adaptive.current_pct

        if self.live:
            plan = {"side": sig.direction.value, "entry": sig.entry,
                    "stop": sig.sl, "tp1": sig.tp, "tp2": sig.tp}
            prepared = self.executor.prepare_order(s, plan, risk_pct)
            logger.info(
                "🔴 LIVE limit %s %s | limit %.6g SL %.6g TP %.6g | qty %s "
                "notional $%.2f risk $%.2f (%.2f%%) RR %.2f | %s",
                prepared["side"], prepared["symbol"], sig.entry, sig.sl,
                sig.tp, prepared["quantity"], prepared["notional_usdt"],
                prepared["risk_usdt"], risk_pct, prepared["rr1"], sig.reason)
            order = self.executor.place_limit_entry(
                s, sig.direction.value, prepared["quantity"], sig.entry)
            try:
                # SL protektif resting SEBELUM order terisi — wajib
                self.executor.place_protective_sl(s, sig.direction.value, sig.sl)
            except Exception as e:
                logger.error("SL protektif %s gagal (%s) — limit entry ditarik", s, e)
                self.executor.cancel_order(s, order.get("orderId"))
                return
            qty = prepared["quantity"]
            risk_amount = prepared["risk_usdt"]
            self.pending_oid[s] = order.get("orderId")
        else:
            qty, risk_amount = position_size(self._balance(), risk_pct,
                                             sig.entry, sig.sl)
            if qty <= 0:
                return
            logger.info("PAPER limit %s %s | limit %.6g SL %.6g TP %.6g | "
                        "qty %.6g risk $%.2f (%.2f%%) | %s",
                        sig.direction.value, s, sig.entry, sig.sl, sig.tp,
                        qty, risk_amount, risk_pct, sig.reason)

        self.pendings[s] = PendingEntry(signal=sig, qty=qty,
                                        risk_amount=risk_amount,
                                        risk_pct=risk_pct, placed_ts=sig.ts)
        self.throttle.on_entry(sig.ts)

    # ── Loop ──────────────────────────────────────────────────────────
    def run(self, once: bool = False) -> None:
        mode = "🔴 LIVE (uang nyata)" if self.live else "📄 PAPER (simulasi)"
        logger.info("stupidbot runner mulai — mode %s, %s %s, risk tier %s%%, "
                    "entry LIMIT di bekas SL (no market order)",
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
                logger.info("Dihentikan — state tersimpan. Posisi/order LIVE "
                            "tetap terlindungi SL di exchange.")
                return
