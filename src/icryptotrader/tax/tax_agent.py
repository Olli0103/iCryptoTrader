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
        blow_through_mode: bool = False,
        vault_lock_priority: bool = True,
        wash_sale_cooldown_hours: int = 24,
    ) -> None:
        self._ledger = ledger
        self._annual_exemption_eur = annual_exemption_eur
        self._near_threshold_days = near_threshold_days
        self._emergency_dd_pct = emergency_dd_pct
        self._blow_through_mode = blow_through_mode
        self._vault_lock_priority = vault_lock_priority
        self._wash_sale_cooldown_hours = wash_sale_cooldown_hours
        # Buy-side cooldown: blocks all buys after a harvest sell to prevent
        # immediately recreating economic exposure (§42 AO compliance).
        # Stored as a Unix timestamp (time.time()) until which buys are blocked.
        self._buy_cooldown_until: float = 0.0
        # Also track per-harvest for logging/auditing
        self._harvest_timestamps: dict[str, float] = {}

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
        #    In blow-through mode: skip this gate entirely, allow all trades
        ytd_gain = self._ledger.taxable_gain_ytd()
        estimated_gain = self._estimate_gain(qty_btc, current_price_usd, eur_usd_rate)

        if self._blow_through_mode:
            logger.info(
                "Tax ALLOW (blow-through): YTD EUR %.2f + est EUR %.2f "
                "(Freigrenze gating disabled)",
                ytd_gain, estimated_gain,
            )
            return SellEvaluation(
                decision=TaxVetoDecision.ALLOW,
                allowed_qty_btc=qty_btc,
                reason="Blow-through mode: Freigrenze gating disabled",
                taxable_gain_if_sold_eur=estimated_gain,
                days_until_next_free=days_until,
            )

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

        In blow-through mode: always return -1 (all levels active).

        Ratio >= 0.8: full sell-side (return -1 for "all")
        Ratio 0.5-0.8: 60% of levels
        Ratio 0.2-0.5: 1 level
        Ratio < 0.2: 0 levels (buy-only)
        """
        if self._blow_through_mode:
            return -1  # All levels active in blow-through mode

        ratio = self.sellable_ratio()
        if ratio >= 0.8:
            return -1  # All levels
        if ratio >= 0.5:
            return 3  # ~60% of 5
        if ratio >= 0.2:
            return 1
        return 0  # Buy-only

    def is_tax_locked(self) -> bool:
        """True if no BTC can be sold tax-free and we're not in Freigrenze.

        In blow-through mode: never locked (always allow trading).
        """
        if self._blow_through_mode:
            return False
        return (
            self._ledger.tax_free_btc() == 0
            and self._ledger.total_btc() > 0
        )

    def vault_lot_btc(self) -> Decimal:
        """BTC held >365 days — tax-free 'vault' lots."""
        return self._ledger.tax_free_btc()

    def should_prioritize_vault_sell(self) -> bool:
        """True if we have vault lots and vault_lock_priority is enabled.

        The strategy should prioritize selling these lots on rallies
        because their profits are 100% tax-free.
        """
        return self._vault_lock_priority and self._ledger.tax_free_btc() > 0

    def days_until_unlock(self) -> int | None:
        """Days until the next lot becomes tax-free."""
        return self._ledger.days_until_next_free()

    def record_harvest(self, lot_id: str) -> None:
        """Record that a lot was harvested. Starts the buy-side cooldown.

        After a tax-loss harvest sell, the grid must NOT buy back the same
        asset for ``wash_sale_cooldown_hours`` (default 24h). This prevents
        the Finanzamt from nullifying the loss under §42 AO
        (Gestaltungsmissbrauch): selling at a loss and immediately rebuying
        to maintain economic exposure while claiming the tax deduction.

        The cooldown blocks ALL buys (not just the specific lot), because
        the lot has been sold and no longer exists in the ledger.
        """
        import time as _time
        now = _time.time()
        self._harvest_timestamps[lot_id] = now
        cooldown_sec = self._wash_sale_cooldown_hours * 3600.0
        self._buy_cooldown_until = max(self._buy_cooldown_until, now + cooldown_sec)
        logger.info(
            "Wash sale buy-cooldown set: buys blocked for %dh (lot %s harvested)",
            self._wash_sale_cooldown_hours, lot_id[:8],
        )

    def is_buy_blocked_by_wash_sale(self) -> bool:
        """True if buys are currently blocked due to a recent harvest sell.

        The strategy loop should check this before allowing any buy orders.
        This is the PRIMARY wash sale guard: it prevents the grid from
        immediately recreating the position that was sold for a tax loss.
        """
        import time as _time
        return _time.time() < self._buy_cooldown_until

    def is_wash_sale_safe(self, lot_id: str) -> bool:
        """Check if enough time has passed since last harvest for this lot.

        Used internally by ``recommend_loss_harvest()`` to avoid
        re-recommending a harvest for the same lot_id if it somehow
        reappears (e.g., partial fill edge case).
        """
        import time as _time
        last_harvest = self._harvest_timestamps.get(lot_id)
        if last_harvest is None:
            return True
        elapsed_hours = (_time.time() - last_harvest) / 3600.0
        return elapsed_hours >= self._wash_sale_cooldown_hours

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
        6. Skip lots still in wash sale cooldown (§42 AO compliance)
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

            # Skip lots still in wash sale cooldown
            if not self.is_wash_sale_safe(lot.lot_id):
                logger.debug(
                    "Skipping lot %s: wash sale cooldown active", lot.lot_id[:8],
                )
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
                       else "loss_offset",
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
