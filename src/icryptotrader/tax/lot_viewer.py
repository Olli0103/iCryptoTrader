"""Lot Age Visualization — CLI view of FIFO lot age distribution.

Shows:
  - Per-lot age, quantity, cost basis, status, and days until tax-free
  - Age distribution histogram (bucketed by 30-day intervals)
  - Summary: total BTC, tax-free %, locked %, next unlock date
  - Projected tax-free unlock schedule
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from icryptotrader.tax.fifo_ledger import HOLDING_PERIOD_DAYS, FIFOLedger, TaxLot
from icryptotrader.types import LotStatus

# Age bucket boundaries in days
_AGE_BUCKETS = [
    (0, 30, "0-30d"),
    (30, 90, "30-90d"),
    (90, 180, "90-180d"),
    (180, 270, "180-270d"),
    (270, 330, "270-330d"),
    (330, 365, "330-365d"),
    (365, 99999, ">365d"),
]

# Bar chart max width
_BAR_WIDTH = 30


def format_lot_table(ledger: FIFOLedger) -> str:
    """Format a table of all open lots with age and status info."""
    lots = ledger.open_lots()
    if not lots:
        return "No open lots in ledger."

    buf = io.StringIO()
    buf.write(f"{'Lot ID':>8}  {'Age':>6}  {'Qty BTC':>12}  {'Cost/BTC EUR':>14}  "
              f"{'Status':>10}  {'Tax-Free In':>12}\n")
    buf.write("-" * 78 + "\n")

    for lot in lots:
        lot_id = lot.lot_id[:8]
        age = f"{lot.days_held}d"
        qty = f"{lot.remaining_qty_btc:.8f}"
        cost = f"{lot.cost_basis_per_btc_eur:.2f}"
        status = _lot_status_label(lot)
        days_left = _days_until_free(lot)
        free_in = "TAX-FREE" if lot.is_tax_free else f"{days_left}d"

        buf.write(f"{lot_id:>8}  {age:>6}  {qty:>12}  {cost:>14}  "
                  f"{status:>10}  {free_in:>12}\n")

    return buf.getvalue()


def format_age_histogram(ledger: FIFOLedger) -> str:
    """Format an ASCII histogram of lot ages by BTC quantity."""
    lots = ledger.open_lots()
    if not lots:
        return "No open lots."

    # Bucket lots by age
    buckets: dict[str, Decimal] = {}
    for _start, _end, label in _AGE_BUCKETS:
        buckets[label] = Decimal("0")

    for lot in lots:
        for start, end, label in _AGE_BUCKETS:
            if start <= lot.days_held < end:
                buckets[label] += lot.remaining_qty_btc
                break

    # Find max for scaling
    max_qty = max(buckets.values()) if buckets else Decimal("1")
    if max_qty == 0:
        max_qty = Decimal("1")

    buf = io.StringIO()
    buf.write("Lot Age Distribution (BTC)\n")
    buf.write("=" * 60 + "\n")

    for _start, _end, label in _AGE_BUCKETS:
        qty = buckets[label]
        bar_len = int(float(qty / max_qty) * _BAR_WIDTH) if qty > 0 else 0
        bar = "#" * bar_len
        buf.write(f"  {label:>8}  |{bar:<{_BAR_WIDTH}}| {qty:.8f} BTC\n")

    return buf.getvalue()


def format_unlock_schedule(ledger: FIFOLedger) -> str:
    """Format a projected tax-free unlock schedule."""
    lots = ledger.open_lots()
    locked = [lot for lot in lots if not lot.is_tax_free]
    if not locked:
        return "All lots are already tax-free."

    # Sort by tax-free date
    locked.sort(key=lambda lot: lot.tax_free_date)

    buf = io.StringIO()
    buf.write("Projected Tax-Free Unlock Schedule\n")
    buf.write("=" * 60 + "\n")
    buf.write(f"{'Date':>12}  {'Days Left':>10}  {'Qty BTC':>12}  {'Cumulative':>12}\n")
    buf.write("-" * 52 + "\n")

    cumulative = Decimal("0")
    for lot in locked:
        days_left = _days_until_free(lot)
        cumulative += lot.remaining_qty_btc
        date_str = lot.tax_free_date.strftime("%Y-%m-%d")
        buf.write(
            f"{date_str:>12}  {days_left:>10}d  "
            f"{lot.remaining_qty_btc:.8f}  {cumulative:.8f}\n"
        )

    return buf.getvalue()


def format_summary(ledger: FIFOLedger) -> str:
    """Format a concise portfolio summary with tax status."""
    total = ledger.total_btc()
    tax_free = ledger.tax_free_btc()
    locked = ledger.locked_btc()
    ratio = ledger.sellable_ratio()
    next_free = ledger.days_until_next_free()
    near = ledger.near_threshold_btc()
    num_lots = len(ledger.open_lots())

    buf = io.StringIO()
    buf.write("Portfolio Tax Summary\n")
    buf.write("=" * 40 + "\n")
    buf.write(f"  Open lots:       {num_lots}\n")
    buf.write(f"  Total BTC:       {total:.8f}\n")
    buf.write(f"  Tax-free BTC:    {tax_free:.8f} ({ratio * 100:.1f}%)\n")
    buf.write(f"  Locked BTC:      {locked:.8f} ({(1 - ratio) * 100:.1f}%)\n")
    buf.write(f"  Near-threshold:  {near:.8f} (330-365d)\n")

    if next_free is not None:
        unlock_date = datetime.now(UTC) + timedelta(days=next_free)
        buf.write(f"  Next unlock:     {next_free}d ({unlock_date.strftime('%Y-%m-%d')})\n")
    else:
        buf.write("  Next unlock:     N/A (all free or empty)\n")

    ytd_gain = ledger.taxable_gain_ytd()
    buf.write(f"  YTD taxable:     EUR {ytd_gain:.2f}\n")

    return buf.getvalue()


def format_full_report(ledger: FIFOLedger) -> str:
    """Format a complete lot age report combining all views."""
    parts = [
        format_summary(ledger),
        "",
        format_age_histogram(ledger),
        "",
        format_lot_table(ledger),
        "",
        format_unlock_schedule(ledger),
    ]
    return "\n".join(parts)


def _lot_status_label(lot: TaxLot) -> str:
    """Human-readable status label for a lot."""
    if lot.status == LotStatus.OPEN:
        return "OPEN"
    if lot.status == LotStatus.PARTIALLY_SOLD:
        return "PARTIAL"
    return "CLOSED"


def _days_until_free(lot: TaxLot) -> int:
    """Days until a lot becomes tax-free. 0 if already free."""
    remaining = HOLDING_PERIOD_DAYS - lot.days_held
    return max(0, remaining)
