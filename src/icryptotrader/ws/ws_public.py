"""WS1 — Public WebSocket feed manager for Kraken Spot WS v2.

Connects to wss://ws.kraken.com/v2 for market data:
  - book (L2 order book snapshots + updates with checksum)
  - trade (public trades)
  - ticker (24h stats)
  - ohlc (candlestick data)
  - instrument (pair metadata)

This runs in the Feed Process. Data is forwarded to the Strategy Process
via ZMQ PUB socket.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
import websockets.asyncio.client as ws_client

from icryptotrader.ws import ws_codec
from icryptotrader.ws.ws_codec import MessageType, WSMessage

logger = logging.getLogger(__name__)

# Reconnection backoff: 3 instant retries, then exponential up to 30s
_RECONNECT_SCHEDULE = [0.0, 0.0, 0.0, 5.0, 10.0, 20.0, 30.0]


@dataclass
class Subscription:
    """A pending or active channel subscription."""

    channel: str
    params: dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False


ChannelCallback = Callable[[WSMessage], None]


class WSPublicFeed:
    """Manages the public WS1 connection lifecycle.

    Usage:
        feed = WSPublicFeed(url="wss://ws.kraken.com/v2")
        feed.on_channel("book", handle_book_update)
        feed.on_channel("trade", handle_trade)
        await feed.run()  # blocks, reconnects automatically
    """

    def __init__(self, url: str = "wss://ws.kraken.com/v2") -> None:
        self._url = url
        self._ws: ws_client.ClientConnection | None = None
        self._subscriptions: list[Subscription] = []
        self._callbacks: dict[str, list[ChannelCallback]] = {}
        self._req_id_counter = 0
        self._running = False
        self._reconnect_count = 0

        # Metrics
        self.msgs_received: int = 0
        self.last_msg_ts: float = 0.0
        self.reconnects: int = 0

    def subscribe(self, channel: str, **params: Any) -> None:
        """Register a subscription. Will be sent on (re)connect."""
        self._subscriptions.append(Subscription(channel=channel, params=params))

    def on_channel(self, channel: str, callback: ChannelCallback) -> None:
        """Register a callback for channel data messages."""
        self._callbacks.setdefault(channel, []).append(callback)

    async def run(self) -> None:
        """Main loop: connect, subscribe, receive, reconnect on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_run()
            except (TimeoutError, websockets.ConnectionClosed, websockets.InvalidURI, OSError) as e:
                logger.warning("WS1 disconnected: %s", e)
                self.reconnects += 1
                self._reconnect_count += 1
                base_backoff = _RECONNECT_SCHEDULE[
                    min(self._reconnect_count, len(_RECONNECT_SCHEDULE) - 1)
                ]
                if base_backoff > 0:
                    # Full jitter: randomize [0, base_backoff] to prevent
                    # thundering herd when all clients reconnect at once
                    # after a brief exchange outage.
                    jittered = random.uniform(0, base_backoff)  # noqa: S311
                    logger.info(
                        "WS1 reconnecting in %.1fs (attempt %d, base=%.1fs)",
                        jittered, self._reconnect_count, base_backoff,
                    )
                    await asyncio.sleep(jittered)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _connect_and_run(self) -> None:
        """Single connection lifecycle."""
        logger.info("WS1 connecting to %s", self._url)
        async with ws_client.connect(
            self._url,
            max_size=2**22,  # 4MB max message
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self._reconnect_count = 0  # Reset on successful connect
            logger.info("WS1 connected")

            # (Re)subscribe to all channels
            for sub in self._subscriptions:
                sub.confirmed = False
                await self._send_subscribe(sub)

            # Receive loop
            async for raw_msg in ws:
                self.msgs_received += 1
                self.last_msg_ts = time.monotonic()
                msg = ws_codec.decode(raw_msg)
                self._dispatch(msg)

        self._ws = None

    async def _send_subscribe(self, sub: Subscription) -> None:
        """Send a subscribe request for a single channel."""
        if not self._ws:
            return
        self._req_id_counter += 1
        frame = ws_codec.encode_subscribe(
            sub.channel, params=sub.params, req_id=self._req_id_counter,
        )
        await self._ws.send(frame)
        logger.info("WS1 subscribe sent: %s %s", sub.channel, sub.params)

    def _dispatch(self, msg: WSMessage) -> None:
        """Route a parsed message to registered callbacks."""
        if msg.msg_type == MessageType.CHANNEL_DATA:
            for cb in self._callbacks.get(msg.channel, []):
                try:
                    cb(msg)
                except Exception:
                    logger.exception("WS1 callback error for channel %s", msg.channel)

        elif msg.msg_type == MessageType.SUBSCRIBE_RESP:
            if msg.success:
                channel = msg.result.get("channel", "")
                for sub in self._subscriptions:
                    if sub.channel == channel and not sub.confirmed:
                        sub.confirmed = True
                        break
                logger.info("WS1 subscribed: %s", channel)
            else:
                logger.error("WS1 subscribe failed: %s", msg.error)

        elif msg.msg_type == MessageType.HEARTBEAT:
            pass  # Expected, no action needed

        elif msg.msg_type == MessageType.STATUS:
            logger.info("WS1 status: %s", msg.data)

        elif msg.msg_type == MessageType.ERROR:
            logger.error("WS1 error: %s", msg.error)
