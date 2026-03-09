"""Tests for the order book pattern analyzer (analysis.book_analyzer)."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.analysis.book_analyzer import BookAnalyzer


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, sec: float) -> None:
        self._t += sec


def _symmetric_book(
    mid: Decimal = Decimal("85000"),
    levels: int = 5,
    spacing: Decimal = Decimal("10"),
    qty: Decimal = Decimal("1.0"),
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    """Create a perfectly symmetric book around mid-price."""
    bids = [(mid - spacing * i, qty) for i in range(1, levels + 1)]
    asks = [(mid + spacing * i, qty) for i in range(1, levels + 1)]
    return bids, asks


def _asymmetric_book(
    mid: Decimal = Decimal("85000"),
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    """Book with heavy bid-side layering."""
    bids = [(mid - Decimal(str(i * 10)), Decimal("5.0")) for i in range(1, 8)]
    asks = [(mid + Decimal(str(i * 10)), Decimal("0.5")) for i in range(1, 4)]
    return bids, asks


# ---------------------------------------------------------------------------
# Symmetry detection
# ---------------------------------------------------------------------------


class TestSymmetryDetection:
    def test_detects_symmetric_book(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        for _ in range(20):
            bids, asks = _symmetric_book()
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("2"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        assert report.symmetry.detected
        assert report.symmetry.avg_symmetry_score > 0.9
        assert report.symmetry.confidence > 0.5

    def test_no_symmetry_in_asymmetric_book(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        for _ in range(20):
            bids, asks = _asymmetric_book()
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("5"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        assert report.symmetry.avg_symmetry_score < 0.5


# ---------------------------------------------------------------------------
# Spoofing detection
# ---------------------------------------------------------------------------


class TestSpoofingDetection:
    def test_detects_vanishing_large_orders(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        # Normal book
        normal_bids = [
            (Decimal("84990"), Decimal("0.5")),
            (Decimal("84980"), Decimal("0.5")),
        ]
        normal_asks = [
            (Decimal("85010"), Decimal("0.5")),
            (Decimal("85020"), Decimal("0.5")),
        ]

        # First: book with large bid order (spoof)
        spoof_bids = [
            (Decimal("84990"), Decimal("10.0")),  # 20x median = spoof
            (Decimal("84980"), Decimal("0.5")),
        ]

        # Record a few snapshots with the spoof order
        for _ in range(3):
            analyzer.record_snapshot(
                bids=spoof_bids, asks=normal_asks,
                mid_price=Decimal("85000"), spread_bps=Decimal("2"),
            )
            clock.advance(1.0)

        # Now it vanishes (less than 10s lifespan)
        for _ in range(5):
            analyzer.record_snapshot(
                bids=normal_bids, asks=normal_asks,
                mid_price=Decimal("85000"), spread_bps=Decimal("2"),
            )
            clock.advance(1.0)

        # Add more spoof-vanish cycles
        for _ in range(3):
            analyzer.record_snapshot(
                bids=spoof_bids, asks=normal_asks,
                mid_price=Decimal("85000"), spread_bps=Decimal("2"),
            )
            clock.advance(1.0)
        for _ in range(3):
            analyzer.record_snapshot(
                bids=normal_bids, asks=normal_asks,
                mid_price=Decimal("85000"), spread_bps=Decimal("2"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        assert report.spoofing.detected
        assert report.spoofing.vanished_orders >= 2


# ---------------------------------------------------------------------------
# Layering detection
# ---------------------------------------------------------------------------


class TestLayeringDetection:
    def test_detects_heavy_one_sided_depth(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        for _ in range(10):
            bids, asks = _asymmetric_book()
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("5"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        assert report.layering.detected
        assert report.layering.max_imbalance_ratio > 2.0


# ---------------------------------------------------------------------------
# Quote stuffing detection
# ---------------------------------------------------------------------------


class TestQuoteStuffingDetection:
    def test_detects_rapid_changes(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        # Rapidly changing top-of-book (every 50ms = 20/sec)
        for i in range(200):
            price_offset = Decimal(str(i % 5))
            bids = [(Decimal("84990") + price_offset, Decimal("0.5"))]
            asks = [(Decimal("85010") - price_offset, Decimal("0.5"))]
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("2"),
            )
            clock.advance(0.05)

        report = analyzer.analyze()
        assert report.quote_stuffing.detected
        assert report.quote_stuffing.changes_per_sec > 5

    def test_no_stuffing_with_stable_book(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        bids = [(Decimal("84990"), Decimal("1.0"))]
        asks = [(Decimal("85010"), Decimal("1.0"))]
        for _ in range(50):
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("2"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        assert not report.quote_stuffing.detected


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class TestBookReport:
    def test_summary_output(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        for _ in range(10):
            bids, asks = _symmetric_book()
            analyzer.record_snapshot(
                bids=bids, asks=asks,
                mid_price=Decimal("85000"),
                spread_bps=Decimal("3"),
            )
            clock.advance(1.0)

        report = analyzer.analyze()
        summary = report.summary()
        assert "Order Book Pattern Analysis" in summary
        assert "Snapshots:" in summary

    def test_empty_analysis(self) -> None:
        clock = _FakeClock()
        analyzer = BookAnalyzer(window_sec=600, clock=clock)

        report = analyzer.analyze()
        assert report.snapshots_analyzed == 0
        assert report.avg_spread_bps == 0.0
