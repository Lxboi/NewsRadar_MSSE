"""Phase 1 tests: config files parse and encode the safety defaults the
rest of the system relies on. See docs/09_tests_plan.md."""
from pathlib import Path

REQUIRED_CONFIG_FILES = {
    "settings",
    "assets",
    "sources",
    "strategy",
    "telegram",
}


def test_all_expected_config_files_exist(config_dir: Path):
    found = {p.stem for p in config_dir.glob("*.yaml")}
    missing = REQUIRED_CONFIG_FILES - found
    assert not missing, f"missing config files: {missing}"


def test_default_trading_mode_is_research(all_configs: dict):
    assert all_configs["settings"]["trading_mode"]["default"] == "RESEARCH", (
        "the system must always default to RESEARCH mode (section 5)"
    )


def test_initial_asset_universe_is_btc_eth_sol(all_configs: dict):
    enabled = [a["symbol"] for a in all_configs["assets"]["assets"] if a.get("enabled")]
    assert set(enabled) == {"BTC", "ETH", "SOL"}, (
        "section 6: start with BTC/ETH/SOL only, not the full future universe"
    )


def test_keywords_are_configurable_not_hardcoded(all_configs: dict):
    keywords = all_configs["sources"]["keywords"]
    assert isinstance(keywords, list) and len(keywords) > 0
    assert "Bitcoin" in keywords and "SEC" in keywords


def test_telegram_defaults_to_sniper_mode(all_configs: dict):
    assert all_configs["telegram"]["mode"] == "SNIPER", (
        "section 69: SNIPER must be the default Telegram mode"
    )


def test_risk_limits_are_all_present(all_configs: dict):
    risk = all_configs["strategy"]["risk"]
    required = {
        "max_risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_position_size_pct_equity",
        "max_consecutive_losses",
        "max_open_positions",
        "max_slippage_bps",
        "max_spread_bps",
        "max_latency_ms",
    }
    missing = required - set(risk.keys())
    assert not missing, f"missing risk limits (section 66): {missing}"


def test_env_example_defines_every_required_secret():
    root = Path(__file__).resolve().parent.parent
    env_example = (root / ".env.example").read_text()
    for key in [
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "PAPER_TRADING",
        "LIVE_TRADING",
    ]:
        assert key in env_example, f"{key} missing from .env.example (section 64)"
