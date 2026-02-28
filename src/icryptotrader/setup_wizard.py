"""Setup Wizard — interactive first-run configuration generator.

Guides the user through API key setup, balance-based defaults,
and safe grid sizing. Outputs a config/default.toml file.

Usage:
    python -m icryptotrader.setup_wizard
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _ask(prompt: str, default: str = "") -> str:
    """Ask user for input with an optional default."""
    if default:
        raw = input(f"  {prompt} [{default}]: ").strip()
        return raw if raw else default
    return input(f"  {prompt}: ").strip()


def _ask_decimal(prompt: str, default: str = "0") -> Decimal:
    """Ask for a Decimal value."""
    while True:
        raw = _ask(prompt, default)
        try:
            return Decimal(raw)
        except InvalidOperation:
            print("    Ungueltige Zahl, bitte erneut eingeben.")


def _ask_bool(prompt: str, default: bool = False) -> bool:
    """Ask for a yes/no value."""
    d = "ja" if default else "nein"
    raw = _ask(f"{prompt} (ja/nein)", d).lower()
    return raw in ("ja", "j", "yes", "y", "true", "1")


def run_wizard() -> str:
    """Run the interactive setup wizard. Returns TOML content."""
    print()
    print("=" * 60)
    print("  iCryptoTrader — Ersteinrichtung")
    print("=" * 60)
    print()

    # Step 1: Kraken API
    print("[1/6] Kraken API-Zugangsdaten")
    print("  (Erstelle einen API-Key unter https://www.kraken.com/u/security/api)")
    print("  Benoetigte Berechtigungen: Query, Trade, WebSocket")
    print()
    api_key = _ask("API Key", "")
    api_secret = _ask("API Secret", "")

    # Step 2: Starting balance
    print()
    print("[2/6] Startkapital")
    usd_balance = _ask_decimal("USD-Balance auf Kraken", "5000")
    _ask_decimal("BTC-Balance auf Kraken", "0")  # Shown for user reference

    # Step 3: Compute safe defaults
    print()
    print("[3/6] Grid-Konfiguration")

    # Suggest order size: ~10% of portfolio
    suggested_size = max(Decimal("100"), usd_balance / 10)
    suggested_size = (suggested_size // 50) * 50  # Round to nearest 50
    order_size = _ask_decimal("Order-Groesse (USD)", str(suggested_size))

    # Suggest levels based on balance
    max_levels = int(usd_balance / order_size) if order_size > 0 else 5
    suggested_levels = min(max_levels, 5)
    levels = int(_ask(f"Grid-Level (max {max_levels})", str(suggested_levels)))
    levels = max(1, min(levels, max_levels))

    total_required = order_size * levels
    print(f"  -> Gesamt gebundenes Kapital: ${total_required:,.0f} "
          f"von ${usd_balance:,.0f} ({float(total_required/usd_balance)*100:.0f}%)")

    auto_compound = _ask_bool("Auto-Compounding aktivieren", False)

    # Step 4: Risk
    print()
    print("[4/6] Risiko-Management")
    max_dd = float(_ask("Max Drawdown (%) vor Pause", "15")) / 100
    emergency_dd = float(_ask("Emergency Drawdown (%) Notverkauf", "20")) / 100
    trailing = _ask_bool("Trailing Stop aktivieren", True)

    # Step 5: Tax
    print()
    print("[5/6] Steuer (Deutschland, §23 EStG)")
    harvest = _ask_bool("Tax-Loss Harvesting aktivieren", False)

    # Step 6: Telegram
    print()
    print("[6/6] Telegram Bot (optional)")
    tg_enabled = _ask_bool("Telegram-Bot aktivieren", False)
    tg_token = ""
    tg_chat = ""
    if tg_enabled:
        tg_token = _ask("Bot Token (von @BotFather)")
        tg_chat = _ask("Chat ID")

    # Generate TOML
    toml = _generate_toml(
        api_key=api_key,
        api_secret=api_secret,
        usd_balance=usd_balance,
        order_size=order_size,
        levels=levels,
        auto_compound=auto_compound,
        max_dd=max_dd,
        emergency_dd=emergency_dd,
        trailing=trailing,
        harvest=harvest,
        tg_enabled=tg_enabled,
        tg_token=tg_token,
        tg_chat=tg_chat,
    )

    print()
    print("=" * 60)
    print("  Konfiguration generiert!")
    print("=" * 60)
    print()

    # Save
    config_dir = Path("config")
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "default.toml"

    if config_path.exists():
        overwrite = _ask_bool(
            f"  {config_path} existiert bereits. Ueberschreiben",
            False,
        )
        if not overwrite:
            print("  Abgebrochen. Konfiguration nicht gespeichert.")
            return toml

    config_path.write_text(toml, encoding="utf-8")
    print(f"  Gespeichert unter: {config_path}")
    print()
    print("  Starte den Bot mit:")
    print("    python -m icryptotrader")
    print()

    return toml


def _generate_toml(
    *,
    api_key: str,
    api_secret: str,
    usd_balance: Decimal,
    order_size: Decimal,
    levels: int,
    auto_compound: bool,
    max_dd: float,
    emergency_dd: float,
    trailing: bool,
    harvest: bool,
    tg_enabled: bool,
    tg_token: str,
    tg_chat: str,
) -> str:
    """Generate a TOML configuration string."""
    return f'''pair = "XBT/USD"
log_level = "INFO"
data_dir = "data"
ledger_path = "data/fifo_ledger.json"

[kraken]
api_key = "{api_key}"
api_secret = "{api_secret}"
ws_public_url = "wss://ws.kraken.com/v2"
ws_private_url = "wss://ws-auth.kraken.com/v2"
rest_url = "https://api.kraken.com"

[grid]
levels = {levels}
order_size_usd = "{order_size}"
min_spacing_bps = "20"
post_only = true
auto_compound = {str(auto_compound).lower()}
compound_base_usd = "{usd_balance}"

[risk]
max_portfolio_drawdown_pct = {max_dd}
emergency_drawdown_pct = {emergency_dd}
price_velocity_freeze_pct = 0.03
price_velocity_window_sec = 60
price_velocity_cooldown_sec = 30
trailing_stop_enabled = {str(trailing).lower()}
trailing_stop_tighten_pct = 0.02

[tax]
holding_period_days = 365
near_threshold_days = 330
annual_exemption_eur = "1000"
emergency_dd_override_pct = {emergency_dd}
harvest_enabled = {str(harvest).lower()}
harvest_min_loss_eur = "50"
harvest_max_per_day = 3
harvest_target_net_eur = "800"

[regime.range_bound]
btc_target_pct = 0.50
btc_max_pct = 0.60
btc_min_pct = 0.40
grid_levels = {levels}
signal_enabled = true
order_size_scale = 1.0

[regime.trending_up]
btc_target_pct = 0.70
btc_max_pct = 0.80
btc_min_pct = 0.55
grid_levels = {max(1, levels - 2)}
signal_enabled = true
order_size_scale = 0.75

[regime.trending_down]
btc_target_pct = 0.30
btc_max_pct = 0.40
btc_min_pct = 0.15
grid_levels = {max(1, levels - 2)}
signal_enabled = true
order_size_scale = 0.75

[regime.chaos]
btc_target_pct = 0.00
btc_max_pct = 0.05
btc_min_pct = 0.00
grid_levels = 0
signal_enabled = false
order_size_scale = 0.5

[ws]
cancel_after_timeout_sec = 60
heartbeat_interval_sec = 20
reconnect_max_backoff_sec = 30
pending_ack_timeout_ms = 500

[rate_limit]
max_counter = 180
decay_rate = 3.75
headroom_pct = 0.80

[bollinger]
enabled = true
window = 20
multiplier = 2.0
spacing_scale = 0.5
min_spacing_bps = "15"
max_spacing_bps = "200"
atr_enabled = true
atr_window = 14
atr_weight = 0.3

[telegram]
enabled = {str(tg_enabled).lower()}
bot_token = "{tg_token}"
chat_id = "{tg_chat}"

[ai_signal]
enabled = false
provider = "gemini"
api_key = ""
model = "gemini-2.0-flash"
temperature = 0.2
max_tokens = 512
cooldown_sec = 300
weight = 0.3
timeout_sec = 10

[metrics]
enabled = true
port = 9090
prefix = "icryptotrader"
'''


if __name__ == "__main__":
    try:
        run_wizard()
    except KeyboardInterrupt:
        print("\n  Abgebrochen.")
        sys.exit(1)
