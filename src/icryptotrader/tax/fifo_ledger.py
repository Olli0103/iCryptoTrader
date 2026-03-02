"""Tax-Aware FIFO Ledger for German §23 EStG compliance.

Tracks every BTC purchase as a TaxLot with timestamp, cost basis in USD and EUR.
On every sell, consumes lots in FIFO order (oldest first) per BMF 10.05.2022.
Lots held >365 days are tax-free (Haltefrist überschritten).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from icryptotrader.types import LotStatus

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Approximate holding period in days (used for informational display only).
# For actual tax decisions, use _one_year_after() which handles leap years.
HOLDING_PERIOD_DAYS = 365

# Safety buffer: German tax uses CET/CEST but our timestamps are UTC,
# creating up to 2 hours of drift across timezone boundaries.  Adding
# 1 day ensures we never sell a lot that hasn't fully cleared the
# holding period in CET.
_TAX_SAFETY_BUFFER = timedelta(days=1)


def _one_year_after(dt: datetime) -> datetime:
    """Compute the date exactly one calendar year after ``dt``.

    German §23 EStG uses "one year" (1 Jahr) per BGB §188, meaning the
    same calendar date the following year.  This handles leap years:
      - 2024-02-29 + 1 year → 2025-02-28 (Feb 29 doesn't exist in 2025)
      - 2024-03-01 + 1 year → 2025-03-01 (366 days, not 365)

    Adds a 1-day safety buffer for UTC→CET timezone drift.
    """
    try:
        anniversary = dt.replace(year=dt.year + 1)
    except ValueError:
        # Feb 29 in a leap year → Feb 28 in the next non-leap year
        anniversary = dt.replace(year=dt.year + 1, month=2, day=28)
    return anniversary + _TAX_SAFETY_BUFFER


@dataclass
class TaxLot:
    """A single BTC purchase lot for FIFO tracking."""

    lot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Purchase data
    purchase_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    quantity_btc: Decimal = Decimal("0")
    remaining_qty_btc: Decimal = Decimal("0")
    purchase_price_usd: Decimal = Decimal("0")
    purchase_total_usd: Decimal = Decimal("0")
    purchase_fee_usd: Decimal = Decimal("0")

    # EUR conversion (required for German tax filing)
    purchase_price_eur: Decimal = Decimal("0")
    purchase_total_eur: Decimal = Decimal("0")
    exchange_rate_eur_usd: Decimal = Decimal("0")

    # Exchange identifiers
    exchange_order_id: str = ""
    exchange_trade_id: str = ""

    # Metadata
    source_engine: str = ""  # "grid" or "signal"
    grid_level: int | None = None

    # Status
    status: LotStatus = LotStatus.OPEN

    # Disposals from this lot
    disposals: list[Disposal] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.remaining_qty_btc == Decimal("0") and self.quantity_btc > 0:
            self.remaining_qty_btc = self.quantity_btc

    @property
    def days_held(self) -> int:
        return (datetime.now(UTC) - self.purchase_timestamp).days

    @property
    def is_tax_free(self) -> bool:
        """Whether this lot has exceeded the §23 EStG 1-year holding period."""
        return datetime.now(UTC) >= self.tax_free_date

    @property
    def tax_free_date(self) -> datetime:
        """Date when this lot becomes tax-free (1 calendar year + safety buffer)."""
        return _one_year_after(self.purchase_timestamp)

    @property
    def cost_basis_per_btc_eur(self) -> Decimal:
        if self.quantity_btc == 0:
            return Decimal("0")
        return self.purchase_total_eur / self.quantity_btc


@dataclass
class Disposal:
    """Records a (partial) sale of a TaxLot under FIFO."""

    disposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    lot_id: str = ""
    disposal_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Sale data
    quantity_btc: Decimal = Decimal("0")
    sale_price_usd: Decimal = Decimal("0")
    sale_total_usd: Decimal = Decimal("0")
    sale_fee_usd: Decimal = Decimal("0")

    # EUR conversion at time of sale
    sale_price_eur: Decimal = Decimal("0")
    sale_total_eur: Decimal = Decimal("0")
    exchange_rate_eur_usd: Decimal = Decimal("0")

    # Computed tax fields
    cost_basis_eur: Decimal = Decimal("0")
    gain_loss_eur: Decimal = Decimal("0")
    is_taxable: bool = True
    days_held_at_disposal: int = 0

    # Exchange identifiers
    exchange_order_id: str = ""
    exchange_trade_id: str = ""

    # Splinter index: when a single exchange trade crosses tax-lot boundaries
    # (e.g., 0.6 BTC from a tax-free lot + 0.4 BTC from a taxable lot),
    # each resulting Disposal gets a sequential index (0, 1, ...) so German
    # Finanzamt software can distinguish the sub-transactions under one
    # exchange_trade_id.  -1 means unsplit (single lot consumed).
    splinter_index: int = -1


class FIFOLedger:
    """FIFO-ordered ledger of all BTC lots.

    Primary data structure for tax-aware trading. Lots are always ordered
    by purchase_timestamp ascending (FIFO). Every sell consumes from the
    oldest lots first.

    Aggregates (total_btc, tax_free_btc) are cached and invalidated on mutation
    to avoid O(n) scans on every strategy tick.
    """

    def __init__(self) -> None:
        self._lots: list[TaxLot] = []
        self._cache_valid = False
        self._cached_total_btc: Decimal = Decimal("0")
        self._cached_tax_free_btc: Decimal = Decimal("0")

    def _invalidate_cache(self) -> None:
        self._cache_valid = False

    def _ensure_cache(self) -> None:
        if self._cache_valid:
            return
        total = Decimal("0")
        tax_free = Decimal("0")
        for lot in self._lots:
            if lot.status != LotStatus.CLOSED:
                total += lot.remaining_qty_btc
                if lot.is_tax_free:
                    tax_free += lot.remaining_qty_btc
        self._cached_total_btc = total
        self._cached_tax_free_btc = tax_free
        self._cache_valid = True

    @property
    def lots(self) -> list[TaxLot]:
        return self._lots

    def add_lot(
        self,
        quantity_btc: Decimal,
        purchase_price_usd: Decimal,
        purchase_fee_usd: Decimal,
        eur_usd_rate: Decimal,
        purchase_timestamp: datetime | None = None,
        exchange_order_id: str = "",
        exchange_trade_id: str = "",
        source_engine: str = "",
        grid_level: int | None = None,
    ) -> TaxLot:
        """Record a new BTC purchase as a FIFO lot."""
        ts = purchase_timestamp or datetime.now(UTC)
        total_usd = quantity_btc * purchase_price_usd + purchase_fee_usd

        lot = TaxLot(
            purchase_timestamp=ts,
            quantity_btc=quantity_btc,
            remaining_qty_btc=quantity_btc,
            purchase_price_usd=purchase_price_usd,
            purchase_total_usd=total_usd,
            purchase_fee_usd=purchase_fee_usd,
            purchase_price_eur=purchase_price_usd / eur_usd_rate,
            purchase_total_eur=total_usd / eur_usd_rate,
            exchange_rate_eur_usd=eur_usd_rate,
            exchange_order_id=exchange_order_id,
            exchange_trade_id=exchange_trade_id,
            source_engine=source_engine,
            grid_level=grid_level,
        )

        # Insert in timestamp order (most additions are at the end)
        self._lots.append(lot)
        self._lots.sort(key=lambda x: x.purchase_timestamp)

        self._invalidate_cache()
        logger.info(
            "FIFO lot added: %s BTC @ $%s (lot %s, %s)",
            quantity_btc, purchase_price_usd, lot.lot_id[:8], source_engine,
        )
        return lot

    def sell_fifo(
        self,
        quantity_btc: Decimal,
        sale_price_usd: Decimal,
        sale_fee_usd: Decimal,
        eur_usd_rate: Decimal,
        sale_timestamp: datetime | None = None,
        exchange_order_id: str = "",
        exchange_trade_id: str = "",
    ) -> list[Disposal]:
        """Execute a FIFO sell. Consumes from oldest lots first.

        Returns list of Disposal records for tax reporting.
        Raises ValueError if insufficient BTC to sell.
        """
        available = self.total_btc()
        if quantity_btc > available:
            raise ValueError(
                f"Cannot sell {quantity_btc} BTC: only {available} available in FIFO ledger"
            )

        ts = sale_timestamp or datetime.now(UTC)
        remaining_to_sell = quantity_btc
        disposals: list[Disposal] = []
        splinter_idx = 0

        for lot in self._lots:
            if remaining_to_sell <= 0:
                break
            if lot.status == LotStatus.CLOSED:
                continue

            sell_from_lot = min(lot.remaining_qty_btc, remaining_to_sell)

            # Proportional cost basis from this lot
            cost_proportion = sell_from_lot / lot.quantity_btc
            cost_basis_eur = cost_proportion * lot.purchase_total_eur

            # Sale proceeds for this portion
            sale_proceeds_usd = sell_from_lot * sale_price_usd
            proportional_fee = (sell_from_lot / quantity_btc) * sale_fee_usd
            net_proceeds_usd = sale_proceeds_usd - proportional_fee
            net_proceeds_eur = net_proceeds_usd / eur_usd_rate

            gain_loss_eur = net_proceeds_eur - cost_basis_eur

            disposal = Disposal(
                lot_id=lot.lot_id,
                disposal_timestamp=ts,
                quantity_btc=sell_from_lot,
                sale_price_usd=sale_price_usd,
                sale_total_usd=net_proceeds_usd,
                sale_fee_usd=proportional_fee,
                sale_price_eur=sale_price_usd / eur_usd_rate,
                sale_total_eur=net_proceeds_eur,
                exchange_rate_eur_usd=eur_usd_rate,
                cost_basis_eur=cost_basis_eur,
                gain_loss_eur=gain_loss_eur,
                is_taxable=not lot.is_tax_free,
                days_held_at_disposal=lot.days_held,
                exchange_order_id=exchange_order_id,
                exchange_trade_id=exchange_trade_id,
                splinter_index=splinter_idx,
            )

            lot.remaining_qty_btc -= sell_from_lot
            if lot.remaining_qty_btc == 0:
                lot.status = LotStatus.CLOSED
            else:
                lot.status = LotStatus.PARTIALLY_SOLD
            lot.disposals.append(disposal)
            disposals.append(disposal)
            remaining_to_sell -= sell_from_lot
            splinter_idx += 1

        self._invalidate_cache()
        total_gain = sum(d.gain_loss_eur for d in disposals)
        taxable_count = sum(1 for d in disposals if d.is_taxable)
        logger.info(
            "FIFO sell: %s BTC @ $%s → %d disposals (%d taxable), gain/loss: EUR %.2f",
            quantity_btc, sale_price_usd, len(disposals), taxable_count, total_gain,
        )
        return disposals

    # --- Query methods ---

    def total_btc(self) -> Decimal:
        self._ensure_cache()
        return self._cached_total_btc

    def tax_free_btc(self) -> Decimal:
        self._ensure_cache()
        return self._cached_tax_free_btc

    def locked_btc(self) -> Decimal:
        """BTC that cannot be sold tax-free (held < 365 days)."""
        return self.total_btc() - self.tax_free_btc()

    def sellable_ratio(self) -> float:
        """Fraction of total BTC that is tax-free. 0.0 to 1.0."""
        total = self.total_btc()
        if total == 0:
            return 0.0
        return float(self.tax_free_btc() / total)

    def days_until_next_free(self) -> int | None:
        """Days until the next locked lot becomes tax-free. None if all free or empty.

        Uses calendar-year holding period (not fixed 365 days) to handle
        leap years correctly.
        """
        now = datetime.now(UTC)
        min_days: int | None = None
        for lot in self._lots:
            if lot.status == LotStatus.CLOSED or lot.is_tax_free:
                continue
            days_left = (lot.tax_free_date - now).days
            if days_left > 0 and (min_days is None or days_left < min_days):
                min_days = days_left
        return min_days

    def near_threshold_btc(self, near_days: int = 330) -> Decimal:
        """BTC held between near_days and 365 days (approaching tax-free)."""
        return sum(
            (lot.remaining_qty_btc
            for lot in self._lots
            if lot.status != LotStatus.CLOSED
            and near_days <= lot.days_held < HOLDING_PERIOD_DAYS),
            Decimal("0"),
        )

    def open_lots(self) -> list[TaxLot]:
        return [lot for lot in self._lots if lot.status != LotStatus.CLOSED]

    def underwater_lots(
        self,
        current_price_usd: Decimal,
        eur_usd_rate: Decimal,
        near_threshold_days: int = 330,
    ) -> list[tuple[TaxLot, Decimal]]:
        """Return open lots with unrealized losses, sorted by loss magnitude.

        Returns list of (lot, estimated_loss_eur) where loss_eur < 0.
        Excludes:
        - Closed lots
        - Tax-free lots (selling at a loss has no tax benefit)
        - Lots within near_threshold_days of maturity (protect for Haltefrist)
        """
        results: list[tuple[TaxLot, Decimal]] = []
        for lot in self._lots:
            if lot.status == LotStatus.CLOSED:
                continue
            if lot.is_tax_free:
                continue
            if lot.days_held >= near_threshold_days:
                continue

            current_value_eur = (lot.remaining_qty_btc * current_price_usd) / eur_usd_rate
            cost_basis_eur = (lot.remaining_qty_btc / lot.quantity_btc) * lot.purchase_total_eur
            unrealized_pnl = current_value_eur - cost_basis_eur

            if unrealized_pnl < 0:
                results.append((lot, unrealized_pnl))

        results.sort(key=lambda x: x[1])  # Most negative first
        return results

    def all_disposals(self, year: int | None = None) -> list[Disposal]:
        """All disposals, optionally filtered by tax year."""
        disposals: list[Disposal] = []
        for lot in self._lots:
            for d in lot.disposals:
                if year is None or d.disposal_timestamp.year == year:
                    disposals.append(d)
        return disposals

    def taxable_gain_ytd(self, year: int | None = None) -> Decimal:
        """Sum of taxable gains/losses for the given year (default: current year)."""
        yr = year or date.today().year
        return sum(
            (d.gain_loss_eur
            for d in self.all_disposals(yr)
            if d.is_taxable),
            Decimal("0"),
        )

    # --- Persistence ---

    def save(self, path: Path) -> None:
        """Save ledger to JSON file using atomic write (temp + rename + fsync).

        Prevents data loss if the process crashes during write. The rename
        operation is atomic on POSIX filesystems, so readers always see
        either the old or new file — never a partial write.
        """
        import os
        import tempfile

        data = [_lot_to_dict(lot) for lot in self._lots]
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file in the same directory, then atomic rename
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            logger.info("FIFO ledger saved to %s (%d lots)", path, len(data))
        except BaseException:
            # Clean up temp file on any failure
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def load(self, path: Path) -> None:
        """Load ledger from JSON file."""
        if not path.exists():
            logger.info("No ledger file at %s, starting fresh", path)
            return
        with open(path) as f:
            data = json.load(f)
        self._lots = [_dict_to_lot(d) for d in data]
        self._lots.sort(key=lambda x: x.purchase_timestamp)
        self._invalidate_cache()
        logger.info("FIFO ledger loaded from %s (%d lots)", path, len(self._lots))

    def save_sqlite(self, path: Path) -> None:
        """Save ledger to SQLite database (ACID-compliant).

        SQLite provides full ACID guarantees — WAL mode ensures readers
        never block writers and vice versa. This is safer than JSON
        for high-frequency write patterns (every fill).
        """
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lots (
                    lot_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("DELETE FROM lots")
            for lot in self._lots:
                conn.execute(
                    "INSERT INTO lots (lot_id, data) VALUES (?, ?)",
                    (lot.lot_id, json.dumps(_lot_to_dict(lot), default=str)),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("lot_count", str(len(self._lots))),
            )
            conn.commit()
            logger.info(
                "FIFO ledger saved to SQLite %s (%d lots)",
                path, len(self._lots),
            )
        finally:
            conn.close()

    def load_sqlite(self, path: Path) -> None:
        """Load ledger from SQLite database."""
        import sqlite3

        if not path.exists():
            logger.info("No SQLite ledger at %s, starting fresh", path)
            return
        conn = sqlite3.connect(str(path))
        try:
            cursor = conn.execute("SELECT data FROM lots")
            rows = cursor.fetchall()
            self._lots = [
                _dict_to_lot(json.loads(row[0])) for row in rows
            ]
            self._lots.sort(key=lambda x: x.purchase_timestamp)
            self._invalidate_cache()
            logger.info(
                "FIFO ledger loaded from SQLite %s (%d lots)",
                path, len(self._lots),
            )
        finally:
            conn.close()


def _lot_to_dict(lot: TaxLot) -> dict:  # type: ignore[type-arg]
    """Serialize a TaxLot to a JSON-safe dict."""
    return {
        "lot_id": lot.lot_id,
        "purchase_timestamp": lot.purchase_timestamp.isoformat(),
        "quantity_btc": str(lot.quantity_btc),
        "remaining_qty_btc": str(lot.remaining_qty_btc),
        "purchase_price_usd": str(lot.purchase_price_usd),
        "purchase_total_usd": str(lot.purchase_total_usd),
        "purchase_fee_usd": str(lot.purchase_fee_usd),
        "purchase_price_eur": str(lot.purchase_price_eur),
        "purchase_total_eur": str(lot.purchase_total_eur),
        "exchange_rate_eur_usd": str(lot.exchange_rate_eur_usd),
        "exchange_order_id": lot.exchange_order_id,
        "exchange_trade_id": lot.exchange_trade_id,
        "source_engine": lot.source_engine,
        "grid_level": lot.grid_level,
        "status": lot.status.value,
        "disposals": [_disposal_to_dict(d) for d in lot.disposals],
    }


def _disposal_to_dict(d: Disposal) -> dict:  # type: ignore[type-arg]
    return {
        "disposal_id": d.disposal_id,
        "lot_id": d.lot_id,
        "disposal_timestamp": d.disposal_timestamp.isoformat(),
        "quantity_btc": str(d.quantity_btc),
        "sale_price_usd": str(d.sale_price_usd),
        "sale_total_usd": str(d.sale_total_usd),
        "sale_fee_usd": str(d.sale_fee_usd),
        "sale_price_eur": str(d.sale_price_eur),
        "sale_total_eur": str(d.sale_total_eur),
        "exchange_rate_eur_usd": str(d.exchange_rate_eur_usd),
        "cost_basis_eur": str(d.cost_basis_eur),
        "gain_loss_eur": str(d.gain_loss_eur),
        "is_taxable": d.is_taxable,
        "days_held_at_disposal": d.days_held_at_disposal,
        "exchange_order_id": d.exchange_order_id,
        "exchange_trade_id": d.exchange_trade_id,
    }


def _dict_to_lot(d: dict) -> TaxLot:  # type: ignore[type-arg]
    disposals = [_dict_to_disposal(dd) for dd in d.get("disposals", [])]
    return TaxLot(
        lot_id=d["lot_id"],
        purchase_timestamp=datetime.fromisoformat(d["purchase_timestamp"]),
        quantity_btc=Decimal(d["quantity_btc"]),
        remaining_qty_btc=Decimal(d["remaining_qty_btc"]),
        purchase_price_usd=Decimal(d["purchase_price_usd"]),
        purchase_total_usd=Decimal(d["purchase_total_usd"]),
        purchase_fee_usd=Decimal(d["purchase_fee_usd"]),
        purchase_price_eur=Decimal(d["purchase_price_eur"]),
        purchase_total_eur=Decimal(d["purchase_total_eur"]),
        exchange_rate_eur_usd=Decimal(d["exchange_rate_eur_usd"]),
        exchange_order_id=d.get("exchange_order_id", ""),
        exchange_trade_id=d.get("exchange_trade_id", ""),
        source_engine=d.get("source_engine", ""),
        grid_level=d.get("grid_level"),
        status=LotStatus(d["status"]),
        disposals=disposals,
    )


def _dict_to_disposal(d: dict) -> Disposal:  # type: ignore[type-arg]
    return Disposal(
        disposal_id=d["disposal_id"],
        lot_id=d["lot_id"],
        disposal_timestamp=datetime.fromisoformat(d["disposal_timestamp"]),
        quantity_btc=Decimal(d["quantity_btc"]),
        sale_price_usd=Decimal(d["sale_price_usd"]),
        sale_total_usd=Decimal(d["sale_total_usd"]),
        sale_fee_usd=Decimal(d["sale_fee_usd"]),
        sale_price_eur=Decimal(d["sale_price_eur"]),
        sale_total_eur=Decimal(d["sale_total_eur"]),
        exchange_rate_eur_usd=Decimal(d["exchange_rate_eur_usd"]),
        cost_basis_eur=Decimal(d["cost_basis_eur"]),
        gain_loss_eur=Decimal(d["gain_loss_eur"]),
        is_taxable=d["is_taxable"],
        days_held_at_disposal=d["days_held_at_disposal"],
        exchange_order_id=d.get("exchange_order_id", ""),
        exchange_trade_id=d.get("exchange_trade_id", ""),
    )
