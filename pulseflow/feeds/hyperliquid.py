import asyncio
import logging
import time
import json
import threading
import requests
import websocket
from pulseflow.feeds.base import BaseFeed, ConnectionStatus
from pulseflow.config.settings import DAILY_ATR_CONFIG
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger("PulseFlow.Hyperliquid")

HL_REST_URL = "https://api.hyperliquid.xyz/info"
HL_WS_URL   = "wss://api.hyperliquid.xyz/ws"


def _is_hip3(symbol: str) -> bool:
    """HIP-3 / DEX coin identifiers always contain ':'  (e.g. xyz:NVDA)."""
    return ':' in symbol


class HyperliquidFeed(BaseFeed):
    """
    Real-time feed for Hyperliquid.

    Supports standard perp coins (BTC, ETH …) and HIP-3 / DEX coins
    (xyz:NVDA, xyz:XYZ100, …).

    Streams
    -------
    • trades  — SDK WebSocket for standard perps;
                raw WebSocket for HIP-3 (SDK doesn't subscribe correctly)
    • OI      — polled via REST meta_and_asset_ctxs() every 5 s
                (HIP-3 coins are not in the standard universe; OI is skipped)
    """

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._is_hip3 = _is_hip3(symbol)
        # SDK is only used for standard perps
        self.info = Info(constants.MAINNET_API_URL, skip_ws=False) if not self._is_hip3 else None
        self._coin_index: int | None = None   # cached index into asset_ctxs list
        # Raw WS handle for HIP-3 paths
        self._raw_ws = None
        self._raw_ws_thread: threading.Thread | None = None

    # ── Entry point ───────────────────────────────────────────────────

    async def connect_and_stream(self):
        if self._is_hip3:
            await self._connect_hip3()
        else:
            await self._connect_sdk()

    # ── Standard perp path (SDK) ──────────────────────────────────────

    async def _connect_sdk(self):
        self._emit_status(
            ConnectionStatus.CONNECTING,
            f"Subscribing Hyperliquid SDK trades for {self.symbol}...",
        )

        def on_trade_msg(msg):
            if msg.get("channel") != "trades" or "data" not in msg:
                return
            for trade in msg["data"]:
                if trade.get("coin") != self.symbol:
                    continue
                try:
                    price          = float(trade["px"])
                    size           = float(trade["sz"])
                    is_buyer_maker = trade["side"] == "S"
                    timestamp      = float(trade["time"]) / 1000.0
                    if self.on_trade_callback:
                        self.on_trade_callback(price, size, is_buyer_maker, timestamp)
                except Exception as e:
                    logger.error(f"Hyperliquid trade parse error: {e}")

        try:
            self.info.subscribe(
                {"type": "trades", "coin": self.symbol}, on_trade_msg
            )
            self._emit_status(
                ConnectionStatus.CONNECTED,
                f"SDK trades active ({self.symbol}) — OI via REST poll",
            )
        except Exception as e:
            self._emit_status(ConnectionStatus.ERROR, f"Subscribe failed: {e}")
            return

        try:
            await asyncio.gather(
                self._keepalive_loop(),
                self._oi_poll_loop(),
                self._daily_candle_poll_loop(),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            pass

    # ── HIP-3 / DEX coin path (raw WebSocket) ────────────────────────

    async def _connect_hip3(self):
        """
        HIP-3 coins (xyz:…) are not handled correctly by the SDK.
        Use a raw WebSocket subscription instead, matching the Hyperliquid
        public WS API directly.
        """
        self._emit_status(
            ConnectionStatus.CONNECTING,
            f"Connecting raw WS for HIP-3 coin {self.symbol}...",
        )

        symbol = self.symbol
        loop   = asyncio.get_event_loop()

        def on_open(ws):
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": symbol},
            }))
            loop.call_soon_threadsafe(
                self._emit_status,
                ConnectionStatus.CONNECTED,
                f"Raw WS trades active ({symbol}) — HIP-3 mode",
            )

        def on_msg(ws, raw):
            try:
                msg = json.loads(raw)
            except Exception:
                return
            if msg.get("channel") != "trades" or "data" not in msg:
                return
            items = msg["data"] if isinstance(msg["data"], list) else [msg["data"]]
            for trade in items:
                try:
                    px  = float(trade.get("px", 0))
                    sz  = float(trade.get("sz", 0))
                    buy = trade.get("side", "") == "B"
                    ts  = float(trade.get("time", time.time() * 1000)) / 1000.0
                    if px and sz and self.on_trade_callback:
                        self.on_trade_callback(px, sz, not buy, ts)
                except Exception as e:
                    logger.error(f"HIP-3 trade parse error ({symbol}): {e}")

        def on_close(ws, *_):
            if self.is_running:
                loop.call_soon_threadsafe(
                    self._emit_status, ConnectionStatus.ERROR, "WS disconnected"
                )

        def run_ws():
            while self.is_running:
                ws = websocket.WebSocketApp(
                    HL_WS_URL,
                    on_open=on_open,
                    on_message=on_msg,
                    on_close=on_close,
                    on_error=lambda ws, e: logger.error(f"HIP-3 WS error: {e}"),
                )
                self._raw_ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
                if self.is_running:
                    time.sleep(3)

        self._raw_ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._raw_ws_thread.start()

        try:
            await asyncio.gather(
                self._keepalive_loop(),
                self._daily_candle_poll_loop(),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            pass

    # ── Helpers ───────────────────────────────────────────────────────

    async def _daily_candle_poll_loop(self):
        """
        Poll daily candles via REST candleSnapshot every 60 s and feed them
        to the daily-ATR tracker. Works for standard perps and HIP-3 coins.
        The most recent candle is the still-forming session (not closed).
        """
        if not self.on_kline_callback:
            return

        interval = DAILY_ATR_CONFIG["interval"]
        n_days   = DAILY_ATR_CONFIG["period"] + 3
        day_ms   = 86_400_000
        loop     = asyncio.get_event_loop()

        def _fetch():
            end   = int(time.time() * 1000)
            start = end - n_days * day_ms
            resp = requests.post(
                HL_REST_URL,
                json={
                    "type": "candleSnapshot",
                    "req": {"coin": self.symbol, "interval": interval,
                            "startTime": start, "endTime": end},
                },
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json()

        while self.is_running:
            try:
                candles = await loop.run_in_executor(None, _fetch)
                if isinstance(candles, list) and candles:
                    for i, c in enumerate(candles):
                        self.on_kline_callback({
                            "open_time": int(c.get("t", 0)),
                            "open":  float(c["o"]),
                            "high":  float(c["h"]),
                            "low":   float(c["l"]),
                            "close": float(c["c"]),
                            "is_closed": i < len(candles) - 1,
                        })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Hyperliquid candle poll error for {self.symbol}: {e}")

            await asyncio.sleep(60)

    async def _keepalive_loop(self):
        """Keep the asyncio task alive while the SDK thread handles messages."""
        while self.is_running:
            await asyncio.sleep(1)

    async def _oi_poll_loop(self):
        """
        Poll Hyperliquid REST meta_and_asset_ctxs() every 5 s.

        HIP-3 / DEX coins are not part of the standard universe; OI is
        not available for them via this endpoint, so the loop exits early.
        """
        if self._is_hip3:
            logger.info(
                f"Hyperliquid OI: skipping poll for HIP-3 coin {self.symbol!r}"
            )
            return

        loop = asyncio.get_event_loop()
        await asyncio.sleep(2)   # let trade subscription warm up first

        while self.is_running:
            try:
                meta, asset_ctxs = await loop.run_in_executor(
                    None, self.info.meta_and_asset_ctxs
                )

                # Cache the coin index so we don't scan every poll
                if self._coin_index is None:
                    universe = meta.get("universe", [])
                    for i, asset in enumerate(universe):
                        if asset.get("name") == self.symbol:
                            self._coin_index = i
                            logger.info(
                                f"Hyperliquid OI: {self.symbol} at index {i}"
                            )
                            break
                    if self._coin_index is None:
                        logger.warning(
                            f"Hyperliquid: symbol {self.symbol!r} not found in universe "
                            f"({len(meta.get('universe', []))} assets)"
                        )

                if self._coin_index is not None and self._coin_index < len(asset_ctxs):
                    ctx = asset_ctxs[self._coin_index]
                    oi  = float(ctx.get("openInterest", 0.0))
                    if oi > 0 and self.on_oi_callback:
                        self.on_oi_callback(oi, time.time())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Hyperliquid OI poll error for {self.symbol}: {e}")
                self._coin_index = None   # reset so we re-discover on next poll

            await asyncio.sleep(5)

    async def stop(self):
        self.is_running = False
        # Stop raw WS for HIP-3 path
        if self._raw_ws is not None:
            try:
                self._raw_ws.close()
            except Exception as e:
                logger.warning(f"Error closing HIP-3 raw WS: {e}")
        # Stop SDK WS for standard perp path
        if self.info is not None:
            try:
                if hasattr(self.info, "ws_manager") and self.info.ws_manager:
                    self.info.ws_manager.stop()
                elif hasattr(self.info, "websocket_manager") and self.info.websocket_manager:
                    self.info.websocket_manager.stop()
            except Exception as e:
                logger.warning(f"Error stopping Hyperliquid WS manager: {e}")
        await super().stop()
