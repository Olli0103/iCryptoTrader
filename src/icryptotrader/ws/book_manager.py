"""L2 Order Book manager with CRC32 checksum validation.

Maintains a local mirror of the Kraken WS v2 order book and validates
each update against the exchange-provided CRC32 checksum.

Checksum algorithm (Kraken v2):
  1. Top 10 asks (ascending price) + top 10 bids (descending price)
  2. For each level: strip '.' from price, remove leading zeros;
     strip '.' from qty, remove leading zeros
  3. Concatenate all price+qty strings
  4. Compute CRC32 (unsigned 32-bit)
"""

from __future__ import annotations

import logging
import time
import zlib
from collections.abc import Callable
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CHECKSUM_FAILURES = 3


def _format_decimal(value: str) -> str:
    """Format a decimal string for CRC32: remove '.', strip leading zeros.

    Handles scientific notation (e.g., '1E+5', '1E-7') which Python's
    ``Decimal.__str__()`` can produce after arithmetic or for very
    large/small values.  The Kraken CRC32 spec requires plain decimal
    digit strings, so we normalize through ``Decimal`` formatting first.
    """
    if "E" in value or "e" in value:
        # Scientific notation → convert to fixed-point string
        value = format(Decimal(value), "f")
    return value.replace(".", "").lstrip("0") or "0"


class OrderBook:
    """L2 order book with CRC32 checksum validation.

    Tracks bid and ask levels as {price: qty} dicts. Validates every
    snapshot and update against the Kraken-provided checksum.

    On checksum failure (after _MAX_CHECKSUM_FAILURES consecutive),
    the book is marked invalid and the caller should re-subscribe.
    """

    def __init__(self, symbol: str = "XBT/USD", depth: int = 10) -> None:
        self._symbol = symbol
        self._depth = depth
        self._asks: dict[Decimal, Decimal] = {}
        self._bids: dict[Decimal, Decimal] = {}
        self._is_valid: bool = False
        self._sequence: int = 0
        self._consecutive_checksum_failures: int = 0
        self.checksum_failures: int = 0
        self.updates_applied: int = 0
        self.resync_count: int = 0
        self._last_update_ts: float = 0.0  # monotonic time of last data

        # Auto-recovery callback: invoked when the book becomes invalid
        # due to consecutive CRC32 checksum failures. The caller should
        # wire this to drop the WS connection and trigger a full REST
        # snapshot re-sync (e.g., re-subscribe to the book channel).
        self._on_invalid_callbacks: list[Callable[[str], None]] = []

    def on_invalid(self, callback: Callable[[str], None]) -> None:
        """Register a callback for when the book becomes invalid.

        The callback receives the symbol name as its argument.
        Typical usage: trigger WS reconnect and request fresh snapshot.
        """
        self._on_invalid_callbacks.append(callback)

    @property
    def is_valid(self) -> bool:
        return self._is_valid

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def last_update_ts(self) -> float:
        """Monotonic timestamp of the last snapshot/update applied."""
        return self._last_update_ts

    def apply_snapshot(self, data: dict[str, Any], checksum_enabled: bool = True) -> bool:
        """Initialize book from a WS snapshot message.

        Args:
            data: The book snapshot data dict (from WSMessage.data[0]).
            checksum_enabled: Whether to validate the checksum.

        Returns True if snapshot applied successfully (checksum passed).
        """
        self._asks.clear()
        self._bids.clear()

        for ask in data.get("asks", []):
            price = Decimal(str(ask["price"]))
            qty = Decimal(str(ask["qty"]))
            if qty > 0:
                self._asks[price] = qty

        for bid in data.get("bids", []):
            price = Decimal(str(bid["price"]))
            qty = Decimal(str(bid["qty"]))
            if qty > 0:
                self._bids[price] = qty

        if checksum_enabled and "checksum" in data:
            expected = data["checksum"]
            if not self._validate_checksum(expected):
                self._is_valid = False
                return False

        self._is_valid = True
        self._consecutive_checksum_failures = 0
        self._sequence = data.get("sequence", 0)
        self.updates_applied += 1
        self._last_update_ts = time.monotonic()
        logger.info(
            "Book snapshot applied: %d asks, %d bids",
            len(self._asks), len(self._bids),
        )
        return True

    def apply_update(self, data: dict[str, Any], checksum_enabled: bool = True) -> bool:
        """Apply an incremental book update.

        Qty of 0 means remove the level. Returns False if checksum fails.
        """
        if not self._is_valid:
            logger.warning("Book update ignored — book is invalid, awaiting resync")
            return False

        for ask in data.get("asks", []):
            price = Decimal(str(ask["price"]))
            qty = Decimal(str(ask["qty"]))
            if qty == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = qty

        for bid in data.get("bids", []):
            price = Decimal(str(bid["price"]))
            qty = Decimal(str(bid["qty"]))
            if qty == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = qty

        if checksum_enabled and "checksum" in data:
            expected = data["checksum"]
            if not self._validate_checksum(expected):
                self._consecutive_checksum_failures += 1
                if self._consecutive_checksum_failures >= _MAX_CHECKSUM_FAILURES:
                    logger.error(
                        "Book checksum failed %d times consecutively — "
                        "clearing book and triggering auto-resync",
                        self._consecutive_checksum_failures,
                    )
                    self.request_resync()
                    self._notify_invalid()
                return False
            self._consecutive_checksum_failures = 0

        self._sequence = data.get("sequence", self._sequence)
        self.updates_applied += 1
        self._last_update_ts = time.monotonic()
        return True

    def compute_checksum(self) -> int:
        """Compute CRC32 checksum of top-10 asks + bids per Kraken spec.

        Algorithm:
          1. Top 10 asks sorted ascending by price
          2. Top 10 bids sorted descending by price
          3. For each: format price (remove '.', strip leading zeros) +
             format qty (remove '.', strip leading zeros)
          4. Concatenate all, compute CRC32 as unsigned 32-bit int
        """
        parts: list[str] = []

        # Top 10 asks (ascending)
        sorted_asks = sorted(self._asks.items(), key=lambda x: x[0])[:10]
        for price, qty in sorted_asks:
            parts.append(_format_decimal(str(price)))
            parts.append(_format_decimal(str(qty)))

        # Top 10 bids (descending)
        sorted_bids = sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[:10]
        for price, qty in sorted_bids:
            parts.append(_format_decimal(str(price)))
            parts.append(_format_decimal(str(qty)))

        checksum_str = "".join(parts)
        return zlib.crc32(checksum_str.encode()) & 0xFFFFFFFF

    @property
    def mid_price(self) -> Decimal:
        """Best bid/ask midpoint. Returns 0 if book is empty."""
        if not self._asks or not self._bids:
            return Decimal("0")
        best_ask = min(self._asks)
        best_bid = max(self._bids)
        return (best_ask + best_bid) / 2

    @property
    def best_ask(self) -> Decimal | None:
        return min(self._asks) if self._asks else None

    @property
    def best_bid(self) -> Decimal | None:
        return max(self._bids) if self._bids else None

    @property
    def spread_bps(self) -> Decimal:
        """Spread in basis points. Returns 0 if book is empty."""
        if not self._asks or not self._bids:
            return Decimal("0")
        best_ask = min(self._asks)
        best_bid = max(self._bids)
        mid = (best_ask + best_bid) / 2
        if mid == 0:
            return Decimal("0")
        return (best_ask - best_bid) / mid * 10000

    def order_book_imbalance(self, levels: int = 5) -> float:
        """OBI = (bid_qty - ask_qty) / (bid_qty + ask_qty).

        Range: [-1, 1]. Positive = more bid pressure, negative = more ask pressure.
        """
        sorted_asks = sorted(self._asks.items(), key=lambda x: x[0])[:levels]
        sorted_bids = sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[:levels]

        ask_qty = sum(qty for _, qty in sorted_asks)
        bid_qty = sum(qty for _, qty in sorted_bids)
        total = ask_qty + bid_qty
        if total == 0:
            return 0.0
        return float((bid_qty - ask_qty) / total)

    @property
    def ask_count(self) -> int:
        return len(self._asks)

    @property
    def bid_count(self) -> int:
        return len(self._bids)

    def request_resync(self) -> None:
        """Mark book as invalid. Caller should re-subscribe for a fresh snapshot."""
        self._is_valid = False
        self._asks.clear()
        self._bids.clear()
        self.resync_count += 1
        logger.warning("Book resync requested for %s (resync #%d)", self._symbol, self.resync_count)

    def _notify_invalid(self) -> None:
        """Invoke all registered on_invalid callbacks."""
        for cb in self._on_invalid_callbacks:
            try:
                cb(self._symbol)
            except Exception:
                logger.exception("on_invalid callback error for %s", self._symbol)

    def _validate_checksum(self, expected: int) -> bool:
        """Validate our computed checksum against Kraken's."""
        computed = self.compute_checksum()
        if computed != expected:
            self.checksum_failures += 1
            logger.warning(
                "Book checksum mismatch: computed=%d expected=%d (failure #%d)",
                computed, expected, self.checksum_failures,
            )
            return False
        return True
