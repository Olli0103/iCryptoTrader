"""Tests for L2 Order Book manager with CRC32 checksum validation."""

from decimal import Decimal

from icryptotrader.ws.book_manager import OrderBook, _format_decimal


def _make_snapshot(
    asks: list[tuple[str, str]],
    bids: list[tuple[str, str]],
    checksum: int | None = None,
) -> dict:
    """Build a snapshot data dict from ask/bid tuples."""
    data: dict = {
        "asks": [{"price": p, "qty": q} for p, q in asks],
        "bids": [{"price": p, "qty": q} for p, q in bids],
    }
    if checksum is not None:
        data["checksum"] = checksum
    return data


def _make_update(
    asks: list[tuple[str, str]] | None = None,
    bids: list[tuple[str, str]] | None = None,
    checksum: int | None = None,
) -> dict:
    data: dict = {
        "asks": [{"price": p, "qty": q} for p, q in (asks or [])],
        "bids": [{"price": p, "qty": q} for p, q in (bids or [])],
    }
    if checksum is not None:
        data["checksum"] = checksum
    return data


class TestFormatDecimal:
    def test_remove_decimal_point(self) -> None:
        assert _format_decimal("45285.2") == "452852"

    def test_strip_leading_zeros(self) -> None:
        assert _format_decimal("0.00100000") == "100000"

    def test_integer_value(self) -> None:
        assert _format_decimal("85000") == "85000"

    def test_zero(self) -> None:
        assert _format_decimal("0") == "0"

    def test_small_decimal(self) -> None:
        assert _format_decimal("0.00000001") == "1"


class TestSnapshot:
    def test_snapshot_populates_book(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5"), ("85200", "1.0")],
            bids=[("85000", "0.3"), ("84900", "0.7")],
        )
        result = book.apply_snapshot(data, checksum_enabled=False)
        assert result is True
        assert book.is_valid
        assert book.ask_count == 2
        assert book.bid_count == 2

    def test_snapshot_replaces_existing(self) -> None:
        book = OrderBook()
        data1 = _make_snapshot(
            asks=[("85100", "0.5")], bids=[("85000", "0.3")],
        )
        book.apply_snapshot(data1, checksum_enabled=False)
        assert book.ask_count == 1

        data2 = _make_snapshot(
            asks=[("85200", "1.0"), ("85300", "2.0"), ("85400", "3.0")],
            bids=[("84900", "0.7")],
        )
        book.apply_snapshot(data2, checksum_enabled=False)
        assert book.ask_count == 3
        assert book.bid_count == 1

    def test_snapshot_with_valid_checksum(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100.0", "0.50000000")],
            bids=[("85000.0", "0.30000000")],
        )
        # Compute expected checksum
        data["checksum"] = book.compute_checksum()  # Won't work — book empty
        # Instead, apply without checksum, compute, then re-apply with checksum
        book.apply_snapshot(data, checksum_enabled=False)
        expected_crc = book.compute_checksum()

        book2 = OrderBook()
        data["checksum"] = expected_crc
        result = book2.apply_snapshot(data, checksum_enabled=True)
        assert result is True
        assert book2.is_valid

    def test_snapshot_with_invalid_checksum(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5")],
            bids=[("85000", "0.3")],
            checksum=999999,  # Wrong
        )
        result = book.apply_snapshot(data, checksum_enabled=True)
        assert result is False
        assert not book.is_valid


class TestUpdate:
    def _setup_book(self) -> OrderBook:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5"), ("85200", "1.0")],
            bids=[("85000", "0.3"), ("84900", "0.7")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        return book

    def test_add_level(self) -> None:
        book = self._setup_book()
        update = _make_update(asks=[("85300", "2.0")])
        result = book.apply_update(update, checksum_enabled=False)
        assert result is True
        assert book.ask_count == 3

    def test_remove_level(self) -> None:
        book = self._setup_book()
        update = _make_update(bids=[("85000", "0")])  # Qty 0 = remove
        result = book.apply_update(update, checksum_enabled=False)
        assert result is True
        assert book.bid_count == 1

    def test_modify_qty(self) -> None:
        book = self._setup_book()
        update = _make_update(asks=[("85100", "0.8")])  # Update existing
        book.apply_update(update, checksum_enabled=False)
        assert book._asks[Decimal("85100")] == Decimal("0.8")

    def test_update_with_valid_checksum(self) -> None:
        book = self._setup_book()
        update = _make_update(asks=[("85300", "2.0")])
        book.apply_update(update, checksum_enabled=False)
        expected_crc = book.compute_checksum()

        # Reset and reapply with checksum
        book2 = self._setup_book()
        update_with_crc = _make_update(asks=[("85300", "2.0")], checksum=expected_crc)
        result = book2.apply_update(update_with_crc, checksum_enabled=True)
        assert result is True

    def test_update_with_invalid_checksum(self) -> None:
        book = self._setup_book()
        update = _make_update(asks=[("85300", "2.0")], checksum=999999)
        result = book.apply_update(update, checksum_enabled=True)
        assert result is False
        assert book.checksum_failures == 1
        # Book stays valid after single failure
        assert book.is_valid

    def test_consecutive_failures_invalidate_book(self) -> None:
        book = self._setup_book()
        for _ in range(3):
            update = _make_update(asks=[("85300", "2.0")], checksum=999999)
            book.apply_update(update, checksum_enabled=True)
        assert not book.is_valid
        assert book.checksum_failures == 3

    def test_update_ignored_when_invalid(self) -> None:
        book = OrderBook()  # Never initialized — invalid
        update = _make_update(asks=[("85100", "0.5")])
        result = book.apply_update(update, checksum_enabled=False)
        assert result is False


class TestDerivedMetrics:
    def test_mid_price(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5")],
            bids=[("85000", "0.3")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        assert book.mid_price == Decimal("85050")

    def test_mid_price_empty_book(self) -> None:
        book = OrderBook()
        assert book.mid_price == Decimal("0")

    def test_best_bid_ask(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5"), ("85200", "1.0")],
            bids=[("85000", "0.3"), ("84900", "0.7")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        assert book.best_ask == Decimal("85100")
        assert book.best_bid == Decimal("85000")

    def test_best_bid_ask_empty(self) -> None:
        book = OrderBook()
        assert book.best_ask is None
        assert book.best_bid is None

    def test_spread_bps(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5")],
            bids=[("85000", "0.3")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        spread = book.spread_bps
        # (85100 - 85000) / 85050 * 10000 ≈ 11.76 bps
        assert Decimal("11") < spread < Decimal("12")

    def test_obi_balanced(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "1.0")],
            bids=[("85000", "1.0")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        assert book.order_book_imbalance() == 0.0

    def test_obi_bid_heavy(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5")],
            bids=[("85000", "1.5")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        obi = book.order_book_imbalance()
        assert obi > 0  # Bid-heavy = positive
        assert abs(obi - 0.5) < 0.01  # (1.5 - 0.5) / (1.5 + 0.5) = 0.5

    def test_obi_ask_heavy(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "2.0")],
            bids=[("85000", "0.5")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        obi = book.order_book_imbalance()
        assert obi < 0  # Ask-heavy = negative

    def test_obi_empty_book(self) -> None:
        book = OrderBook()
        assert book.order_book_imbalance() == 0.0


class TestChecksum:
    def test_deterministic(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100.0", "0.50000000"), ("85200.0", "1.00000000")],
            bids=[("85000.0", "0.30000000"), ("84900.0", "0.70000000")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        crc1 = book.compute_checksum()
        crc2 = book.compute_checksum()
        assert crc1 == crc2
        assert isinstance(crc1, int)
        assert 0 <= crc1 <= 0xFFFFFFFF

    def test_different_books_different_checksums(self) -> None:
        book1 = OrderBook()
        data1 = _make_snapshot(
            asks=[("85100", "0.5")], bids=[("85000", "0.3")],
        )
        book1.apply_snapshot(data1, checksum_enabled=False)

        book2 = OrderBook()
        data2 = _make_snapshot(
            asks=[("85200", "0.5")], bids=[("85000", "0.3")],
        )
        book2.apply_snapshot(data2, checksum_enabled=False)

        assert book1.compute_checksum() != book2.compute_checksum()

    def test_top_10_only(self) -> None:
        """Checksum uses only top 10 levels per side."""
        asks = [(str(85100 + i * 100), "0.1") for i in range(15)]
        bids = [(str(85000 - i * 100), "0.1") for i in range(15)]

        book_full = OrderBook()
        book_full.apply_snapshot(
            _make_snapshot(asks, bids), checksum_enabled=False,
        )

        book_top10 = OrderBook()
        book_top10.apply_snapshot(
            _make_snapshot(asks[:10], bids[:10]), checksum_enabled=False,
        )

        assert book_full.compute_checksum() == book_top10.compute_checksum()


class TestResync:
    def test_resync_clears_book(self) -> None:
        book = OrderBook()
        data = _make_snapshot(
            asks=[("85100", "0.5")], bids=[("85000", "0.3")],
        )
        book.apply_snapshot(data, checksum_enabled=False)
        assert book.is_valid

        book.request_resync()
        assert not book.is_valid
        assert book.ask_count == 0
        assert book.bid_count == 0
