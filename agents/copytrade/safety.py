from dataclasses import dataclass
from typing import Optional

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger
from agents.polymarket.polymarket import Polymarket


@dataclass
class TradeValidation:
    valid: bool
    reason: str
    adjusted_amount: float = 0.0


class SafetyGuard:
    """Validates and constrains trades before execution."""

    def __init__(self, config: CopyTradeConfig, polymarket: Polymarket) -> None:
        self.config = config
        self.polymarket = polymarket
        self.logger = setup_audit_logger(config.log_file)

    def get_usdc_balance(self) -> float:
        return self.polymarket.get_usdc_balance()

    def calculate_trade_amount(
        self, target_trade_size: float, usdc_balance: float
    ) -> float:
        """Calculate the trade amount, capped at max_trade_pct of balance."""
        max_amount = usdc_balance * self.config.max_trade_pct
        amount = min(target_trade_size, max_amount)

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
        usdc_balance = self.get_usdc_balance()

        # Check minimum balance
        if usdc_balance < 1.0:
            return TradeValidation(
                valid=False,
                reason=f"Insufficient USDC balance: ${usdc_balance:.2f}",
            )

        # Cap the amount at max_trade_pct
        capped_amount = self.calculate_trade_amount(amount, usdc_balance)
        if capped_amount <= 0:
            return TradeValidation(
                valid=False,
                reason=f"Trade amount too small after 5% cap: ${capped_amount}",
            )

        # Check slippage against current orderbook price
        try:
            current_price = self.polymarket.get_orderbook_price(token_id)
            price_diff = abs(current_price - target_price)
            if target_price > 0:
                slippage = price_diff / target_price
            else:
                slippage = 1.0

            if slippage > self.config.max_slippage_pct:
                return TradeValidation(
                    valid=False,
                    reason=(
                        f"Slippage too high: {slippage:.2%} "
                        f"(target: {target_price}, current: {current_price}, "
                        f"max: {self.config.max_slippage_pct:.2%})"
                    ),
                )
        except Exception as e:
            log_event(
                self.logger,
                "SLIPPAGE_CHECK_ERROR",
                f"Could not check slippage for {token_id}: {e}",
            )
            # Proceed with caution if we can't check slippage
            pass

        # Validate side
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

        log_event(
            self.logger,
            "TRADE_VALIDATED",
            f"Trade approved: {side} {token_id} amount=${capped_amount:.2f} "
            f"(balance=${usdc_balance:.2f}, cap={self.config.max_trade_pct:.0%})",
        )

        return TradeValidation(
            valid=True,
            reason="Trade passed all safety checks",
            adjusted_amount=capped_amount,
        )
