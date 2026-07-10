import asyncio
import time
import logging
import threading
from typing import Dict, Any, List, Callable, Optional
from pulseflow.config.settings import (DEFAULT_SYMBOLS, TICK_INTERVAL_MS,
                                        DAILY_ATR_CONFIG, HTF_BIAS_CONFIG)
from pulseflow.core.buffer import MarketTicker
from pulseflow.velocity.calculator import VelocityCalculator
from pulseflow.signals.detector import SignalDetector
from pulseflow.battle.engine import BattleStateEngine
from pulseflow.liquidity.probability_engine import LiquidityProbabilityEngine
from pulseflow.liquidity.macro_engine import MacroLiquidityEngine
from pulseflow.entry.engine import EntrySignalEngine
from pulseflow.analytics.daily_atr import DailyATRTracker
from pulseflow.analytics.context import MarketContextTracker
from pulseflow.analytics.htf_bias import HTFBiasTracker
from pulseflow.storage.parquet_writer import ParquetWriter
from pulseflow.feeds.hyperliquid import HyperliquidFeed
from pulseflow.feeds.binance import BinanceFeed

logger = logging.getLogger("PulseFlow.Engine")

class PulseEngine:
    """
    Core engine coordinating data ingestion, rolling calculations, 
    real-time anomaly and signal detection, UI state updates, and storage.
    """
    def __init__(self, mode: str = "live", symbols: Optional[List[str]] = None,
                 htf_interval: Optional[str] = None):
        self.mode = mode  # "live" or "simulated"
        self.symbols = symbols or DEFAULT_SYMBOLS
        self.htf_interval = (htf_interval or HTF_BIAS_CONFIG["interval"]).lower()
        self.is_running = False
        
        # Pipelines for each active symbol
        self.tickers: Dict[str, MarketTicker] = {}
        self.calculators: Dict[str, VelocityCalculator] = {}
        self.detectors: Dict[str, SignalDetector] = {}
        self.battle_engines: Dict[str, BattleStateEngine] = {}
        self.liquidity_engines: Dict[str, LiquidityProbabilityEngine] = {}
        self.macro_liquidity_engines: Dict[str, MacroLiquidityEngine] = {}
        self.entry_engines: Dict[str, EntrySignalEngine] = {}
        self.daily_atr: Dict[str, DailyATRTracker] = {}
        self.context_trackers: Dict[str, MarketContextTracker] = {}
        self.htf_bias: Dict[str, HTFBiasTracker] = {}
        self.feeds: Dict[str, List[Any]] = {}
        
        # Storage
        self.storage = ParquetWriter()
        self._tick_counter = 0   # siklus loop (untuk throttle logging node)
        
        # Callbacks for UI components
        self.ui_update_callback:     Optional[Callable[[str, Dict[str, Any], List[Dict[str, Any]]], None]] = None
        self.signal_callback:        Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.feed_status_callback:   Optional[Callable[[str, str, Any, str], None]] = None

        # Raw trade callbacks — untuk FootprintWindow dan sejenisnya
        self._raw_trade_callbacks: List[Callable] = []
        self._raw_trade_cb_lock = threading.Lock()

        # Depth (order book) callbacks — untuk heatmap Bookmap
        self._depth_callbacks: List[Callable] = []
        self._depth_cb_lock = threading.Lock()

        self._init_pipelines()

    def _init_pipelines(self):
        for symbol in self.symbols:
            ticker = MarketTicker(symbol=symbol)
            calc = VelocityCalculator(ticker)
            det = SignalDetector(ticker, calc)
            battle = BattleStateEngine(symbol=symbol)

            self.tickers[symbol] = ticker
            self.calculators[symbol] = calc
            self.detectors[symbol] = det
            self.battle_engines[symbol] = battle
            self.liquidity_engines[symbol] = LiquidityProbabilityEngine(symbol=symbol)
            self.macro_liquidity_engines[symbol] = MacroLiquidityEngine(symbol=symbol)
            self.entry_engines[symbol] = EntrySignalEngine(symbol=symbol)
            self.daily_atr[symbol] = DailyATRTracker(period=DAILY_ATR_CONFIG["period"])
            self.context_trackers[symbol] = MarketContextTracker(symbol=symbol)
            self.htf_bias[symbol] = HTFBiasTracker(
                symbol=symbol, interval=self.htf_interval)
            self.feeds[symbol] = []

    def register_ui_callback(self, callback: Callable[[str, Dict[str, Any], List[Dict[str, Any]]], None]):
        """UI registers a callback to receive computed metrics & signals every 100ms."""
        self.ui_update_callback = callback

    def register_signal_callback(self, callback: Callable[[str, Dict[str, Any]], None]):
        """For audio alerts / desktop popups."""
        self.signal_callback = callback

    def register_feed_status_callback(self, callback: Callable[[str, str, Any, str], None]):
        """Receives (symbol, feed_name, status, message) when feed connection state changes."""
        self.feed_status_callback = callback

    def register_raw_trade_callback(self, callback: Callable):
        """Subscribe ke setiap trade mentah: callback(symbol, price, size, is_buyer_maker)."""
        with self._raw_trade_cb_lock:
            self._raw_trade_callbacks.append(callback)

    def unregister_raw_trade_callback(self, callback: Callable):
        with self._raw_trade_cb_lock:
            if callback in self._raw_trade_callbacks:
                self._raw_trade_callbacks.remove(callback)

    def register_depth_callback(self, callback: Callable):
        """Subscribe ke snapshot order book: callback(symbol, bids, asks, ts)."""
        with self._depth_cb_lock:
            self._depth_callbacks.append(callback)

    def unregister_depth_callback(self, callback: Callable):
        with self._depth_cb_lock:
            if callback in self._depth_callbacks:
                self._depth_callbacks.remove(callback)

    def set_htf_interval(self, interval: str) -> bool:
        """Ganti timeframe bias HTF untuk SEMUA symbol runtime (GUI / headless).
        Return True bila interval valid & diterapkan."""
        interval = (interval or "").lower()
        if interval not in HTF_BIAS_CONFIG["allowed"]:
            logger.warning("Interval HTF %r tidak diizinkan (pilihan: %s)",
                           interval, ", ".join(HTF_BIAS_CONFIG["allowed"]))
            return False
        self.htf_interval = interval
        for tracker in self.htf_bias.values():
            tracker.set_interval(interval)
        logger.info("Interval bias HTF diganti → %s (semua symbol)", interval)
        return True

    @property
    def tick_count(self) -> int:
        """Jumlah iterasi loop engine sejak start — untuk diagnostik tick rate
        (target 1000/TICK_INTERVAL_MS per detik; lebih rendah = CPU keteteran)."""
        return self._tick_counter

    def get_feed_stats(self, symbol: str):
        """Returns (trade_count, last_trade_time) for the primary feed of a symbol."""
        feeds = self.feeds.get(symbol, [])
        if feeds:
            return feeds[0].trade_count, feeds[0].last_trade_time
        return 0, 0.0

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        
        # Seed konteks klines 1m (REST, background) — mode live saja;
        # mode simulated membangun bar dari trade replay secara bertahap.
        if self.mode in ("binance", "hyperliquid"):
            for symbol in self.symbols:
                self.context_trackers[symbol].seed_history_async()
                self.htf_bias[symbol].start()   # REST 4h, refresh berkala

        # Start feeds
        for symbol in self.symbols:
            if self.mode == "hyperliquid":
                hl_feed = HyperliquidFeed(symbol)
                hl_feed.register_callbacks(
                    on_trade=lambda p, s, b_m, t, sym=symbol: self.on_trade(sym, p, s, b_m, t),
                    on_oi=lambda oi, t, sym=symbol: self.on_oi(sym, oi),
                    on_kline=lambda k, sym=symbol: self.on_kline(sym, k),
                )
                hl_feed.register_status_callback(
                    lambda sym2, st, msg, sym=symbol: self._on_feed_status(sym, "hyperliquid", st, msg)
                )
                hl_feed.start()
                self.feeds[symbol].append(hl_feed)

            elif self.mode == "binance":
                binance_feed = BinanceFeed(symbol)
                binance_feed.register_callbacks(
                    on_trade=lambda p, s, b_m, t, sym=symbol: self.on_trade(sym, p, s, b_m, t),
                    on_liq=lambda v, sd, p, t, sym=symbol: self.on_liquidation(sym, v, sd, p, t),
                    on_oi=lambda oi, t, sym=symbol: self.on_oi(sym, oi),
                    on_kline=lambda k, sym=symbol: self.on_kline(sym, k),
                    on_depth=lambda b, a, t, sym=symbol: self.on_depth(sym, b, a, t),
                    on_deep_depth=lambda b, a, t, sym=symbol: self.on_deep_depth(sym, b, a, t),
                )
                binance_feed.register_status_callback(
                    lambda sym2, st, msg, sym=symbol: self._on_feed_status(sym, "binance", st, msg)
                )
                binance_feed.start()
                self.feeds[symbol].append(binance_feed)

            else:
                # Simulated Feed
                from pulseflow.replay.replayer import MarketReplayer
                sim_feed = MarketReplayer(symbol)
                sim_feed.register_callbacks(
                    on_trade=lambda p, s, b_m, t, sym=symbol: self.on_trade(sym, p, s, b_m, t),
                    on_liq=lambda v, sd, p, t, sym=symbol: self.on_liquidation(sym, v, sd, p, t),
                    on_oi=lambda oi, t, sym=symbol: self.on_oi(sym, oi),
                    on_kline=lambda k, sym=symbol: self.on_kline(sym, k),
                )
                sim_feed.register_status_callback(
                    lambda sym2, st, msg, sym=symbol: self._on_feed_status(sym, "simulated", st, msg)
                )
                sim_feed.start()
                self.feeds[symbol].append(sim_feed)

        # Start periodic rolling calculation loop
        asyncio.create_task(self._engine_loop())
        logger.info(f"PulseFlow Orchestrator started in '{self.mode}' mode with symbols: {self.symbols}")

    def _on_feed_status(self, symbol: str, feed_name: str, status, message: str):
        if self.feed_status_callback:
            self.feed_status_callback(symbol, feed_name, status, message)

    def on_trade(self, symbol: str, price: float, size: float, is_buyer_maker: bool, timestamp: float):
        self.tickers[symbol].add_trade(price, size, is_buyer_maker)
        self.context_trackers[symbol].on_trade(price, size, timestamp)
        self.liquidity_engines[symbol].on_trade(price, size, is_buyer_maker, timestamp)
        self.macro_liquidity_engines[symbol].on_trade(price, size, is_buyer_maker, timestamp)
        self.storage.write_trade(symbol, price, size, is_buyer_maker, timestamp)
        with self._raw_trade_cb_lock:
            cbs = list(self._raw_trade_callbacks)
        for cb in cbs:
            try:
                cb(symbol, price, size, is_buyer_maker)
            except Exception:
                pass

    def on_depth(self, symbol: str, bids: list, asks: list, timestamp: float):
        """Fan-out order book snapshot ke subscriber (heatmap) + liquidity engine.
        Tidak di-persist."""
        self.liquidity_engines[symbol].on_depth(bids, asks, timestamp)
        with self._depth_cb_lock:
            cbs = list(self._depth_callbacks)
        for cb in cbs:
            try:
                cb(symbol, bids, asks, timestamp)
            except Exception:
                pass

    def on_deep_depth(self, symbol: str, bids: list, asks: list, timestamp: float):
        """Deep order book snapshot → macro liquidity engine (Binance only)."""
        self.macro_liquidity_engines[symbol].on_deep_depth(bids, asks, timestamp)

    def on_liquidation(self, symbol: str, usd_value: float, side: str, price: float, timestamp: float):
        self.tickers[symbol].add_liquidation(usd_value, side)
        self.macro_liquidity_engines[symbol].on_liquidation(usd_value, side, price, timestamp)
        self.storage.write_liquidation(symbol, usd_value, side, timestamp)

    def on_oi(self, symbol: str, oi_value: float):
        self.tickers[symbol].update_oi(oi_value)

    def on_kline(self, symbol: str, kline: dict):
        tracker = self.daily_atr.get(symbol)
        if tracker is not None:
            tracker.on_kline(kline)

    async def _engine_loop(self):
        """
        Runs continuously in the background. Every 100ms:
        1. Rolls active window ticker buckets.
        2. Calculates instantaneous + rolling velocity statistics.
        3. Invokes real-time anomaly detection.
        4. Notifies the UI and stores logs.
        """
        interval = TICK_INTERVAL_MS / 1000.0
        while self.is_running:
            start_time = time.time()
            self._tick_counter += 1

            for symbol in self.symbols:
                ticker = self.tickers[symbol]
                calc = self.calculators[symbol]
                det = self.detectors[symbol]
                
                # Roll tick data (resets accumulators)
                ticker.roll_tick()
                
                # Compute metrics
                metrics = calc.compute_metrics()

                # Derive battlefield state from metrics (Battle State Engine)
                metrics["battle"] = self.battle_engines[symbol].update(metrics, ticker.last_price)

                # Predict where liquidity is likely to form next (5–30s ahead)
                metrics["liquidity"] = self.liquidity_engines[symbol].update(
                    metrics, metrics["battle"], ticker.last_price, metrics.get("atr")
                )

                # Daily ATR vs current price movement
                metrics["daily_atr"] = self.daily_atr[symbol].snapshot(ticker.last_price)

                # Konteks klines 1m (bias trend, ATR struktural, swing levels)
                metrics["context"] = self.context_trackers[symbol].snapshot(ticker.last_price)

                # Bias trend 4H (REST, refresh 5 menit) — filter arah entry
                metrics["bias_4h"] = self.htf_bias[symbol].snapshot()

                # Predict BIG liquidity pools (macro magnets, far-reaching)
                metrics["macro_liquidity"] = self.macro_liquidity_engines[symbol].update(
                    ticker.last_price, metrics.get("daily_atr")
                )

                # Gabungkan semua engine jadi satu keputusan entry
                # (LONG/SHORT/WAIT + skor confluence + trade plan)
                metrics["entry"] = self.entry_engines[symbol].update(
                    metrics, metrics["battle"], metrics["liquidity"],
                    metrics["macro_liquidity"], metrics["daily_atr"],
                    ticker.last_price,
                    context=metrics["context"],
                )

                # Evaluate signals
                signals = det.evaluate_signals(metrics)
                
                # Persistence
                self.storage.write_metrics(symbol, metrics, start_time)

                # Log LiquidityNode lifecycle ~tiap 2 s (untuk kalibrasi/ML)
                if self._tick_counter % 20 == 0:
                    self.storage.write_liquidity_nodes(
                        symbol, self.liquidity_engines[symbol].snapshot_nodes(), start_time
                    )
                for sig in signals:
                    self.storage.write_signal(symbol, sig)
                    if self.signal_callback:
                        self.signal_callback(symbol, sig)
                
                # Update UI
                if self.ui_update_callback:
                    try:
                        self.ui_update_callback(symbol, metrics, signals)
                    except Exception as e:
                        logger.error(f"UI update callback failed for {symbol}: {e}")

            # Accurate rate sleep
            elapsed = time.time() - start_time
            sleep_time = max(0.001, interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def stop(self):
        self.is_running = False
        for tracker in self.htf_bias.values():
            tracker.stop()
        # Stop all feeds
        for feeds_list in self.feeds.values():
            for feed in feeds_list:
                await feed.stop()
        self.storage.flush()
        logger.info("PulseFlow Orchestrator stopped.")
