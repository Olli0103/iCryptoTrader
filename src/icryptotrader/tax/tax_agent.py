"""Tax Agent — veto mechanism for sell decisions under German §23 EStG.

Priority hierarchy: Tax > Risk > Alpha.

The Tax Agent evaluates every sell request and returns a TaxVetoDecision:
  - ALLOW: sell proceeds (tax-free lots available, or within Freigrenze)
  - ALLOW_PARTIAL: sell only the tax-free portion
  - VETO: block the sell (lots are tax-locked, near-threshold protection)
  - OVERRIDE_EMERGENCY: sell regardless (portfolio DD > emergency threshold)

Buys are never vetoed by the Tax Agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from icryptotrader.types import HarvestRecommendation, TaxVetoDecision

if TYPE_CHECKING:
    from icryptotrader.tax.fifo_ledger import FIFOLedger

logger = logging.getLogger(__name__)


@dataclass
class SellEvaluation:
    """Result of evaluating a sell request."""

    decision: TaxVetoDecision
    allowed_qty_btc: Decimal = Decimal("0")
    reason: str = ""
    taxable_gain_if_sold_eur: Decimal = Decimal("0")
    days_until_next_free: int | None = None


class TaxAgent:
    """Evaluates sell decisions against German §23 EStG constraints.

    Usage:
        agent = TaxAgent(ledger=fifo_ledger)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("85000"),
            portfolio_drawdown_pct=0.05,
        )
        if result.decision == TaxVetoDecision.ALLOW:
            execute_sell(result.allowed_qty_btc)
    """

    def __init__(
        self,
        ledger: FIFOLedger,
        annual_exemption_eur: Decimal = Decimal("1000"),
        near_threshold_days: int = 330,
        emergency_dd_pct: float = 0.20,
    ) -> None:
        self._ledger = ledger
        self._annual_exemption_eur = annual_exemption_eur
        self._near_threshold_days = near_threshold_days
        self._emergency_dd_pct = emergency_dd_pct

    def evaluate_sell(
        self,
        qty_btc: Decimal,
        current_price_usd: Decimal,
        eur_usd_rate: Decimal = Decimal("1.08"),
        portfolio_drawdown_pct: float = 0.0,
    ) -> SellEvaluation:
        """Evaluate whether a sell of qty_btc should be allowed.

        Priority order:
        1. Emergency override (DD > threshold): ALLOW regardless
        2. Tax-free lots available: ALLOW (sell these first under FIFO)
        3. Partial tax-free: ALLOW_PARTIAL (sell only free portion)
        4. Within annual Freigrenze: ALLOW
        5. Near-threshold lots (330-365 days): VETO
        6. All lots locked: VETO
        """
        days_until = self._ledger.days_until_next_free()

        # 1. Emergency override
        if portfolio_drawdown_pct >= self._emergency_dd_pct:
            logger.warning(
                "Tax OVERRIDE: portfolio DD %.1f%% >= %.1f%% emergency threshold",
                portfolio_drawdown_pct * 100, self._emergency_dd_pct * 100,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.OVERRIDE_EMERGENCY,
                allowed_qty_btc=qty_btc,
                reason=f"Emergency DD override ({portfolio_drawdown_pct:.1%})",
                days_until_next_free=days_until,
            )

        tax_free = self._ledger.tax_free_btc()
        total = self._ledger.total_btc()

        if total <= 0:
            return SellEvaluation(
                decision=TaxVetoDecision.VETO,
                reason="No BTC in ledger",
                days_until_next_free=days_until,
            )

        # 2. Full tax-free coverage
        if tax_free >= qty_btc:
            logger.info(
                "Tax ALLOW: %s BTC fully covered by tax-free lots (%s free)",
                qty_btc, tax_free,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.ALLOW,
                allowed_qty_btc=qty_btc,
                reason="Fully covered by tax-free lots",
                days_until_next_free=days_until,
            )

        # 3. Partial tax-free coverage
        if tax_free > 0:
            logger.info(
                "Tax ALLOW_PARTIAL: %s of %s BTC is tax-free",
                tax_free, qty_btc,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.ALLOW_PARTIAL,
                allowed_qty_btc=tax_free,
                reason=f"Only {tax_free} BTC tax-free of {qty_btc} requested",
                days_until_next_free=days_until,
            )

        # 4. Check Freigrenze (€1,000 annual exemption)
        ytd_gain = self._ledger.taxable_gain_ytd()
        estimated_gain = self._estimate_gain(qty_btc, current_price_usd, eur_usd_rate)

        if ytd_gain + estimated_gain < self._annual_exemption_eur:
            logger.info(
                "Tax ALLOW: within Freigrenze (YTD: EUR %.2f + est: EUR %.2f < EUR %s)",
                ytd_gain, estimated_gain, self._annual_exemption_eur,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.ALLOW,
                allowed_qty_btc=qty_btc,
                reason="Within annual Freigrenze",
                taxable_gain_if_sold_eur=estimated_gain,
                days_until_next_free=days_until,
            )

        # 5. Near-threshold protection (lots 330-365 days old)
        near_threshold = self._ledger.near_threshold_btc(self._near_threshold_days)
        if near_threshold > 0:
            logger.info(
                "Tax VETO: %s BTC near threshold (%d-%d days held), protecting",
                near_threshold, self._near_threshold_days, 365,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.VETO,
                reason=f"{near_threshold} BTC approaching tax-free ({days_until}d remaining)",
                days_until_next_free=days_until,
            )

        # 6. All lots tax-locked
        logger.info("Tax VETO: all %s BTC is tax-locked", total)
        return SellEvaluation(
            decision=TaxVetoDecision.VETO,
            reason="All BTC tax-locked (held < 365 days)",
            days_until_next_free=days_until,
        )

    def sellable_ratio(self) -> float:
        """Fraction of total BTC that can be sold tax-free."""
        return self._ledger.sellable_ratio()

    def recommended_sell_levels(self) -> int:
        """How many sell levels the grid should run based on sellable ratio.

        Ratio >= 0.8: full sell-side (return -1 for "all")
        Ratio 0.5-0.8: 60% of levels
        Ratio 0.2-0.5: 1 level
        Ratio < 0.2: 0 levels (buy-only)
        """
        ratio = self.sellable_ratio()
        if ratio >= 0.8:
            return -1  # All levels
        if ratio >= 0.5:
            return 3  # ~60% of 5
        if ratio >= 0.2:
            return 1
        return 0  # Buy-only

    def is_tax_locked(self) -> bool:
        """True if no BTC can be sold tax-free and we're not in Freigrenze."""
        return (
            self._ledger.tax_free_btc() == 0
            and self._ledger.total_btc() > 0
        )

    def days_until_unlock(self) -> int | None:
        """Days until the next lot becomes tax-free."""
        return self._ledger.days_until_next_free()

    def recommend_loss_harvest(
        self,
        current_price_usd: Decimal,
        eur_usd_rate: Decimal,
        max_harvests: int = 3,
        min_loss_eur: Decimal = Decimal("50"),
        target_net_eur: Decimal | None = None,
    ) -> list[HarvestRecommendation]:
        """Recommend lots to sell for tax-loss harvesting.

        Identifies underwater lots (unrealized loss) that could be sold to
        offset realized taxable gains, ideally keeping net taxable income
        below the annual Freigrenze (EUR 1,000).

        Rules:
        1. Only harvest if YTD taxable gains > 0 (no point if net negative)
        2. Never harvest lots within near_threshold_days of maturity
        3. Skip lots with losses below min_loss_eur (avoid dust)
        4. Cap at max_harvests per call
        5. Stop once enough losses are harvested to reach target_net_eur
        """
        ytd_gain = self._ledger.taxable_gain_ytd()
        if ytd_gain <= 0:
            return []

        target = target_net_eur if target_net_eur is not None else (
            self._annual_exemption_eur * Decimal("0.8")
        )

        underwater = self._ledger.underwater_lots(
            current_price_usd, eur_usd_rate, self._near_threshold_days,
        )

        recommendations: list[HarvestRecommendation] = []
        cumulative_loss = Decimal("0")

        for lot, estimated_loss in underwater:
            if len(recommendations) >= max_harvests:
                break

            # Skip trivial losses
            if abs(estimated_loss) < min_loss_eur:
                continue

            # Check if we still need to harvest more
            projected_net = ytd_gain + cumulative_loss + estimated_loss
            if projected_net < 0:
                # Would overshoot into net loss — skip or reduce
                continue

            recommendations.append(HarvestRecommendation(
                lot_id=lot.lot_id,
                qty_btc=lot.remaining_qty_btc,
                estimated_loss_eur=estimated_loss,
                current_price_usd=current_price_usd,
                cost_basis_per_btc_eur=lot.cost_basis_per_btc_eur,
                days_held=lot.days_held,
                reason="offset_gains" if ytd_gain > self._annual_exemption_eur
                       else "freigrenze_optimization",
            ))
            cumulative_loss += estimated_loss

            # Check if we've harvested enough to reach target
            if ytd_gain + cumulative_loss <= target:
                break

        if recommendations:
            total_loss = sum(r.estimated_loss_eur for r in recommendations)
            logger.info(
                "Tax-loss harvest: %d lots recommended, est. total loss EUR %.2f "
                "(YTD gain EUR %.2f → projected net EUR %.2f)",
                len(recommendations), total_loss, ytd_gain, ytd_gain + total_loss,
            )

        return recommendations

    def _estimate_gain(
        self,
        qty_btc: Decimal,
        current_price_usd: Decimal,
        eur_usd_rate: Decimal,
    ) -> Decimal:
        """Estimate EUR gain from selling qty_btc at current price.

        Uses FIFO order to compute approximate gain against cost basis.
        """
        remaining = qty_btc
        estimated_gain = Decimal("0")

        for lot in self._ledger.open_lots():
            if remaining <= 0:
                break
            sell_from = min(lot.remaining_qty_btc, remaining)
            proceeds_eur = (sell_from * current_price_usd) / eur_usd_rate
            cost_eur = sell_from * lot.cost_basis_per_btc_eur
            estimated_gain += proceeds_eur - cost_eur
            remaining -= sell_from

        return estimated_gain
