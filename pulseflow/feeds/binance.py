import asyncio
import json
import logging
import aiohttp
import websockets
from pulseflow.feeds.base import BaseFeed, ConnectionStatus
from pulseflow.config.settings import DAILY_ATR_CONFIG

logger = logging.getLogger("PulseFlow.Binance")

_BINANCE_REST = "https://fapi.binance.com"


class BinanceFeed(BaseFeed):
    """
    Real-time feed for Binance Futures.

    Streams
    -------
    • aggTrade  — every executed trade (price, size, side)
    • forceOrder — liquidation events
    • depth20   — top-20 order book snapshot every 100 ms (for the Bookmap-style
                  liquidity heatmap); partial-depth stream is a full snapshot,
                  not a diff, so each message is used directly
    • OI        — polled via REST /fapi/v1/openInterest every 3 s
                  (Binance Futures has no WebSocket OI stream)
    • kline_1d  — live forming daily candle (for daily ATR); historical
                  daily candles are seeded once via REST /fapi/v1/klines
    """

    def __init__(self, symbol: str):
        self.binance_symbol = (
            f"{symbol.lower()}usdt"
            if not symbol.upper().endswith("USDT")
            else symbol.lower()
        )
        super().__init__(symbol)
        self._interval = DAILY_ATR_CONFIG["interval"]
        self._ws_url = (
            f"wss://fstream.binance.com/market/stream"
            f"?streams={self.binance_symbol}@aggTrade"
            f"/{self.binance_symbol}@forceOrder"
            f"/{self.binance_symbol}@kline_{self._interval}"
        )
        # Order book depth lives on the /public route (the /market route does
        # not deliver it); kept on its own WebSocket connection.
        self._depth_ws_url = (
            f"wss://fstream.binance.com/public/stream"
            f"?streams={self.binance_symbol}@depth20@100ms"
        )
        self._oi_url = (
            f"{_BINANCE_REST}/fapi/v1/openInterest"
            f"?symbol={self.binance_symbol.upper()}"
        )
        # Deep order book (up to 1000 levels each side) — captures BIG resting
        # walls far from price (macro liquidity pools). REST poll; weight 20.
        self._deep_depth_url = (
            f"{_BINANCE_REST}/fapi/v1/depth"
            f"?symbol={self.binance_symbol.upper()}&limit=1000"
        )
        self._klines_url = (
            f"{_BINANCE_REST}/fapi/v1/klines"
            f"?symbol={self.binance_symbol.upper()}"
            f"&interval={self._interval}"
            f"&limit={DAILY_ATR_CONFIG['period'] + 3}"
        )

    # ── Entry point ───────────────────────────────────────────────────

    async def connect_and_stream(self):
        """Run WebSocket stream and OI poller concurrently."""
        # Seed historical daily candles before the live stream takes over.
        await self._seed_daily_klines()
        try:
            await asyncio.gather(
                self._ws_loop(),
                self._depth_ws_loop(),
                self._oi_poll_loop(),
                self._deep_depth_poll_loop(),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            pass

    # ── Daily kline REST seed ─────────────────────────────────────────

    async def _seed_daily_klines(self):
        """
        Fetch the recent daily candles once so the daily-ATR series has
        history immediately. The final row is the still-forming session
        and is emitted as a live (not closed) candle.
        """
        if not self.on_kline_callback:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._klines_url, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"Binance kline seed HTTP {resp.status} for {self.binance_symbol}"
                        )
                        return
                    rows = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"Binance kline seed error {self.binance_symbol}: {e}")
            return

        for i, row in enumerate(rows):
            # row: [openTime, open, high, low, close, volume, closeTime, ...]
            self.on_kline_callback({
                "open_time": int(row[0]),
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
                "is_closed": i < len(rows) - 1,   # last row = current session
            })

    # ── WebSocket stream ──────────────────────────────────────────────

    async def _ws_loop(self):
        while self.is_running:
            try:
                self._emit_status(
                    ConnectionStatus.CONNECTING,
                    f"Connecting: {self.binance_symbol}@aggTrade + @forceOrder",
                )
                async with websockets.connect(
                    self._ws_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    self._emit_status(
                        ConnectionStatus.CONNECTED,
                        f"WS stream active: {self.binance_symbol}",
                    )
                    async for raw in ws:
                        if not self.is_running:
                            break
                        self._dispatch_ws(raw)

            except websockets.exceptions.ConnectionClosed as e:
                self._emit_status(
                    ConnectionStatus.RECONNECTING,
                    f"WS closed (code {e.code}). Retry in 3 s…",
                )
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._emit_status(ConnectionStatus.ERROR, str(e)[:100])
                await asyncio.sleep(3)

        self._emit_status(ConnectionStatus.DISCONNECTED, f"WS feed stopped for {self.symbol}")

    async def _depth_ws_loop(self):
        """
        Separate connection for the order book depth stream on the /public
        route. Stays quiet on the connection log (the primary _ws_loop owns
        the symbol's status); depth simply reconnects on its own if dropped.
        Reuses _dispatch_ws, which routes @depth messages to on_depth_callback.
        """
        while self.is_running:
            try:
                async with websockets.connect(
                    self._depth_ws_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.info(f"Depth WS active: {self.binance_symbol}@depth20")
                    async for raw in ws:
                        if not self.is_running:
                            break
                        self._dispatch_ws(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Depth WS error {self.binance_symbol}: {e}")
                await asyncio.sleep(3)

    def _dispatch_ws(self, raw: str):
        try:
            data    = json.loads(raw)
            stream  = data.get("stream", "")
            payload = data.get("data")
            if not payload:
                return

            if stream.endswith("@aggTrade"):
                price          = float(payload["p"])
                size           = float(payload["q"])
                is_buyer_maker = bool(payload["m"])
                timestamp      = float(payload["T"]) / 1000.0
                if self.on_trade_callback:
                    self.on_trade_callback(price, size, is_buyer_maker, timestamp)

            elif stream.endswith("@forceOrder"):
                order     = payload.get("o", {})
                side      = order.get("S", "BUY")
                price     = float(order.get("ap", 0.0))
                quantity  = float(order.get("q", 0.0))
                usd_value = price * quantity
                timestamp = float(order.get("T", 0.0)) / 1000.0
                if self.on_liquidation_callback:
                    self.on_liquidation_callback(usd_value, side, price, timestamp)

            elif "@depth" in stream:
                # Partial book depth snapshot (top-20): "b"=bids, "a"=asks,
                # each entry ["price", "qty"]. Used directly (full snapshot).
                if self.on_depth_callback:
                    bids = [(float(p), float(q)) for p, q in payload.get("b", [])]
                    asks = [(float(p), float(q)) for p, q in payload.get("a", [])]
                    ts   = float(payload.get("E", 0.0)) / 1000.0
                    self.on_depth_callback(bids, asks, ts)

            elif "@kline_" in stream:
                k = payload.get("k", {})
                if k and self.on_kline_callback:
                    self.on_kline_callback({
                        "open_time": int(k.get("t", 0)),
                        "open":  float(k["o"]),
                        "high":  float(k["h"]),
                        "low":   float(k["l"]),
                        "close": float(k["c"]),
                        "is_closed": bool(k.get("x", False)),
                    })

        except Exception as e:
            logger.error(f"Binance dispatch error: {e}")

    # ── OI REST poller ────────────────────────────────────────────────

    async def _oi_poll_loop(self):
        """Poll /fapi/v1/openInterest every 3 s (no WS stream exists for OI)."""
        await asyncio.sleep(1)   # let WS connect first
        async with aiohttp.ClientSession() as session:
            while self.is_running:
                try:
                    async with session.get(
                        self._oi_url,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            oi   = float(data["openInterest"])
                            ts   = float(data["time"]) / 1000.0
                            if self.on_oi_callback:
                                self.on_oi_callback(oi, ts)
                        else:
                            logger.warning(
                                f"Binance OI poll HTTP {resp.status} for {self.binance_symbol}"
                            )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Binance OI poll error {self.binance_symbol}: {e}")

                await asyncio.sleep(3)

    # ── Deep order book REST poller ───────────────────────────────────

    async def _deep_depth_poll_loop(self):
        """Poll /fapi/v1/depth?limit=1000 every 5 s → big resting walls far from
        price (macro liquidity pools). No WS stream covers this depth."""
        if not self.on_deep_depth_callback:
            return
        await asyncio.sleep(2)   # let WS connect first
        async with aiohttp.ClientSession() as session:
            while self.is_running:
                try:
                    async with session.get(
                        self._deep_depth_url,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
                            asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
                            ts   = float(data.get("E", 0.0)) / 1000.0
                            if bids and asks:
                                self.on_deep_depth_callback(bids, asks, ts)
                        else:
                            logger.warning(
                                f"Binance deep-depth HTTP {resp.status} for {self.binance_symbol}"
                            )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Binance deep-depth poll error {self.binance_symbol}: {e}")

                await asyncio.sleep(5)
