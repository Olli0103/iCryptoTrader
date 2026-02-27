"""Tests for fee model service."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.fee.fee_model import KRAKEN_SPOT_TIERS, FeeModel


class TestTierResolution:
    def test_base_tier_at_zero_volume(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        assert fm.maker_fee_bps() == Decimal("25")
        assert fm.taker_fee_bps() == Decimal("40")

    def test_10k_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=10_000)
        assert fm.maker_fee_bps() == Decimal("20")

    def test_100k_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=100_000)
        assert fm.maker_fee_bps() == Decimal("12")

    def test_1m_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=1_000_000)
        assert fm.maker_fee_bps() == Decimal("4")

    def test_10m_tier_zero_maker(self) -> None:
        fm = FeeModel(volume_30d_usd=10_000_000)
        assert fm.maker_fee_bps() == Decimal("0")

    def test_volume_between_tiers_uses_lower(self) -> None:
        fm = FeeModel(volume_30d_usd=75_000)  # Between 50k and 100k
        assert fm.maker_fee_bps() == Decimal("14")  # 50k tier

    def test_update_volume_changes_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        assert fm.maker_fee_bps() == Decimal("25")
        fm.update_volume(500_000)
        assert fm.maker_fee_bps() == Decimal("6")


class TestRoundTripCost:
    def test_rt_cost_maker_both(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        assert fm.rt_cost_bps(maker_both_sides=True) == Decimal("50")

    def test_rt_cost_mixed(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        assert fm.rt_cost_bps(maker_both_sides=False) == Decimal("65")

    def test_rt_cost_at_10m(self) -> None:
        fm = FeeModel(volume_30d_usd=10_000_000)
        assert fm.rt_cost_bps(maker_both_sides=True) == Decimal("0")


class TestNetEdge:
    def test_positive_edge_at_base_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        edge = fm.expected_net_edge_bps(
            grid_spacing_bps=Decimal("80"),
            adverse_selection_bps=Decimal("10"),
        )
        assert edge == Decimal("20")  # 80 - 50 - 10

    def test_negative_edge_too_tight(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        edge = fm.expected_net_edge_bps(
            grid_spacing_bps=Decimal("40"),
            adverse_selection_bps=Decimal("10"),
        )
        assert edge == Decimal("-20")  # 40 - 50 - 10

    def test_edge_improves_with_tier(self) -> None:
        fm_base = FeeModel(volume_30d_usd=0)
        fm_pro = FeeModel(volume_30d_usd=1_000_000)
        spacing = Decimal("40")
        edge_base = fm_base.expected_net_edge_bps(spacing)
        edge_pro = fm_pro.expected_net_edge_bps(spacing)
        assert edge_pro > edge_base
        assert edge_pro == Decimal("22")  # 40 - 8 - 10


class TestMinProfitableSpacing:
    def test_base_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        min_spacing = fm.min_profitable_spacing_bps()
        assert min_spacing == Decimal("65")  # 50 + 10 + 5

    def test_1m_tier(self) -> None:
        fm = FeeModel(volume_30d_usd=1_000_000)
        min_spacing = fm.min_profitable_spacing_bps()
        assert min_spacing == Decimal("23")  # 8 + 10 + 5


class TestFeeForNotional:
    def test_maker_fee_on_500(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        fee = fm.fee_for_notional(Decimal("500"), is_maker=True)
        assert fee == Decimal("1.25")  # 500 * 25 / 10000

    def test_taker_fee_on_500(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        fee = fm.fee_for_notional(Decimal("500"), is_maker=False)
        assert fee == Decimal("2.00")  # 500 * 40 / 10000


class TestNextTier:
    def test_volume_to_next_at_zero(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        assert fm.volume_to_next_tier() == 10_000

    def test_volume_to_next_at_max(self) -> None:
        fm = FeeModel(volume_30d_usd=10_000_000)
        assert fm.volume_to_next_tier() is None

    def test_next_tier_at_50k(self) -> None:
        fm = FeeModel(volume_30d_usd=50_000)
        nxt = fm.next_tier()
        assert nxt is not None
        assert nxt.min_volume_usd == 100_000


class TestTierTableIntegrity:
    def test_tiers_sorted_ascending(self) -> None:
        for i in range(1, len(KRAKEN_SPOT_TIERS)):
            assert KRAKEN_SPOT_TIERS[i].min_volume_usd > KRAKEN_SPOT_TIERS[i - 1].min_volume_usd

    def test_maker_fees_decrease(self) -> None:
        for i in range(1, len(KRAKEN_SPOT_TIERS)):
            assert KRAKEN_SPOT_TIERS[i].maker_bps <= KRAKEN_SPOT_TIERS[i - 1].maker_bps

    def test_taker_fees_decrease(self) -> None:
        for i in range(1, len(KRAKEN_SPOT_TIERS)):
            assert KRAKEN_SPOT_TIERS[i].taker_bps <= KRAKEN_SPOT_TIERS[i - 1].taker_bps

    def test_nine_tiers(self) -> None:
        assert len(KRAKEN_SPOT_TIERS) == 9
