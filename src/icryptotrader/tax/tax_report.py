"""Tax Report Generator — §23 EStG annual Anlage SO report.

Generates the required fields per disposal for German tax filing:
  - Art des Wirtschaftsguts: "Bitcoin (BTC)"
  - Datum der Anschaffung / Veräußerung
  - Veräußerungspreis (EUR)
  - Anschaffungskosten (EUR)
  - Werbungskosten (fees, EUR)
  - Gewinn/Verlust (EUR)
  - Haltefrist überschritten (bool)

Supporting documentation must be retained 10 years (§147 AO).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from icryptotrader.tax.fifo_ledger import FIFOLedger

logger = logging.getLogger(__name__)

FREIGRENZE_EUR = Decimal("1000")


@dataclass
class AnnualSummary:
    """Summary of tax-relevant activity for a given year."""

    year: int
    total_disposals: int
    taxable_disposals: int
    tax_free_disposals: int
    total_proceeds_eur: Decimal
    total_cost_basis_eur: Decimal
    total_fees_eur: Decimal
    taxable_gain_eur: Decimal
    taxable_loss_eur: Decimal
    net_taxable_eur: Decimal
    within_freigrenze: bool


class TaxReportGenerator:
    """Generates §23 EStG tax reports from the FIFO ledger.

    Usage:
        gen = TaxReportGenerator(ledger)
        summary = gen.annual_summary(2025)
        gen.export_csv(2025, Path("tax_report_2025.csv"))
        gen.export_json(2025, Path("tax_report_2025.json"))
    """

    def __init__(self, ledger: FIFOLedger) -> None:
        self._ledger = ledger

    def annual_summary(self, year: int) -> AnnualSummary:
        """Compute summary statistics for a tax year."""
        disposals = self._ledger.all_disposals(year=year)

        taxable = [d for d in disposals if d.is_taxable]
        tax_free = [d for d in disposals if not d.is_taxable]

        total_proceeds = sum(d.sale_total_eur for d in disposals)
        total_cost = sum(d.cost_basis_eur for d in disposals)
        total_fees = sum(
            d.sale_fee_usd / d.exchange_rate_eur_usd
            for d in disposals
            if d.exchange_rate_eur_usd > 0
        )

        taxable_gain = sum(d.gain_loss_eur for d in taxable if d.gain_loss_eur > 0)
        taxable_loss = sum(d.gain_loss_eur for d in taxable if d.gain_loss_eur < 0)
        net_taxable = sum(d.gain_loss_eur for d in taxable)

        return AnnualSummary(
            year=year,
            total_disposals=len(disposals),
            taxable_disposals=len(taxable),
            tax_free_disposals=len(tax_free),
            total_proceeds_eur=total_proceeds,
            total_cost_basis_eur=total_cost,
            total_fees_eur=total_fees,
            taxable_gain_eur=taxable_gain,
            taxable_loss_eur=taxable_loss,
            net_taxable_eur=net_taxable,
            within_freigrenze=net_taxable < FREIGRENZE_EUR,
        )

    def disposal_rows(self, year: int) -> list[dict[str, str]]:
        """Generate per-disposal rows for Anlage SO."""
        disposals = self._ledger.all_disposals(year=year)
        rows: list[dict[str, str]] = []

        # Need lot data for purchase dates
        lot_map = {lot.lot_id: lot for lot in self._ledger.lots}

        for d in disposals:
            lot = lot_map.get(d.lot_id)
            purchase_date = (
                lot.purchase_timestamp.strftime("%d.%m.%Y") if lot else "Unknown"
            )

            fee_eur = (
                d.sale_fee_usd / d.exchange_rate_eur_usd
                if d.exchange_rate_eur_usd > 0
                else Decimal("0")
            )

            rows.append({
                "Art des Wirtschaftsguts": "Bitcoin (BTC)",
                "Menge": str(d.quantity_btc),
                "Datum der Anschaffung": purchase_date,
                "Datum der Veräußerung": d.disposal_timestamp.strftime("%d.%m.%Y"),
                "Veräußerungspreis (EUR)": f"{d.sale_total_eur:.2f}",
                "Anschaffungskosten (EUR)": f"{d.cost_basis_eur:.2f}",
                "Werbungskosten (EUR)": f"{fee_eur:.2f}",
                "Gewinn/Verlust (EUR)": f"{d.gain_loss_eur:.2f}",
                "Haltefrist überschritten": "Ja" if not d.is_taxable else "Nein",
                "Haltedauer (Tage)": str(d.days_held_at_disposal),
                "Steuerpflichtig": "Nein" if not d.is_taxable else "Ja",
                "Order-ID": d.exchange_order_id,
                "Trade-ID": d.exchange_trade_id,
                "EUR/USD Kurs": str(d.exchange_rate_eur_usd),
                "Methode": "FIFO (§23 Abs. 1 Satz 1 Nr. 2 EStG)",
            })

        return rows

    def export_csv(self, year: int, path: Path) -> None:
        """Export disposals as CSV for tax advisor / Finanzamt."""
        rows = self.disposal_rows(year)
        if not rows:
            logger.info("No disposals for %d, skipping CSV export", year)
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys(), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Tax report CSV exported to %s (%d rows)", path, len(rows))

    def export_json(self, year: int, path: Path) -> None:
        """Export disposals and summary as JSON."""
        summary = self.annual_summary(year)
        rows = self.disposal_rows(year)

        data = {
            "year": year,
            "summary": {
                "total_disposals": summary.total_disposals,
                "taxable_disposals": summary.taxable_disposals,
                "tax_free_disposals": summary.tax_free_disposals,
                "total_proceeds_eur": str(summary.total_proceeds_eur),
                "total_cost_basis_eur": str(summary.total_cost_basis_eur),
                "total_fees_eur": str(summary.total_fees_eur),
                "taxable_gain_eur": str(summary.taxable_gain_eur),
                "taxable_loss_eur": str(summary.taxable_loss_eur),
                "net_taxable_eur": str(summary.net_taxable_eur),
                "within_freigrenze": summary.within_freigrenze,
            },
            "disposals": rows,
            "method": "FIFO per BMF 10.05.2022",
            "legal_basis": "§23 Abs. 1 Satz 1 Nr. 2 EStG",
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info("Tax report JSON exported to %s", path)

    def format_summary_text(self, year: int) -> str:
        """Human-readable summary for Telegram / logging."""
        s = self.annual_summary(year)
        buf = io.StringIO()
        buf.write(f"Tax Report {s.year}\n")
        buf.write(f"{'=' * 40}\n")
        buf.write(f"Total disposals:    {s.total_disposals}\n")
        buf.write(f"  Taxable:          {s.taxable_disposals}\n")
        buf.write(f"  Tax-free (>1yr):  {s.tax_free_disposals}\n")
        buf.write(f"Proceeds (EUR):     {s.total_proceeds_eur:.2f}\n")
        buf.write(f"Cost basis (EUR):   {s.total_cost_basis_eur:.2f}\n")
        buf.write(f"Fees (EUR):         {s.total_fees_eur:.2f}\n")
        buf.write(f"Taxable gains:      {s.taxable_gain_eur:.2f}\n")
        buf.write(f"Taxable losses:     {s.taxable_loss_eur:.2f}\n")
        buf.write(f"Net taxable:        {s.net_taxable_eur:.2f}\n")
        freigrenze = "YES" if s.within_freigrenze else "NO"
        buf.write(f"Within Freigrenze:  {freigrenze} (< EUR {FREIGRENZE_EUR})\n")
        return buf.getvalue()
