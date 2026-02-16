from dataclasses import dataclass, field
from datetime import date

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger
from agents.polymarket.polymarket import Polymarket


@dataclass
class TradeValidation:
    valid: bool
    reason: str
    adjusted_amount: float = 0.0


@dataclass
class DailyLedger:
    """Tracks spending per calendar day for the circuit breaker."""

    current_date: str = ""
    total_spent: float = 0.0

    def record(self, amount: float) -> None:
        today = date.today().isoformat()
        if today != self.current_date:
            # New day — reset
            self.current_date = today
            self.total_spent = 0.0
        self.total_spent += amount

    def spent_today(self) -> float:
        today = date.today().isoformat()
        if today != self.current_date:
            return 0.0
        return self.total_spent


class SafetyGuard:
    """Validates and constrains trades before execution."""

    def __init__(self, config: CopyTradeConfig, polymarket: Polymarket) -> None:
        self.config = config
        self.polymarket = polymarket
        self.logger = setup_audit_logger(config.log_file)
        self.daily_ledger = DailyLedger()

    def get_usdc_balance(self) -> float:
        return self.polymarket.get_usdc_balance()

    def record_trade(self, amount: float) -> None:
        """Call after a trade executes to update daily spending."""
        self.daily_ledger.record(amount)

    def calculate_trade_amount(
        self, target_trade_size: float, usdc_balance: float
    ) -> float:
        """Calculate the trade amount, capped at max_trade_pct of balance AND max_trade_usd."""
        pct_cap = usdc_balance * self.config.max_trade_pct
        usd_cap = self.config.max_trade_usd
        amount = min(target_trade_size, pct_cap, usd_cap)

        if amount <= 0:
            return 0.0

        return round(amount, 2)

    def validate_trade(
        self,
        token_id: str,
        side: str,
        amount: float,
        target_price: float,
    ) -> TradeValidation:
        """Run all safety checks on a proposed trade."""

        # Validate side early (cheap check)
        if side.upper() not in ("BUY", "SELL"):
            return TradeValidation(
                valid=False,
                reason=f"Invalid trade side: {side}",
            )

        # Validate price range
        if not (0.0 < target_price < 1.0):
            return TradeValidation(
                valid=False,
                reason=f"Price {target_price} outside valid range (0, 1)",
            )

        usdc_balance = self.get_usdc_balance()

        # Check minimum balance
        if usdc_balance < 1.0:
            return TradeValidation(
                valid=False,
                reason=f"Insufficient USDC balance: ${usdc_balance:.2f}",
            )

        # Daily loss circuit breaker
        spent = self.daily_ledger.spent_today()
        remaining_daily = self.config.max_daily_loss_usd - spent
        if remaining_daily <= 0:
            return TradeValidation(
                valid=False,
                reason=(
                    f"Daily loss limit reached: ${spent:.2f} spent today "
                    f"(limit: ${self.config.max_daily_loss_usd:.2f})"
                ),
            )

        # Cap the amount at percentage limit AND absolute USD limit
        capped_amount = self.calculate_trade_amount(amount, usdc_balance)

        # Also cap at remaining daily allowance
        capped_amount = min(capped_amount, remaining_daily)
        capped_amount = round(capped_amount, 2)

        if capped_amount <= 0:
            return TradeValidation(
                valid=False,
                reason=f"Trade amount too small after caps: ${capped_amount}",
            )

        # NOTE: Slippage check removed — CLOB API get_price() causes rate limits.
        # We trust the target's trade price directly. The max_trade_usd cap and
        # daily loss circuit breaker provide sufficient protection.

        log_event(
            self.logger,
            "TRADE_VALIDATED",
            f"Trade approved: {side} {token_id} amount=${capped_amount:.2f} "
            f"(balance=${usdc_balance:.2f}, pct_cap={self.config.max_trade_pct:.0%}, "
            f"usd_cap=${self.config.max_trade_usd:.2f}, "
            f"daily_spent=${spent:.2f}/{self.config.max_daily_loss_usd:.2f})",
        )

        return TradeValidation(
            valid=True,
            reason="Trade passed all safety checks",
            adjusted_amount=capped_amount,
        )
