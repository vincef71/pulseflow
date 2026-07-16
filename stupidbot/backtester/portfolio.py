"""Backtest mode portfolio: satu balance untuk banyak simbol.

Prinsip: bukan trading semua aset sekaligus — setiap saat hanya kandidat
dengan struktur Daily TERBAIK (structure_score tertinggi) yang boleh mengisi
slot posisi, dan hanya bila skornya melewati ambang minimal.

Semua lapisan proteksi berlaku pada level akun (bukan per simbol):
adaptive risk, equity guard, dan trade throttle dibagi bersama.
"""
from collections import defaultdict

from backtester.backtest import (DAY_MS, PendingEntry, close_position,
                                 fill_pending, pending_action, summarize)
from config.settings import Settings
from core.models import Candle, Direction, Trade
from daily_bias.bias import DailyBiasEngine
from entry_engine.engine import EntryEngine
from position_manager.manager import Fill, Position, PositionManager
from risk_manager.risk import AdaptiveRisk, EquityGuard, TradeThrottle, position_size


class PortfolioBacktester:
    def __init__(self, cfg: Settings, symbols: list[str], entry_tf: str,
                 start_balance: float):
        self.cfg = cfg
        self.symbols = symbols
        self.entry_tf = entry_tf
        self.start_balance = start_balance

    def run(self, data: dict[str, tuple[list[Candle], list[Candle]]],
            trade_from_ts: int) -> dict:
        cfg = self.cfg
        bias_engines = {s: DailyBiasEngine(cfg) for s in self.symbols}
        entry_engines = {s: EntryEngine(cfg) for s in self.symbols}
        pos_manager = PositionManager(cfg)
        adaptive = AdaptiveRisk(cfg, self.start_balance)
        guard = EquityGuard(cfg, self.start_balance)
        throttle = TradeThrottle(cfg)

        # susun candle entry per timestamp agar semua simbol berjalan serempak
        by_ts: dict[int, dict[str, Candle]] = defaultdict(dict)
        for s in self.symbols:
            for c in data[s][1]:
                by_ts[c.ts][s] = c
        timeline = sorted(by_ts.keys())
        di = {s: 0 for s in self.symbols}

        balance = self.start_balance
        trades: list[Trade] = []
        equity: list[tuple[int, float]] = [(trade_from_ts, balance)]
        positions: dict[str, Position] = {}
        pendings: dict[str, PendingEntry] = {}

        def _record_close(s: str, p: Position, ts: int) -> None:
            nonlocal balance
            trade, pnl = close_position(cfg, s, p, ts)
            balance += pnl
            adaptive.on_trade_close(balance)
            guard.on_trade_close(ts, balance)
            trades.append(trade)
            equity.append((ts, balance))

        for ts in timeline:
            candles = by_ts[ts]
            guard.on_candle(ts, balance)

            # 1. update engine per simbol (Daily yang sudah tuntas + TF entry)
            for s, candle in candles.items():
                daily = data[s][0]
                while di[s] < len(daily) and daily[di[s]].ts + DAY_MS <= candle.ts:
                    bias_engines[s].update(daily[di[s]])
                    di[s] += 1
                entry_engines[s].update(candle)

            # 2. kelola posisi terbuka
            for s in list(positions.keys()):
                candle = candles.get(s)
                pos = positions[s]
                if candle is None or candle.ts <= pos.opened_ts:
                    continue
                closed = pos_manager.on_candle(pos, candle, entry_engines[s].atr.value)
                if closed:
                    _record_close(s, pos, candle.ts)
                    del positions[s]

            # 3. nasib order limit yang menunggu
            for s in list(pendings.keys()):
                candle = candles.get(s)
                if candle is None:
                    continue
                if not guard.allowed(ts):
                    throttle.on_cancel()
                    del pendings[s]
                    continue
                bias, _ = bias_engines[s].bias()
                act = pending_action(pendings[s], candle, bias,
                                     entry_engines[s].tracker.trend)
                if act == "CANCEL":
                    throttle.on_cancel()
                    del pendings[s]
                elif act == "FILL":
                    pos, stopped = fill_pending(pendings[s], candle)
                    del pendings[s]
                    if stopped:
                        _record_close(s, pos, candle.ts)
                    else:
                        positions[s] = pos

            # 4. order baru — hanya struktur Daily terbaik yang boleh masuk
            if ts < trade_from_ts:
                continue
            slots = cfg.max_open_positions - len(positions) - len(pendings)
            if slots <= 0 or not guard.allowed(ts) or not throttle.allowed(ts):
                continue

            candidates: list[tuple[float, str, object]] = []
            for s, candle in candles.items():
                if s in positions or s in pendings:
                    continue
                bias, reason = bias_engines[s].bias()
                if bias == Direction.NEUTRAL:
                    continue
                signal = entry_engines[s].check(bias, reason, bias_engines[s])
                if signal is None:
                    continue
                score = bias_engines[s].structure_score()
                if score < cfg.min_structure_score:
                    continue
                candidates.append((score, s, signal))

            candidates.sort(key=lambda x: x[0], reverse=True)
            for score, s, signal in candidates[:slots]:
                if not throttle.allowed(ts):
                    break
                risk_pct = adaptive.current_pct
                qty, risk_amount = position_size(balance, risk_pct, signal.entry, signal.sl)
                if qty <= 0:
                    continue
                pendings[s] = PendingEntry(signal=signal, qty=qty,
                                           risk_amount=risk_amount,
                                           risk_pct=risk_pct, placed_ts=ts)
                throttle.on_entry(ts)

        # tutup posisi tersisa di candle terakhir masing-masing simbol
        for s, pos in positions.items():
            last = data[s][1][-1]
            pos.fills.append(Fill(last.close, pos.qty, "END_OF_DATA", last.ts))
            pos.qty = 0.0
            trade, pnl = close_position(cfg, s, pos, last.ts)
            balance += pnl
            trades.append(trade)
            equity.append((last.ts, balance))

        stats = summarize(trades, equity, self.start_balance, balance)
        return {"trades": trades, "equity": equity, "stats": stats,
                "final_balance": balance, "halts": guard.halts}
