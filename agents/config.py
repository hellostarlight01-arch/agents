"""
Centralized configuration for the Polymarket trading bot.
"""

import os
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class RiskConfig:
    """Risk management parameters."""

    max_position_size_usdc: float = 50.0
    max_total_exposure_usdc: float = 200.0
    max_trades_per_day: int = 10
    max_loss_per_trade_usdc: float = 25.0
    max_daily_loss_usdc: float = 100.0
    min_confidence_threshold: float = 0.6
    position_size_pct: float = 0.05  # % of balance per trade
    stop_loss_pct: float = 0.15  # 15% stop loss
    take_profit_pct: float = 0.30  # 30% take profit


@dataclass
class BotConfig:
    """Main bot configuration."""

    # Mode
    dry_run: bool = True  # Paper trading by default
    strategy: str = "one_best_trade"

    # Timing
    trade_interval_seconds: int = 300  # 5 minutes between trade checks
    monitor_interval_seconds: int = 30  # 30 seconds between monitor updates
    market_refresh_seconds: int = 60  # 1 minute market data refresh

    # LLM
    llm_model: str = "llama-3.3-70b-versatile"

    # Risk
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = "bot.log"

    # Data directories
    data_dir: str = "./data"
    trades_file: str = "./data/trades.json"
    positions_file: str = "./data/positions.json"


def get_config() -> BotConfig:
    """Load configuration from environment variables with defaults."""
    config = BotConfig()

    config.dry_run = os.getenv("BOT_DRY_RUN", "true").lower() == "true"
    config.strategy = os.getenv("BOT_STRATEGY", "one_best_trade")
    config.trade_interval_seconds = int(os.getenv("BOT_TRADE_INTERVAL", "300"))
    config.monitor_interval_seconds = int(os.getenv("BOT_MONITOR_INTERVAL", "30"))
    config.llm_model = os.getenv("BOT_LLM_MODEL", "llama-3.3-70b-versatile")
    config.log_level = os.getenv("BOT_LOG_LEVEL", "INFO")

    risk = config.risk
    risk.max_position_size_usdc = float(
        os.getenv("RISK_MAX_POSITION_SIZE", "50.0")
    )
    risk.max_total_exposure_usdc = float(
        os.getenv("RISK_MAX_TOTAL_EXPOSURE", "200.0")
    )
    risk.max_trades_per_day = int(os.getenv("RISK_MAX_TRADES_PER_DAY", "10"))
    risk.max_daily_loss_usdc = float(os.getenv("RISK_MAX_DAILY_LOSS", "100.0"))
    risk.min_confidence_threshold = float(
        os.getenv("RISK_MIN_CONFIDENCE", "0.6")
    )
    risk.position_size_pct = float(os.getenv("RISK_POSITION_SIZE_PCT", "0.05"))

    return config


def setup_logging(config: BotConfig) -> logging.Logger:
    """Configure structured logging for the bot."""
    logger = logging.getLogger("polybot")
    logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler
    if config.log_file:
        os.makedirs(os.path.dirname(config.log_file) if os.path.dirname(config.log_file) else ".", exist_ok=True)
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
