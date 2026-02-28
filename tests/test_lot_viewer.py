"""Tests for lot age visualization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.lot_viewer import (
    format_age_histogram,
    format_full_report,
    format_lot_table,
    format_summary,
    format_unlock_schedule,
)

EUR_USD = Decimal("1.08")


def _ts(days_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


def _make_diverse_ledger() -> FIFOLedger:
    """Create a ledger with lots at various ages for visualization tests."""
    ledger = FIFOLedger()
    # Young lot (10 days)
    ledger.add_lot(
        quantity_btc=Decimal("0.005"), purchase_price_usd=Decimal("85000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=_ts(10),
    )
    # Medium lot (200 days)
    ledger.add_lot(
        quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=_ts(200),
    )
    # Near-threshold lot (340 days)
    ledger.add_lot(
        quantity_btc=Decimal("0.008"), purchase_price_usd=Decimal("75000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=_ts(340),
    )
    # Tax-free lot (400 days)
    ledger.add_lot(
        quantity_btc=Decimal("0.02"), purchase_price_usd=Decimal("70000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=_ts(400),
    )
    return ledger


class TestFormatLotTable:
    def test_shows_all_lots(self) -> None:
        ledger = _make_diverse_ledger()
        table = format_lot_table(ledger)
        assert "Lot ID" in table
        assert "Age" in table
        assert "Qty BTC" in table
        # Should have 4 lots
        lines = [ln for ln in table.strip().split("\n") if ln and not ln.startswith("-")]
        assert len(lines) >= 5  # header + 4 data rows

    def test_tax_free_label(self) -> None:
        ledger = _make_diverse_ledger()
        table = format_lot_table(ledger)
        assert "TAX-FREE" in table

    def test_empty_ledger(self) -> None:
        ledger = FIFOLedger()
        table = format_lot_table(ledger)
        assert "No open lots" in table

    def test_partial_lot_shows_remaining(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.10"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.03"), sale_price_usd=Decimal("86000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        table = format_lot_table(ledger)
        assert "PARTIAL" in table
        assert "0.07000000" in table


class TestFormatAgeHistogram:
    def test_produces_histogram(self) -> None:
        ledger = _make_diverse_ledger()
        hist = format_age_histogram(ledger)
        assert "Lot Age Distribution" in hist
        assert "#" in hist  # Should have bars

    def test_all_buckets_present(self) -> None:
        ledger = _make_diverse_ledger()
        hist = format_age_histogram(ledger)
        assert "0-30d" in hist
        assert ">365d" in hist

    def test_empty_ledger(self) -> None:
        ledger = FIFOLedger()
        hist = format_age_histogram(ledger)
        assert "No open lots" in hist


class TestFormatUnlockSchedule:
    def test_shows_locked_lots(self) -> None:
        ledger = _make_diverse_ledger()
        schedule = format_unlock_schedule(ledger)
        assert "Unlock Schedule" in schedule
        assert "Days Left" in schedule

    def test_sorted_by_date(self) -> None:
        ledger = _make_diverse_ledger()
        schedule = format_unlock_schedule(ledger)
        # Near-threshold lot should appear first (fewer days left)
        lines = [ln for ln in schedule.strip().split("\n") if "d" in ln and "-" not in ln[:5]]
        # Filter to data lines with day counts
        day_values = []
        for line in lines:
            parts = line.split()
            for part in parts:
                if part.endswith("d") and part[:-1].isdigit():
                    day_values.append(int(part[:-1]))
                    break
        if len(day_values) >= 2:
            assert day_values == sorted(day_values)

    def test_all_free_message(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),
        )
        schedule = format_unlock_schedule(ledger)
        assert "already tax-free" in schedule


class TestFormatSummary:
    def test_shows_key_metrics(self) -> None:
        ledger = _make_diverse_ledger()
        summary = format_summary(ledger)
        assert "Open lots" in summary
        assert "Total BTC" in summary
        assert "Tax-free BTC" in summary
        assert "Locked BTC" in summary
        assert "Near-threshold" in summary
        assert "Next unlock" in summary
        assert "YTD taxable" in summary

    def test_percentage_shown(self) -> None:
        ledger = _make_diverse_ledger()
        summary = format_summary(ledger)
        assert "%" in summary

    def test_empty_ledger(self) -> None:
        ledger = FIFOLedger()
        summary = format_summary(ledger)
        assert "0.00000000" in summary
        assert "N/A" in summary


class TestFormatFullReport:
    def test_combines_all_views(self) -> None:
        ledger = _make_diverse_ledger()
        report = format_full_report(ledger)
        assert "Portfolio Tax Summary" in report
        assert "Lot Age Distribution" in report
        assert "Lot ID" in report
        assert "Unlock Schedule" in report

    def test_full_report_with_empty_ledger(self) -> None:
        ledger = FIFOLedger()
        report = format_full_report(ledger)
        assert "No open lots" in report
