"""Tests for __main__.py entry point and component wiring."""

from __future__ import annotations

from unittest.mock import patch

from icryptotrader.__main__ import _build_components, main
from icryptotrader.config import Config


class TestBuildComponents:
    def test_default_config_builds_all(self) -> None:
        cfg = Config()
        components = _build_components(cfg)
        assert "strategy_loop" in components
        assert "ws_private" in components
        assert "ws_public" in components
        assert "order_manager" in components
        assert "ledger" in components
        assert "risk_manager" in components
        assert "inventory" in components

    def test_metrics_disabled_by_default(self) -> None:
        cfg = Config()
        components = _build_components(cfg)
        assert components["metrics_server"] is None
        assert components["metrics_registry"] is None

    def test_metrics_enabled(self) -> None:
        cfg = Config()
        cfg.metrics.enabled = True
        components = _build_components(cfg)
        assert components["metrics_server"] is not None
        assert components["metrics_registry"] is not None

    def test_telegram_disabled_by_default(self) -> None:
        cfg = Config()
        components = _build_components(cfg)
        assert components["telegram_bot"] is None

    def test_telegram_enabled(self) -> None:
        cfg = Config()
        cfg.telegram.enabled = True
        cfg.telegram.bot_token = "test:token"
        cfg.telegram.chat_id = "123"
        components = _build_components(cfg)
        assert components["telegram_bot"] is not None

    def test_ai_signal_disabled_by_default(self) -> None:
        cfg = Config()
        components = _build_components(cfg)
        assert components["ai_signal"] is None

    def test_ai_signal_enabled(self) -> None:
        cfg = Config()
        cfg.ai_signal.enabled = True
        cfg.ai_signal.api_key = "test-key"
        components = _build_components(cfg)
        assert components["ai_signal"] is not None

    def test_bollinger_enabled_by_default(self) -> None:
        cfg = Config()
        components = _build_components(cfg)
        assert components["bollinger"] is not None

    def test_bollinger_disabled(self) -> None:
        cfg = Config()
        cfg.bollinger.enabled = False
        components = _build_components(cfg)
        assert components["bollinger"] is None

    def test_risk_manager_trailing_stop_config(self) -> None:
        cfg = Config()
        cfg.risk.trailing_stop_enabled = False
        cfg.risk.trailing_stop_tighten_pct = 0.05
        components = _build_components(cfg)
        rm = components["risk_manager"]
        assert rm._trailing_enabled is False
        assert rm._trailing_tighten_pct == 0.05

    def test_persistence_backend_passed(self) -> None:
        cfg = Config()
        cfg.persistence_backend = "sqlite"
        components = _build_components(cfg)
        sl = components["strategy_loop"]
        assert sl._persistence_backend == "sqlite"


class TestCLI:
    def test_setup_subcommand(self) -> None:
        """Verify setup subcommand calls the wizard."""
        with (
            patch("sys.argv", ["icryptotrader", "setup"]),
            patch("icryptotrader.setup_wizard.run_wizard") as mock_wizard,
        ):
            main()
            mock_wizard.assert_called_once()
