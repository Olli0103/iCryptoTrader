"""Tests for FIFO tax ledger — the most critical component for correctness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.types import LotStatus

if TYPE_CHECKING:
    from pathlib import Path

EUR_USD = Decimal("1.08")  # 1 EUR = 1.08 USD


def _ts(days_ago: int) -> datetime:
    """Helper: create a timestamp N days in the past."""
    return datetime.now(UTC) - timedelta(days=days_ago)


class TestAddLot:
    def test_single_lot(self) -> None:
        ledger = FIFOLedger()
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("2.13"),
            eur_usd_rate=EUR_USD,
            source_engine="grid",
            grid_level=1,
        )
        assert lot.quantity_btc == Decimal("0.01")
        assert lot.remaining_qty_btc == Decimal("0.01")
        assert lot.status == LotStatus.OPEN
        assert lot.source_engine == "grid"
        assert lot.grid_level == 1
        assert ledger.total_btc() == Decimal("0.01")

    def test_total_includes_fee(self) -> None:
        ledger = FIFOLedger()
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("2.13"),
            eur_usd_rate=EUR_USD,
        )
        expected_total = Decimal("0.01") * Decimal("85000") + Decimal("2.13")
        assert lot.purchase_total_usd == expected_total

    def test_eur_conversion(self) -> None:
        ledger = FIFOLedger()
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
        )
        expected_eur = Decimal("850") / Decimal("1.08")
        assert abs(lot.purchase_total_eur - expected_eur) < Decimal("0.01")

    def test_multiple_lots_ordered_by_time(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(10),
        )
        ledger.add_lot(
            quantity_btc=Decimal("0.02"), purchase_price_usd=Decimal("84000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(20),  # Older, should be first
        )
        assert ledger.lots[0].quantity_btc == Decimal("0.02")  # Older first
        assert ledger.lots[1].quantity_btc == Decimal("0.01")


class TestSellFIFO:
    def test_sell_single_lot_fully(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(30),
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert len(disposals) == 1
        assert disposals[0].quantity_btc == Decimal("0.01")
        assert ledger.total_btc() == Decimal("0")
        assert ledger.lots[0].status == LotStatus.CLOSED

    def test_sell_partial_lot(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.10"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.03"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert len(disposals) == 1
        assert disposals[0].quantity_btc == Decimal("0.03")
        assert ledger.lots[0].remaining_qty_btc == Decimal("0.07")
        assert ledger.lots[0].status == LotStatus.PARTIALLY_SOLD

    def test_fifo_order_oldest_first(self) -> None:
        """The oldest lot must be consumed first."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),  # OLD
        )
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(10),  # NEW
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        # Should consume the $80k lot (oldest), not the $90k lot
        assert disposals[0].lot_id == ledger.lots[0].lot_id
        assert ledger.lots[0].status == LotStatus.CLOSED
        assert ledger.lots[1].status == LotStatus.OPEN

    def test_sell_spans_multiple_lots(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.005"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        ledger.add_lot(
            quantity_btc=Decimal("0.005"), purchase_price_usd=Decimal("82000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(50),
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.008"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert len(disposals) == 2
        assert disposals[0].quantity_btc == Decimal("0.005")  # All of lot 1
        assert disposals[1].quantity_btc == Decimal("0.003")  # Partial lot 2
        assert ledger.lots[0].status == LotStatus.CLOSED
        assert ledger.lots[1].status == LotStatus.PARTIALLY_SOLD
        assert ledger.lots[1].remaining_qty_btc == Decimal("0.002")

    def test_sell_more_than_available_raises(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        with pytest.raises(ValueError, match="Cannot sell"):
            ledger.sell_fifo(
                quantity_btc=Decimal("0.02"), sale_price_usd=Decimal("85000"),
                sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            )

    def test_sell_from_empty_ledger_raises(self) -> None:
        ledger = FIFOLedger()
        with pytest.raises(ValueError, match="Cannot sell"):
            ledger.sell_fifo(
                quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
                sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            )


class TestGainLossCalculation:
    def test_profit_in_eur(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=Decimal("1.10"),
            purchase_timestamp=_ts(30),
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=Decimal("1.08"),
        )
        # Cost: 0.01 * 80000 / 1.10 = 727.27 EUR
        # Proceeds: 0.01 * 85000 / 1.08 = 787.04 EUR
        # Gain: ~59.77 EUR
        assert disposals[0].gain_loss_eur > Decimal("0")
        cost = Decimal("800") / Decimal("1.10")
        proceeds = Decimal("850") / Decimal("1.08")
        expected = proceeds - cost
        assert abs(disposals[0].gain_loss_eur - expected) < Decimal("0.01")

    def test_loss_in_eur(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(30),
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("80000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert disposals[0].gain_loss_eur < Decimal("0")

    def test_fee_reduces_proceeds(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(30),
        )
        d_no_fee = ledger.sell_fifo(
            quantity_btc=Decimal("0.005"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        d_with_fee = ledger.sell_fifo(
            quantity_btc=Decimal("0.005"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("5.00"), eur_usd_rate=EUR_USD,
        )
        assert d_with_fee[0].gain_loss_eur < d_no_fee[0].gain_loss_eur


class TestTaxFreeStatus:
    def test_lot_under_365_days_is_taxable(self) -> None:
        ledger = FIFOLedger()
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        assert not lot.is_tax_free
        assert ledger.tax_free_btc() == Decimal("0")
        assert ledger.locked_btc() == Decimal("0.01")

    def test_lot_over_365_days_is_tax_free(self) -> None:
        ledger = FIFOLedger()
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),
        )
        assert lot.is_tax_free
        assert ledger.tax_free_btc() == Decimal("0.01")
        assert ledger.locked_btc() == Decimal("0")

    def test_disposal_of_old_lot_not_taxable(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),  # > 365 days
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert not disposals[0].is_taxable

    def test_disposal_of_young_lot_is_taxable(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(30),  # < 365 days
        )
        disposals = ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert disposals[0].is_taxable

    def test_sellable_ratio(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),
        )
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(30),
        )
        assert ledger.sellable_ratio() == 0.5


class TestDaysUntilFree:
    def test_returns_none_when_all_free(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),
        )
        assert ledger.days_until_next_free() is None

    def test_returns_none_when_empty(self) -> None:
        ledger = FIFOLedger()
        assert ledger.days_until_next_free() is None

    def test_returns_days_for_locked_lot(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(350),
        )
        days = ledger.days_until_next_free()
        assert days is not None
        assert 14 <= days <= 16  # ~15 days left

    def test_near_threshold_btc(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(340),  # 340 days held, within 330-365
        )
        assert ledger.near_threshold_btc(near_days=330) == Decimal("0.01")


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("2.13"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100), source_engine="grid", grid_level=2,
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.005"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("1.13"), eur_usd_rate=EUR_USD,
        )

        filepath = tmp_path / "ledger.json"
        ledger.save(filepath)

        ledger2 = FIFOLedger()
        ledger2.load(filepath)

        assert len(ledger2.lots) == 1
        lot = ledger2.lots[0]
        assert lot.quantity_btc == Decimal("0.01")
        assert lot.remaining_qty_btc == Decimal("0.005")
        assert lot.status == LotStatus.PARTIALLY_SOLD
        assert lot.source_engine == "grid"
        assert lot.grid_level == 2
        assert len(lot.disposals) == 1
        assert lot.disposals[0].quantity_btc == Decimal("0.005")

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        ledger = FIFOLedger()
        ledger.load(tmp_path / "does_not_exist.json")
        assert len(ledger.lots) == 0


class TestSQLitePersistence:
    def test_sqlite_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("2.13"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100), source_engine="grid", grid_level=2,
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.005"), sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("1.13"), eur_usd_rate=EUR_USD,
        )

        filepath = tmp_path / "ledger.db"
        ledger.save_sqlite(filepath)
        assert filepath.exists()

        ledger2 = FIFOLedger()
        ledger2.load_sqlite(filepath)

        assert len(ledger2.lots) == 1
        lot = ledger2.lots[0]
        assert lot.quantity_btc == Decimal("0.01")
        assert lot.remaining_qty_btc == Decimal("0.005")
        assert lot.status == LotStatus.PARTIALLY_SOLD
        assert lot.source_engine == "grid"
        assert lot.grid_level == 2
        assert len(lot.disposals) == 1
        assert lot.disposals[0].quantity_btc == Decimal("0.005")

    def test_sqlite_load_nonexistent(self, tmp_path: Path) -> None:
        ledger = FIFOLedger()
        ledger.load_sqlite(tmp_path / "does_not_exist.db")
        assert len(ledger.lots) == 0

    def test_sqlite_overwrite(self, tmp_path: Path) -> None:
        """Saving twice should overwrite, not append."""
        filepath = tmp_path / "ledger.db"
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        ledger.save_sqlite(filepath)

        ledger2 = FIFOLedger()
        ledger2.add_lot(
            quantity_btc=Decimal("0.02"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        ledger2.save_sqlite(filepath)

        ledger3 = FIFOLedger()
        ledger3.load_sqlite(filepath)
        assert len(ledger3.lots) == 1
        assert ledger3.lots[0].quantity_btc == Decimal("0.02")

    def test_sqlite_empty_ledger(self, tmp_path: Path) -> None:
        filepath = tmp_path / "ledger.db"
        ledger = FIFOLedger()
        ledger.save_sqlite(filepath)

        ledger2 = FIFOLedger()
        ledger2.load_sqlite(filepath)
        assert len(ledger2.lots) == 0

    def test_sqlite_multiple_lots(self, tmp_path: Path) -> None:
        filepath = tmp_path / "ledger.db"
        ledger = FIFOLedger()
        for i in range(5):
            ledger.add_lot(
                quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
                purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
                purchase_timestamp=_ts(100 - i * 10),
            )
        ledger.save_sqlite(filepath)

        ledger2 = FIFOLedger()
        ledger2.load_sqlite(filepath)
        assert len(ledger2.lots) == 5
        for i in range(len(ledger2.lots) - 1):
            assert ledger2.lots[i].purchase_timestamp <= ledger2.lots[i + 1].purchase_timestamp


class TestUnderwaterLots:
    """Tests for underwater_lots() used in tax-loss harvesting."""

    def test_identifies_losing_positions(self) -> None:
        """Lots bought at higher price than current should appear."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert len(results) == 1
        lot, loss = results[0]
        assert lot.purchase_price_usd == Decimal("90000")
        assert loss < Decimal("0")

    def test_excludes_profitable_lots(self) -> None:
        """Lots bought at lower price than current should not appear."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("70000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert len(results) == 0

    def test_excludes_tax_free_lots(self) -> None:
        """Tax-free lots (>365 days) should not appear — no tax benefit from loss."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(400),  # >365 days, tax-free
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert len(results) == 0

    def test_excludes_near_threshold_lots(self) -> None:
        """Lots near maturity (>=330 days) should be protected."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(340),  # 340 days, near threshold
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
            near_threshold_days=330,
        )
        assert len(results) == 0

    def test_excludes_closed_lots(self) -> None:
        """Closed lots should not appear."""
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        # Sell it fully to close the lot
        ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("80000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert len(results) == 0

    def test_sorted_by_loss_magnitude(self) -> None:
        """Results should be sorted by loss, most negative first."""
        ledger = FIFOLedger()
        # Small loss lot
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("82000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(100),
        )
        # Large loss lot
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("95000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=_ts(50),
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert len(results) == 2
        # Most negative loss first (the $95k lot)
        assert results[0][1] < results[1][1]

    def test_mixed_lots_filters_correctly(self) -> None:
        """Mix of profitable, losing, tax-free, and near-threshold lots."""
        ledger = FIFOLedger()
        now = datetime.now(UTC)
        # Losing + eligible (100 days old, bought at $90k)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=100),
        )
        # Profitable (100 days old, bought at $70k)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("70000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=100),
        )
        # Losing but tax-free (400 days old)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=400),
        )
        # Losing but near threshold (340 days old)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=340),
        )
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        # Only the first lot (losing + eligible) should appear
        assert len(results) == 1
        assert results[0][0].purchase_price_usd == Decimal("90000")
        assert results[0][0].days_held < 330

    def test_empty_ledger(self) -> None:
        ledger = FIFOLedger()
        results = ledger.underwater_lots(
            current_price_usd=Decimal("80000"), eur_usd_rate=EUR_USD,
        )
        assert results == []


class TestTaxableGainYTD:
    def test_accumulates_taxable_gains(self) -> None:
        ledger = FIFOLedger()
        now = datetime.now(UTC)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=30),
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        gain = ledger.taxable_gain_ytd(now.year)
        assert gain > Decimal("0")

    def test_tax_free_disposals_excluded(self) -> None:
        ledger = FIFOLedger()
        now = datetime.now(UTC)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=400),  # Tax-free
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        gain = ledger.taxable_gain_ytd(now.year)
        assert gain == Decimal("0")
