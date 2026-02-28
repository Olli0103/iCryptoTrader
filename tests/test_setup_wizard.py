"""Tests for the Setup Wizard TOML generation."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.setup_wizard import _generate_toml


class TestGenerateToml:
    def test_basic_toml_generation(self) -> None:
        """Generated TOML should contain all sections."""
        toml = _generate_toml(
            api_key="test_key",
            api_secret="test_secret",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert 'pair = "XBT/USD"' in toml
        assert "[kraken]" in toml
        assert "[grid]" in toml
        assert "[risk]" in toml
        assert "[tax]" in toml
        assert "[telegram]" in toml
        assert "[bollinger]" in toml
        assert "[metrics]" in toml

    def test_api_keys_inserted(self) -> None:
        toml = _generate_toml(
            api_key="MY_API_KEY",
            api_secret="MY_SECRET",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert 'api_key = "MY_API_KEY"' in toml
        assert 'api_secret = "MY_SECRET"' in toml

    def test_grid_config_values(self) -> None:
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("10000"),
            order_size=Decimal("1000"),
            levels=3,
            auto_compound=True,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert "levels = 3" in toml
        assert 'order_size_usd = "1000"' in toml
        assert "auto_compound = true" in toml
        assert 'compound_base_usd = "10000"' in toml

    def test_risk_config_values(self) -> None:
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.10,
            emergency_dd=0.18,
            trailing=False,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert "max_portfolio_drawdown_pct = 0.1" in toml
        assert "emergency_drawdown_pct = 0.18" in toml
        assert "trailing_stop_enabled = false" in toml

    def test_tax_config_values(self) -> None:
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=True,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert "harvest_enabled = true" in toml
        assert "holding_period_days = 365" in toml

    def test_telegram_disabled(self) -> None:
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        assert "enabled = false" in toml

    def test_telegram_enabled(self) -> None:
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=True,
            tg_token="123:ABC",
            tg_chat="456",
        )
        assert 'bot_token = "123:ABC"' in toml
        assert 'chat_id = "456"' in toml

    def test_regime_levels_scale_with_grid(self) -> None:
        """Trending regimes should have fewer levels than grid setting."""
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        # range_bound should have full levels
        assert "grid_levels = 5" in toml
        # trending should have levels - 2 = 3
        assert "grid_levels = 3" in toml
        # chaos should have 0
        assert "grid_levels = 0" in toml

    def test_toml_is_valid(self) -> None:
        """Generated TOML should be parseable."""
        import tomllib

        toml = _generate_toml(
            api_key="test",
            api_secret="test",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=5,
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        # Should parse without error
        parsed = tomllib.loads(toml)
        assert parsed["pair"] == "XBT/USD"
        assert parsed["grid"]["levels"] == 5
        assert parsed["risk"]["max_portfolio_drawdown_pct"] == 0.15

    def test_minimum_grid_levels(self) -> None:
        """Regime levels should never go below 1 (except chaos=0)."""
        toml = _generate_toml(
            api_key="",
            api_secret="",
            usd_balance=Decimal("5000"),
            order_size=Decimal("500"),
            levels=2,  # levels - 2 = 0, but should be clamped to 1
            auto_compound=False,
            max_dd=0.15,
            emergency_dd=0.20,
            trailing=True,
            harvest=False,
            tg_enabled=False,
            tg_token="",
            tg_chat="",
        )
        import tomllib

        parsed = tomllib.loads(toml)
        assert parsed["regime"]["trending_up"]["grid_levels"] >= 1
        assert parsed["regime"]["trending_down"]["grid_levels"] >= 1
