"""Tests for the Tax Agent veto mechanism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent
from icryptotrader.types import HarvestRecommendation, TaxVetoDecision


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


EUR_USD = Decimal("1.08")


def _make_harvest_ledger() -> FIFOLedger:
    """Create a ledger with YTD gains and underwater lots for harvest tests.

    Structure (FIFO order):
    1. Old profitable lot (250 days, $60k, 0.05 BTC) — sold partially to generate gains
    2. Underwater lot (100 days, $90k, 0.01 BTC) — at $75k, loss ≈ -EUR 139
    3. Underwater lot (50 days, $85k, 0.01 BTC) — at $75k, loss ≈ -EUR 93
    """
    now = datetime.now(UTC)
    ledger = FIFOLedger()

    # Profitable lot first (oldest, consumed first by FIFO sell)
    ledger.add_lot(
        quantity_btc=Decimal("0.05"), purchase_price_usd=Decimal("60000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=now - timedelta(days=250),
    )
    # Underwater lot 1: bought at $90k
    ledger.add_lot(
        quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=now - timedelta(days=100),
    )
    # Underwater lot 2: bought at $85k
    ledger.add_lot(
        quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("85000"),
        purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        purchase_timestamp=now - timedelta(days=50),
    )

    # Sell from the profitable lot to generate YTD gains (~EUR 231 gain)
    ledger.sell_fifo(
        quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
        sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
    )

    return ledger


class TestHarvestRecommendation:
    def test_recommends_when_ytd_gains_positive(self) -> None:
        """Should recommend harvesting when there are YTD taxable gains."""
        ledger = _make_harvest_ledger()
        agent = TaxAgent(ledger=ledger)

        ytd = ledger.taxable_gain_ytd()
        assert ytd > 0  # Precondition: we have gains to offset

        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("70000"),  # Price drop creates losses
            eur_usd_rate=EUR_USD,
        )
        assert len(recs) > 0
        for rec in recs:
            assert isinstance(rec, HarvestRecommendation)
            assert rec.estimated_loss_eur < 0
            assert rec.qty_btc > 0

    def test_no_recommendations_when_no_ytd_gains(self) -> None:
        """Should not recommend harvesting when YTD gains are zero or negative."""
        ledger = FIFOLedger()
        now = datetime.now(UTC)
        # Only add underwater lots, no realized gains
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=100),
        )
        agent = TaxAgent(ledger=ledger)

        assert ledger.taxable_gain_ytd() == Decimal("0")
        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("80000"),
            eur_usd_rate=EUR_USD,
        )
        assert recs == []

    def test_respects_max_harvests(self) -> None:
        """Should not recommend more than max_harvests."""
        ledger = _make_harvest_ledger()
        agent = TaxAgent(ledger=ledger)

        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("70000"),
            eur_usd_rate=EUR_USD,
            max_harvests=1,
        )
        assert len(recs) <= 1

    def test_skips_trivial_losses(self) -> None:
        """Losses below min_loss_eur should be skipped."""
        now = datetime.now(UTC)
        ledger = FIFOLedger()

        # Tiny loss lot: bought at $80100 vs current $80000 = ~$1 loss
        ledger.add_lot(
            quantity_btc=Decimal("0.001"), purchase_price_usd=Decimal("80100"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=50),
        )
        # Create YTD gains
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("70000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=200),
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.001"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert ledger.taxable_gain_ytd() > 0

        agent = TaxAgent(ledger=ledger)
        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("80000"),
            eur_usd_rate=EUR_USD,
            min_loss_eur=Decimal("50"),  # Higher than the tiny loss
        )
        assert recs == []

    def test_excludes_near_threshold_lots(self) -> None:
        """Lots near maturity should not be recommended for harvest."""
        now = datetime.now(UTC)
        ledger = FIFOLedger()

        # Profitable lot first (oldest, consumed first by FIFO sell)
        ledger.add_lot(
            quantity_btc=Decimal("0.05"), purchase_price_usd=Decimal("60000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=350),
        )
        # Near-threshold losing lot (340 days, near maturity)
        ledger.add_lot(
            quantity_btc=Decimal("0.01"), purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
            purchase_timestamp=now - timedelta(days=340),
        )

        # Create YTD gains by selling from the profitable lot
        ledger.sell_fifo(
            quantity_btc=Decimal("0.01"), sale_price_usd=Decimal("85000"),
            sale_fee_usd=Decimal("0"), eur_usd_rate=EUR_USD,
        )
        assert ledger.taxable_gain_ytd() > 0

        agent = TaxAgent(ledger=ledger, near_threshold_days=330)
        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("80000"),
            eur_usd_rate=EUR_USD,
        )
        # The 340-day lot should be excluded (>= 330 near threshold)
        assert recs == []

    def test_stops_when_target_reached(self) -> None:
        """Should stop harvesting once target net is reached."""
        ledger = _make_harvest_ledger()
        agent = TaxAgent(ledger=ledger)
        ytd = ledger.taxable_gain_ytd()

        # Set a generous target so we stop after first harvest
        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("70000"),
            eur_usd_rate=EUR_USD,
            target_net_eur=ytd,  # Target == current gains means stop immediately
        )
        # With target == ytd, any loss brings us below target, so should get 1 rec
        assert len(recs) <= 1

    def test_reason_field(self) -> None:
        """Reason should reflect whether offsetting gains or optimizing Freigrenze."""
        ledger = _make_harvest_ledger()
        agent = TaxAgent(ledger=ledger, annual_exemption_eur=Decimal("1000"))

        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("70000"),
            eur_usd_rate=EUR_USD,
        )
        if recs:
            ytd = ledger.taxable_gain_ytd()
            if ytd > Decimal("1000"):
                assert recs[0].reason == "offset_gains"
            else:
                assert recs[0].reason == "freigrenze_optimization"

    def test_harvest_disabled_by_default_in_config(self) -> None:
        """Verify that TaxConfig defaults have harvest_enabled=False."""
        from icryptotrader.config import TaxConfig
        cfg = TaxConfig()
        assert cfg.harvest_enabled is False
        assert cfg.harvest_min_loss_eur == Decimal("50")
        assert cfg.harvest_max_per_day == 3
        assert cfg.harvest_target_net_eur == Decimal("800")
