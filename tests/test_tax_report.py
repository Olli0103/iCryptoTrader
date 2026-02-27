"""Tests for the Tax Report Generator."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path  # noqa: TC003

from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_report import TaxReportGenerator


def _make_ledger_with_trade(
    buy_price: Decimal = Decimal("80000"),
    sell_price: Decimal = Decimal("85000"),
    qty: Decimal = Decimal("0.01"),
    buy_days_ago: int = 100,
    sell_days_ago: int = 10,
) -> FIFOLedger:
    """Create a ledger with one buy and one sell."""
    ledger = FIFOLedger()
    now = datetime.now(UTC)
    ledger.add_lot(
        quantity_btc=qty,
        purchase_price_usd=buy_price,
        purchase_fee_usd=Decimal("1.00"),
        eur_usd_rate=Decimal("1.08"),
        purchase_timestamp=now - timedelta(days=buy_days_ago),
        exchange_order_id="BUY-001",
        exchange_trade_id="TRADE-001",
        source_engine="grid",
    )
    ledger.sell_fifo(
        quantity_btc=qty,
        sale_price_usd=sell_price,
        sale_fee_usd=Decimal("1.00"),
        eur_usd_rate=Decimal("1.08"),
        sale_timestamp=now - timedelta(days=sell_days_ago),
        exchange_order_id="SELL-001",
        exchange_trade_id="TRADE-002",
    )
    return ledger


class TestAnnualSummary:
    def test_basic_summary(self) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        summary = gen.annual_summary(year)
        assert summary.year == year
        assert summary.total_disposals == 1
        assert summary.taxable_disposals == 1  # < 365 days
        assert summary.tax_free_disposals == 0

    def test_tax_free_disposal(self) -> None:
        ledger = _make_ledger_with_trade(buy_days_ago=400, sell_days_ago=5)
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        summary = gen.annual_summary(year)
        assert summary.tax_free_disposals == 1
        assert summary.taxable_disposals == 0

    def test_freigrenze_check(self) -> None:
        # Small gain within Freigrenze
        ledger = _make_ledger_with_trade(
            buy_price=Decimal("84900"), sell_price=Decimal("85000"),
            qty=Decimal("0.01"),
        )
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        summary = gen.annual_summary(year)
        assert summary.within_freigrenze is True

    def test_exceeds_freigrenze(self) -> None:
        # Large gain exceeds Freigrenze
        ledger = _make_ledger_with_trade(
            buy_price=Decimal("50000"), sell_price=Decimal("85000"),
            qty=Decimal("1.0"),
        )
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        summary = gen.annual_summary(year)
        assert summary.within_freigrenze is False

    def test_empty_year(self) -> None:
        ledger = FIFOLedger()
        gen = TaxReportGenerator(ledger)
        summary = gen.annual_summary(2020)
        assert summary.total_disposals == 0
        assert summary.net_taxable_eur == Decimal("0")
        assert summary.within_freigrenze is True


class TestDisposalRows:
    def test_row_fields(self) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        rows = gen.disposal_rows(year)
        assert len(rows) == 1
        row = rows[0]
        assert row["Art des Wirtschaftsguts"] == "Bitcoin (BTC)"
        assert "Datum der Anschaffung" in row
        assert "Datum der Veräußerung" in row
        assert "Veräußerungspreis (EUR)" in row
        assert "Anschaffungskosten (EUR)" in row
        assert "Werbungskosten (EUR)" in row
        assert "Gewinn/Verlust (EUR)" in row
        assert row["Steuerpflichtig"] == "Ja"
        assert row["Methode"] == "FIFO (§23 Abs. 1 Satz 1 Nr. 2 EStG)"

    def test_tax_free_row(self) -> None:
        ledger = _make_ledger_with_trade(buy_days_ago=400, sell_days_ago=5)
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        rows = gen.disposal_rows(year)
        assert rows[0]["Haltefrist überschritten"] == "Ja"
        assert rows[0]["Steuerpflichtig"] == "Nein"

    def test_exchange_ids_in_row(self) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        rows = gen.disposal_rows(year)
        assert rows[0]["Order-ID"] == "SELL-001"
        assert rows[0]["Trade-ID"] == "TRADE-002"


class TestCSVExport:
    def test_csv_export(self, tmp_path: Path) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        csv_path = tmp_path / "tax_report.csv"
        gen.export_csv(year, csv_path)
        assert csv_path.exists()
        content = csv_path.read_text(encoding="utf-8")
        assert "Bitcoin (BTC)" in content
        assert "FIFO" in content

    def test_csv_empty_year_skips(self, tmp_path: Path) -> None:
        ledger = FIFOLedger()
        gen = TaxReportGenerator(ledger)
        csv_path = tmp_path / "empty.csv"
        gen.export_csv(2020, csv_path)
        assert not csv_path.exists()


class TestJSONExport:
    def test_json_export(self, tmp_path: Path) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        json_path = tmp_path / "tax_report.json"
        gen.export_json(year, json_path)
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["year"] == year
        assert "summary" in data
        assert "disposals" in data
        assert data["method"] == "FIFO per BMF 10.05.2022"
        assert data["legal_basis"] == "§23 Abs. 1 Satz 1 Nr. 2 EStG"

    def test_json_summary_fields(self, tmp_path: Path) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        json_path = tmp_path / "report.json"
        gen.export_json(year, json_path)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        s = data["summary"]
        assert "total_disposals" in s
        assert "net_taxable_eur" in s
        assert "within_freigrenze" in s


class TestTextSummary:
    def test_format_summary(self) -> None:
        ledger = _make_ledger_with_trade()
        gen = TaxReportGenerator(ledger)
        year = datetime.now(UTC).year
        text = gen.format_summary_text(year)
        assert f"Tax Report {year}" in text
        assert "Total disposals" in text
        assert "Taxable" in text
        assert "Freigrenze" in text
