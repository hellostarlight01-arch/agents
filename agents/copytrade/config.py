import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class CopyTradeConfig:
    target_profile_url: str
    max_trade_pct: float = 0.05  # 5% of wallet balance per trade
    max_trade_usd: float = 15.0  # Hard cap: never trade more than $15 per order
    max_daily_loss_usd: float = 50.0  # Circuit breaker: stop after $50 daily loss
    poll_interval_seconds: int = 30
    max_slippage_pct: float = 0.02  # 2% max slippage
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    log_file: str = "copytrade_audit.log"
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "CopyTradeConfig":
        target_url = os.getenv("COPY_TARGET_PROFILE_URL", "")
        target_address = os.getenv("COPY_TARGET_ADDRESS", "")
        if not target_url and not target_address:
            raise ValueError(
                "Either COPY_TARGET_PROFILE_URL or COPY_TARGET_ADDRESS must be set in .env"
            )
        if target_address and not re.match(r"^0x[a-fA-F0-9]{40}$", target_address):
            raise ValueError(
                f"COPY_TARGET_ADDRESS does not look like a valid Ethereum address: {target_address}"
            )

        max_trade_pct = float(os.getenv("MAX_TRADE_PCT", "0.05"))
        if not 0.0 < max_trade_pct <= 1.0:
            raise ValueError(
                f"MAX_TRADE_PCT must be between 0 and 1, got {max_trade_pct}"
            )

        poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
        if poll_interval < 10:
            raise ValueError(
                f"POLL_INTERVAL_SECONDS must be >= 10, got {poll_interval}"
            )

        max_slippage = float(os.getenv("MAX_SLIPPAGE_PCT", "0.02"))

        max_trade_usd = float(os.getenv("MAX_TRADE_USD", "15.0"))
        if max_trade_usd <= 0:
            raise ValueError(
                f"MAX_TRADE_USD must be positive, got {max_trade_usd}"
            )

        max_daily_loss_usd = float(os.getenv("MAX_DAILY_LOSS_USD", "50.0"))
        if max_daily_loss_usd <= 0:
            raise ValueError(
                f"MAX_DAILY_LOSS_USD must be positive, got {max_daily_loss_usd}"
            )

        dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        log_file = os.getenv("COPYTRADE_LOG_FILE", "copytrade_audit.log")

        return cls(
            target_profile_url=target_url,
            max_trade_pct=max_trade_pct,
            max_trade_usd=max_trade_usd,
            max_daily_loss_usd=max_daily_loss_usd,
            poll_interval_seconds=poll_interval,
            max_slippage_pct=max_slippage,
            log_file=log_file,
            dry_run=dry_run,
        )

    def extract_username_from_url(self) -> str:
        match = re.search(r"polymarket\.com/@(\w+)", self.target_profile_url)
        if match:
            return match.group(1)
        raise ValueError(
            f"Cannot extract username from URL: {self.target_profile_url}"
        )


def validate_private_key() -> str:
    key = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "")
    if not key:
        raise ValueError(
            "POLYGON_WALLET_PRIVATE_KEY must be set in .env. "
            "Never share this key or commit it to version control."
        )
    if not re.match(r"^(0x)?[0-9a-fA-F]{64}$", key):
        raise ValueError(
            "POLYGON_WALLET_PRIVATE_KEY does not look like a valid private key."
        )
    return key
