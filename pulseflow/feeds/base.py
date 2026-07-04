import asyncio
import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, Any, Optional

logger = logging.getLogger("PulseFlow.Feeds")


class ConnectionStatus(Enum):
    IDLE         = "IDLE"
    CONNECTING   = "CONNECTING"
    CONNECTED    = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"
    ERROR        = "ERROR"


class BaseFeed(ABC):
    """
    Abstract base class for all exchange data feeds.
    Tracks connection status, trade counts, and last message time.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.is_running = False
        self.task: Optional[asyncio.Task] = None

        # Status tracking
        self.status = ConnectionStatus.IDLE
        self.trade_count: int = 0
        self.last_trade_time: float = 0.0

        # Callbacks
        self.on_trade_callback:       Optional[Callable[[float, float, bool, float], None]] = None
        self.on_liquidation_callback: Optional[Callable[[float, str, float, float], None]] = None
        self.on_oi_callback:          Optional[Callable[[float, float], None]] = None
        self.on_kline_callback:       Optional[Callable[[dict], None]] = None
        self.on_depth_callback:       Optional[Callable[[list, list, float], None]] = None
        self.on_deep_depth_callback:  Optional[Callable[[list, list, float], None]] = None
        self.on_status_callback:      Optional[Callable[[str, "ConnectionStatus", str], None]] = None

    def register_callbacks(
        self,
        on_trade: Callable[[float, float, bool, float], None],
        on_liq:   Optional[Callable[[float, str, float, float], None]] = None,
        on_oi:    Optional[Callable[[float, float], None]] = None,
        on_kline: Optional[Callable[[dict], None]] = None,
        on_depth: Optional[Callable[[list, list, float], None]] = None,
        on_deep_depth: Optional[Callable[[list, list, float], None]] = None,
    ):
        # Wrap on_trade to increment counters transparently
        _original = on_trade
        def _counting_trade(price: float, size: float, is_buyer_maker: bool, timestamp: float):
            self.trade_count += 1
            self.last_trade_time = timestamp
            _original(price, size, is_buyer_maker, timestamp)

        self.on_trade_callback       = _counting_trade
        self.on_liquidation_callback = on_liq
        self.on_oi_callback          = on_oi
        self.on_kline_callback       = on_kline
        self.on_depth_callback       = on_depth
        self.on_deep_depth_callback  = on_deep_depth

    def register_status_callback(self, callback: Callable[[str, "ConnectionStatus", str], None]):
        """Engine calls this to receive (symbol, status, message) events."""
        self.on_status_callback = callback

    def _emit_status(self, status: ConnectionStatus, message: str = ""):
        self.status = status
        logger.info(f"[{self.symbol}] {status.value}: {message}")
        if self.on_status_callback:
            self.on_status_callback(self.symbol, status, message)

    @abstractmethod
    async def connect_and_stream(self):
        pass

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._emit_status(ConnectionStatus.CONNECTING, "Initializing feed...")
        self.task = asyncio.create_task(self.connect_and_stream())

    async def stop(self):
        self.is_running = False
        self._emit_status(ConnectionStatus.DISCONNECTED, "Feed stopped.")
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info(f"Stopped feed stream for {self.symbol}")
