"""Tests for the Tax Agent veto mechanism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent
from icryptotrader.types import TaxVetoDecision


def _make_ledger_with_lots(
    *ages_days: int, qty: Decimal = Decimal("0.01"),
) -> FIFOLedger:
    """Create a ledger with lots of given ages in days."""
    ledger = FIFOLedger()
    now = datetime.now(UTC)
    for age in ages_days:
        ledger.add_lot(
            quantity_btc=qty,
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=now - timedelta(days=age),
        )
    return ledger


class TestEmergencyOverride:
    def test_emergency_overrides_tax_lock(self) -> None:
        ledger = _make_ledger_with_lots(10)  # Young lot, would be locked
        agent = TaxAgent(ledger=ledger, emergency_dd_pct=0.20)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
            portfolio_drawdown_pct=0.25,
        )
        assert result.decision == TaxVetoDecision.OVERRIDE_EMERGENCY
        assert result.allowed_qty_btc == Decimal("0.01")

    def test_no_override_below_threshold(self) -> None:
        ledger = _make_ledger_with_lots(10)
        agent = TaxAgent(ledger=ledger, emergency_dd_pct=0.20)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
            portfolio_drawdown_pct=0.15,
        )
        assert result.decision != TaxVetoDecision.OVERRIDE_EMERGENCY


class TestTaxFreeLots:
    def test_fully_tax_free(self) -> None:
        ledger = _make_ledger_with_lots(400)  # >365 days
        agent = TaxAgent(ledger=ledger)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.ALLOW
        assert result.allowed_qty_btc == Decimal("0.01")

    def test_partial_tax_free(self) -> None:
        ledger = _make_ledger_with_lots(400, 10)  # One free, one locked
        agent = TaxAgent(ledger=ledger)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.02"),  # Want both lots
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.ALLOW_PARTIAL
        assert result.allowed_qty_btc == Decimal("0.01")  # Only the free lot


class TestFreigrenze:
    def test_within_freigrenze_allows_sell(self) -> None:
        ledger = _make_ledger_with_lots(100)  # Young lot, not free
        agent = TaxAgent(ledger=ledger, annual_exemption_eur=Decimal("1000"))
        # Price unchanged, so gain ≈ 0 which is < 1000 EUR
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.ALLOW

    def test_exceeds_freigrenze_veto(self) -> None:
        # Create a lot bought at low price, selling at high price
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("1.0"),
            purchase_price_usd=Decimal("50000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=100),
        )
        agent = TaxAgent(ledger=ledger, annual_exemption_eur=Decimal("1000"))
        # Selling 1 BTC at $85000 bought at $50000 = ~$35000 gain = ~€32407
        result = agent.evaluate_sell(
            qty_btc=Decimal("1.0"),
            current_price_usd=Decimal("85000"),
        )
        # Should be VETO since gain exceeds Freigrenze and no near-threshold
        assert result.decision == TaxVetoDecision.VETO


class TestNearThreshold:
    def test_near_threshold_protected(self) -> None:
        # Use a large gain scenario to exceed Freigrenze
        ledger2 = FIFOLedger()
        ledger2.add_lot(
            quantity_btc=Decimal("1.0"),
            purchase_price_usd=Decimal("50000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=340),
        )
        agent2 = TaxAgent(ledger=ledger2, near_threshold_days=330)
        result = agent2.evaluate_sell(
            qty_btc=Decimal("1.0"),
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.VETO
        assert "approaching" in result.reason.lower() or "near" in result.reason.lower()


class TestFullLock:
    def test_all_lots_locked(self) -> None:
        # Young lot, big gain, no near-threshold, exceeds Freigrenze
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("1.0"),
            purchase_price_usd=Decimal("50000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=50),
        )
        agent = TaxAgent(ledger=ledger)
        result = agent.evaluate_sell(
            qty_btc=Decimal("1.0"),
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.VETO
        assert "locked" in result.reason.lower()

    def test_empty_ledger_veto(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
        )
        assert result.decision == TaxVetoDecision.VETO


class TestSellableRecommendations:
    def test_full_sell_levels_when_mostly_free(self) -> None:
        ledger = _make_ledger_with_lots(400, 400, 400, 400, 400)
        agent = TaxAgent(ledger=ledger)
        assert agent.recommended_sell_levels() == -1

    def test_moderate_sell_levels(self) -> None:
        ledger = _make_ledger_with_lots(400, 400, 400, 10, 10)
        agent = TaxAgent(ledger=ledger)
        assert agent.recommended_sell_levels() == 3

    def test_single_sell_level(self) -> None:
        ledger = _make_ledger_with_lots(400, 10, 10, 10)
        agent = TaxAgent(ledger=ledger)
        assert agent.recommended_sell_levels() == 1

    def test_buy_only_when_all_locked(self) -> None:
        ledger = _make_ledger_with_lots(10, 10, 10)
        agent = TaxAgent(ledger=ledger)
        assert agent.recommended_sell_levels() == 0


class TestTaxLockStatus:
    def test_is_locked_with_young_lots(self) -> None:
        ledger = _make_ledger_with_lots(10)
        agent = TaxAgent(ledger=ledger)
        assert agent.is_tax_locked() is True

    def test_not_locked_with_free_lots(self) -> None:
        ledger = _make_ledger_with_lots(400)
        agent = TaxAgent(ledger=ledger)
        assert agent.is_tax_locked() is False

    def test_not_locked_when_empty(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger)
        assert agent.is_tax_locked() is False

    def test_days_until_unlock(self) -> None:
        ledger = _make_ledger_with_lots(360)  # 5 days until free
        agent = TaxAgent(ledger=ledger)
        days = agent.days_until_unlock()
        assert days is not None
        assert days == 5
