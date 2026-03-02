"""WS2 — Private WebSocket connection for Kraken Spot WS v2.

Connects to wss://ws-auth.kraken.com/v2 for:
  - Trading commands: add_order, amend_order, cancel_order, cancel_all, batch_add
  - Dead man's switch: cancel_after (heartbeat every 20s, 60s timeout)
  - Executions channel: order status, fills, trade history
  - Balances channel: account balance updates

Lives in the Strategy Process. Auth token obtained via REST GetWebSocketsToken
at startup and on each reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

import httpx
import websockets
import websockets.asyncio.client as ws_client

from icryptotrader.ws import ws_codec
from icryptotrader.ws.ws_codec import MessageType, WSMessage

logger = logging.getLogger(__name__)

_RECONNECT_SCHEDULE = [0.0, 0.0, 0.0, 5.0, 10.0, 20.0, 30.0]

ExecutionCallback = Callable[[WSMessage], None]
AckCallback = Callable[[WSMessage], None]


class WSPrivateError(Exception):
    """Raised on unrecoverable WS2 errors."""


class WSPrivate:
    """Manages the authenticated WS2 connection lifecycle.

    Handles:
    - Token acquisition via REST
    - Connection with auto-reconnect
    - cancel_after heartbeat (dead man's switch)
    - Execution event routing to Order Manager
    - Command sending with backpressure awareness

    Usage:
        ws2 = WSPrivate(
            rest_url="https://api.kraken.com",
            ws_url="wss://ws-auth.kraken.com/v2",
            api_key="...", api_secret="...",
        )
        ws2.on_execution(handle_execution)
        ws2.on_ack(handle_ack)
        await ws2.run()
    """

    def __init__(
        self,
        rest_url: str = "https://api.kraken.com",
        ws_url: str = "wss://ws-auth.kraken.com/v2",
        api_key: str = "",
        api_secret: str = "",
        cancel_after_sec: int = 60,
        heartbeat_interval_sec: int = 20,
    ) -> None:
        self._rest_url = rest_url
        self._ws_url = ws_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._cancel_after_sec = cancel_after_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec

        self._ws: ws_client.ClientConnection | None = None
        self._token: str = ""
        self._token_ts: float = 0.0  # monotonic time of last token fetch
        self._token_ttl_sec: float = 780.0  # 13 minutes (tokens valid for 15m)
        self._running = False
        self._connected = asyncio.Event()
        self._reconnect_count = 0
        self._req_id_counter = 1000  # Offset from WS1 to avoid collisions
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None

        # Callbacks
        self._execution_callbacks: list[ExecutionCallback] = []
        self._ack_callbacks: list[AckCallback] = []
        self._balance_callbacks: list[ExecutionCallback] = []

        # State flags
        self.is_connected: bool = False
        self.is_recovering: bool = False

        # Metrics
        self.msgs_received: int = 0
        self.msgs_sent: int = 0
        self.last_msg_ts: float = 0.0
        self.reconnects: int = 0

    def on_execution(self, callback: ExecutionCallback) -> None:
        """Register callback for executions channel messages (fills, status changes)."""
        self._execution_callbacks.append(callback)

    def on_ack(self, callback: AckCallback) -> None:
        """Register callback for command acks (add_order, amend_order, cancel_order responses)."""
        self._ack_callbacks.append(callback)

    def on_balance(self, callback: ExecutionCallback) -> None:
        """Register callback for balances channel updates."""
        self._balance_callbacks.append(callback)

    async def run(self) -> None:
        """Main loop: connect, subscribe, heartbeat, receive, reconnect."""
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)
        while self._running:
            try:
                await self._connect_and_run()
            except (TimeoutError, websockets.ConnectionClosed, websockets.InvalidURI, OSError) as e:
                self.is_connected = False
                self.is_recovering = True
                self._connected.clear()
                logger.warning("WS2 disconnected: %s", e)
                self.reconnects += 1
                self._reconnect_count += 1
                backoff = _RECONNECT_SCHEDULE[
                    min(self._reconnect_count, len(_RECONNECT_SCHEDULE) - 1)
                ]
                if backoff > 0:
                    logger.info(
                        "WS2 reconnecting in %.1fs (attempt %d)", backoff, self._reconnect_count
                    )
                    await asyncio.sleep(backoff)

    async def stop(self) -> None:
        """Graceful shutdown: disarm DMS and close."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            # Disarm the dead man's switch before disconnecting
            with contextlib.suppress(Exception):
                await self.send_cancel_after(0)
            await self._ws.close()
            self._ws = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self.is_connected = False
        self._connected.clear()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait until WS2 is connected and subscribed."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # --- Command sending ---

    async def send(self, frame: bytes) -> bool:
        """Send a raw frame. Returns False if not connected."""
        if not self._ws or not self.is_connected:
            logger.warning("WS2 send attempted while disconnected")
            return False
        await self._ws.send(frame)
        self.msgs_sent += 1
        return True

    def next_req_id(self) -> int:
        self._req_id_counter += 1
        return self._req_id_counter

    async def send_add_order(
        self,
        order_type: str,
        side: str,
        pair: str,
        *,
        price: str | None = None,
        quantity: str | None = None,
        cl_ord_id: str | None = None,
        post_only: bool = False,
        req_id: int | None = None,
    ) -> int | None:
        """Send add_order command. Returns req_id, or None if send failed."""
        if req_id is None:
            req_id = self.next_req_id()
        frame = ws_codec.encode_add_order(
            order_type, side, pair,
            price=price, quantity=quantity, cl_ord_id=cl_ord_id,
            post_only=post_only, req_id=req_id,
        )
        if not await self.send(frame):
            return None
        return req_id

    async def send_amend_order(
        self,
        order_id: str,
        *,
        new_price: str | None = None,
        new_qty: str | None = None,
    ) -> int | None:
        """Send amend_order command (atomic). Returns req_id, or None if send failed."""
        req_id = self.next_req_id()
        frame = ws_codec.encode_amend_order(
            order_id, new_price=new_price, new_qty=new_qty, req_id=req_id,
        )
        if not await self.send(frame):
            return None
        return req_id

    async def send_cancel_order(self, order_id: str | list[str]) -> int | None:
        """Send cancel_order command. Returns req_id, or None if send failed."""
        req_id = self.next_req_id()
        frame = ws_codec.encode_cancel_order(order_id, req_id=req_id)
        if not await self.send(frame):
            return None
        return req_id

    async def send_cancel_all(self) -> int | None:
        """Send cancel_all command. Returns req_id, or None if send failed."""
        req_id = self.next_req_id()
        frame = ws_codec.encode_cancel_all(req_id=req_id)
        if not await self.send(frame):
            return None
        return req_id

    async def send_cancel_after(self, timeout_sec: int) -> int | None:
        """Send cancel_after (dead man's switch). timeout=0 disarms.

        Returns req_id, or None if send failed.
        """
        req_id = self.next_req_id()
        frame = ws_codec.encode_cancel_after(timeout_sec, req_id=req_id)
        if not await self.send(frame):
            return None
        return req_id

    # --- Internal connection lifecycle ---

    async def _connect_and_run(self) -> None:
        """Single connection lifecycle: token → connect → subscribe → receive."""
        # Reuse cached token if still valid (tokens last 15 minutes).
        # During WS disconnect storms (e.g., Kraken maintenance dropping TCP
        # after handshake), hitting the REST endpoint on every reconnect
        # attempt burns through REST API rate limits.
        now = time.monotonic()
        if not self._token or (now - self._token_ts) >= self._token_ttl_sec:
            self._token = await self._get_ws_token()
            self._token_ts = now
            logger.info("WS2 obtained fresh auth token")
        else:
            logger.info("WS2 reusing cached auth token (age=%.0fs)", now - self._token_ts)

        logger.info("WS2 connecting to %s", self._ws_url)
        async with ws_client.connect(
            self._ws_url,
            max_size=2**22,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self._reconnect_count = 0
            logger.info("WS2 connected")

            # Subscribe to executions with snapshots for reconciliation
            await self._subscribe_executions()

            self.is_connected = True
            self.is_recovering = False
            self._connected.set()

            # Start dead man's switch heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                # Receive loop
                async for raw_msg in ws:
                    self.msgs_received += 1
                    self.last_msg_ts = time.monotonic()
                    msg = ws_codec.decode(raw_msg)
                    self._dispatch(msg)
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._heartbeat_task

        self._ws = None

    async def _subscribe_executions(self) -> None:
        """Subscribe to executions and balances channels."""
        if not self._ws:
            return
        req_id = self.next_req_id()
        frame = ws_codec.encode_subscribe(
            "executions",
            params={
                "token": self._token,
                "snap_orders": True,
                "snap_trades": True,
            },
            req_id=req_id,
        )
        await self._ws.send(frame)
        logger.info("WS2 executions subscription sent (snap_orders=true, snap_trades=true)")

        # Subscribe to balances channel for real-time balance updates
        bal_req_id = self.next_req_id()
        bal_frame = ws_codec.encode_subscribe(
            "balances",
            params={"token": self._token, "snap_balances": True},
            req_id=bal_req_id,
        )
        await self._ws.send(bal_frame)
        logger.info("WS2 balances subscription sent")

    async def _heartbeat_loop(self) -> None:
        """Periodically re-arm the dead man's switch."""
        while self._running and self.is_connected:
            try:
                await self.send_cancel_after(self._cancel_after_sec)
                logger.debug("WS2 cancel_after(%d) heartbeat sent", self._cancel_after_sec)
            except Exception:
                logger.exception("WS2 heartbeat send failed")
                break
            await asyncio.sleep(self._heartbeat_interval_sec)

    async def _get_ws_token(self) -> str:
        """Obtain a WebSocket auth token via REST GetWebSocketsToken.

        Retries transient network errors up to 3 times with exponential
        backoff.  Auth errors (4xx) are raised immediately.
        """
        if not self._api_key:
            # For testing without real credentials
            logger.warning("WS2 no API key configured, using empty token")
            return ""

        import base64
        import hashlib
        import hmac
        import urllib.parse

        max_attempts = 4
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            nonce = str(int(time.time() * 1000))
            data = {"nonce": nonce}
            url_path = "/0/private/GetWebSocketsToken"

            post_data = urllib.parse.urlencode(data)
            encoded = (nonce + post_data).encode()
            message = url_path.encode() + hashlib.sha256(encoded).digest()
            signature = hmac.new(
                base64.b64decode(self._api_secret), message, hashlib.sha512
            )
            sig_b64 = base64.b64encode(signature.digest()).decode()

            try:
                client = self._http_client or httpx.AsyncClient(timeout=10.0)
                resp = await client.post(
                    f"{self._rest_url}{url_path}",
                    data=data,
                    headers={
                        "API-Key": self._api_key,
                        "API-Sign": sig_b64,
                    },
                )
                # Auth errors (4xx) should not be retried
                if 400 <= resp.status_code < 500:
                    resp.raise_for_status()
                resp.raise_for_status()
                result = resp.json()

                if result.get("error"):
                    raise WSPrivateError(
                        f"GetWebSocketsToken failed: {result['error']}"
                    )

                return str(result["result"]["token"])

            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise  # Auth error — don't retry
                last_exc = e
            except (httpx.TransportError, OSError, TimeoutError) as e:
                last_exc = e

            backoff = 2 ** attempt
            logger.warning(
                "WS2 token request failed (attempt %d/%d): %s — retrying in %ds",
                attempt + 1, max_attempts, last_exc, backoff,
            )
            await asyncio.sleep(backoff)

        raise WSPrivateError(
            f"GetWebSocketsToken failed after {max_attempts} attempts: {last_exc}"
        )

    def _dispatch(self, msg: WSMessage) -> None:
        """Route messages to appropriate callbacks."""
        if msg.msg_type == MessageType.CHANNEL_DATA:
            if msg.channel == "executions":
                for cb in self._execution_callbacks:
                    try:
                        cb(msg)
                    except Exception:
                        logger.exception("WS2 execution callback error")
            elif msg.channel == "balances":
                for cb in self._balance_callbacks:
                    try:
                        cb(msg)
                    except Exception:
                        logger.exception("WS2 balance callback error")

        elif msg.msg_type in (
            MessageType.ADD_ORDER_RESP,
            MessageType.AMEND_ORDER_RESP,
            MessageType.CANCEL_ORDER_RESP,
            MessageType.CANCEL_ALL_RESP,
            MessageType.BATCH_ADD_RESP,
        ):
            for cb in self._ack_callbacks:
                try:
                    cb(msg)
                except Exception:
                    logger.exception("WS2 ack callback error for %s", msg.method)

        elif msg.msg_type == MessageType.SUBSCRIBE_RESP:
            if msg.success:
                logger.info("WS2 subscribed: %s", msg.result.get("channel", ""))
            else:
                logger.error("WS2 subscribe failed: %s", msg.error)

        elif msg.msg_type == MessageType.CANCEL_AFTER_RESP:
            if msg.success:
                logger.debug("WS2 cancel_after confirmed")
            else:
                logger.error("WS2 cancel_after failed: %s", msg.error)

        elif msg.msg_type == MessageType.HEARTBEAT:
            pass

        elif msg.msg_type == MessageType.ERROR:
            logger.error("WS2 error: %s", msg.error)
