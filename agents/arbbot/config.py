import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class ArbBotConfig:
    # Coins to trade (15-min up/down markets)
    coins: list = field(default_factory=lambda: ["btc", "eth", "sol"])
    # Minimum price movement (%) to trigger a trade
    min_move_pct: float = 0.15
    # How many seconds of price history to track for movement detection
    lookback_seconds: int = 60
    # Max USD per trade
    max_trade_usd: float = 15.0
    # Max percentage of balance per trade
    max_trade_pct: float = 0.10
    # Daily loss limit
    max_daily_loss_usd: float = 50.0
    # Dry run mode
    dry_run: bool = True
    # Log file
    log_file: str = "arbbot_audit.log"
    # Minimum seconds into the 15-min window before trading (avoid start)
    min_window_seconds: int = 60
    # Maximum seconds into the 15-min window to trade (avoid end)
    max_window_seconds: int = 780  # 13 minutes

    @classmethod
    def from_env(cls) -> "ArbBotConfig":
        coins_raw = os.getenv("ARB_COINS", "btc,eth,sol")
        coins = [c.strip().lower() for c in coins_raw.split(",") if c.strip()]

        return cls(
            coins=coins,
            min_move_pct=float(os.getenv("ARB_MIN_MOVE_PCT", "0.15")),
            lookback_seconds=int(os.getenv("ARB_LOOKBACK_SECONDS", "60")),
            max_trade_usd=float(os.getenv("MAX_TRADE_USD", "15.0")),
            max_trade_pct=float(os.getenv("MAX_TRADE_PCT", "0.10")),
            max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "50.0")),
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            log_file=os.getenv("ARBBOT_LOG_FILE", "arbbot_audit.log"),
            min_window_seconds=int(os.getenv("ARB_MIN_WINDOW_SECONDS", "60")),
            max_window_seconds=int(os.getenv("ARB_MAX_WINDOW_SECONDS", "780")),
        )
