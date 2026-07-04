import asyncio
import random
import time
import logging
from pulseflow.feeds.base import BaseFeed, ConnectionStatus
from pulseflow.config.settings import DAILY_ATR_CONFIG

logger = logging.getLogger("PulseFlow.Replay")


class MarketReplayer(BaseFeed):
    """
    High-fidelity market simulator.
    Generates realistic microstructure scenarios: absorption, cascades, squeezes.
    """

    def __init__(self, symbol: str, speed_multiplier: float = 1.0):
        super().__init__(symbol)
        self.speed_multiplier = speed_multiplier
        self.simulated_price = 100.0 if symbol != "BTC" else 95000.0
        self.simulated_oi    = 500000.0

        # Synthetic daily session tracking (for daily ATR)
        self._day_open = self.simulated_price
        self._day_high = self.simulated_price
        self._day_low  = self.simulated_price
        self._last_candle_emit = 0.0

    def _seed_daily_candles(self):
        """Emit a run of synthetic closed daily candles + today's forming one."""
        if not self.on_kline_callback:
            return
        n = DAILY_ATR_CONFIG["period"] + 3
        day_ms = 86_400_000
        now_ms = int(time.time() * 1000)
        px = self.simulated_price
        # Typical daily range ≈ 2.5% of price
        for i in range(n - 1, 0, -1):
            rng  = px * random.uniform(0.015, 0.035)
            o    = px * random.uniform(0.99, 1.01)
            c    = o + random.uniform(-1, 1) * rng * 0.6
            hi   = max(o, c) + rng * random.uniform(0.1, 0.5)
            lo   = min(o, c) - rng * random.uniform(0.1, 0.5)
            self.on_kline_callback({
                "open_time": now_ms - i * day_ms,
                "open": o, "high": hi, "low": lo, "close": c,
                "is_closed": True,
            })
        # Today (forming)
        self._day_open = self._day_high = self._day_low = px
        self._emit_today_candle(now_ms)

    def _emit_today_candle(self, now_ms: int):
        if not self.on_kline_callback:
            return
        self.on_kline_callback({
            "open_time": now_ms - (now_ms % 86_400_000),
            "open":  self._day_open,
            "high":  self._day_high,
            "low":   self._day_low,
            "close": self.simulated_price,
            "is_closed": False,
        })

    async def connect_and_stream(self):
        self._emit_status(ConnectionStatus.CONNECTING, f"Initializing simulation for {self.symbol}…")
        await asyncio.sleep(0.05)
        self._emit_status(ConnectionStatus.CONNECTED, f"Simulation active — synthetic microstructure for {self.symbol}")

        self._seed_daily_candles()

        while self.is_running:
            try:
                regime_rand = random.random()

                trade_frequency  = 0.05
                size_min, size_max = 0.01, 1.5
                volatility  = 0.0002
                buy_bias    = 0.5

                if regime_rand < 0.02:
                    trade_frequency = 0.005
                    buy_bias        = 0.2
                    volatility      = 0.001
                    logger.debug(f"[{self.symbol}] Simulation: LONG SQUEEZE triggered")
                    for _ in range(random.randint(5, 15)):
                        liq_val = random.uniform(5000.0, 45000.0)
                        if self.on_liquidation_callback:
                            self.on_liquidation_callback(liq_val, "SELL", self.simulated_price, time.time())
                        self.simulated_price *= (1.0 - random.uniform(0.0001, 0.0005))
                        self.simulated_oi   -= liq_val * 0.1

                elif regime_rand < 0.04:
                    trade_frequency = 0.005
                    buy_bias        = 0.8
                    volatility      = 0.001
                    logger.debug(f"[{self.symbol}] Simulation: SHORT SQUEEZE triggered")
                    for _ in range(random.randint(5, 15)):
                        liq_val = random.uniform(5000.0, 45000.0)
                        if self.on_liquidation_callback:
                            self.on_liquidation_callback(liq_val, "BUY", self.simulated_price, time.time())
                        self.simulated_price *= (1.0 + random.uniform(0.0001, 0.0005))
                        self.simulated_oi   -= liq_val * 0.1

                elif regime_rand < 0.06:
                    trade_frequency = 0.002
                    buy_bias        = 0.9
                    volatility      = 0.00001
                    logger.debug(f"[{self.symbol}] Simulation: PASSIVE ABSORPTION triggered")

                for _ in range(random.randint(1, 5)):
                    is_buyer_maker  = random.random() > buy_bias
                    size            = random.uniform(size_min, size_max)
                    price_direction = -1 if is_buyer_maker else 1
                    price_change    = self.simulated_price * volatility * price_direction * random.random()
                    self.simulated_price += price_change

                    oi_change = size * self.simulated_price * random.choice([1, -1]) * 0.5
                    self.simulated_oi += oi_change
                    if self.simulated_oi < 10000:
                        self.simulated_oi = 500000.0

                    if self.on_trade_callback:
                        self.on_trade_callback(self.simulated_price, size, is_buyer_maker, time.time())

                if random.random() < 0.2:
                    if self.on_oi_callback:
                        self.on_oi_callback(self.simulated_oi, time.time())

                # Track today's synthetic session range and emit ~every 2 s
                self._day_high = max(self._day_high, self.simulated_price)
                self._day_low  = min(self._day_low, self.simulated_price)
                now = time.time()
                if now - self._last_candle_emit > 2.0:
                    self._emit_today_candle(int(now * 1000))
                    self._last_candle_emit = now

                await asyncio.sleep((trade_frequency * random.uniform(0.5, 1.5)) / self.speed_multiplier)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in simulation loop for {self.symbol}: {e}")
                await asyncio.sleep(1)

        self._emit_status(ConnectionStatus.DISCONNECTED, f"Simulation stopped for {self.symbol}")
