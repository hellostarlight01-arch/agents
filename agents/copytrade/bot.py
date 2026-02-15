import ast
import time

from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType

from agents.copytrade.config import CopyTradeConfig, validate_private_key
from agents.copytrade.logger import log_event, log_trade, setup_audit_logger
from agents.copytrade.monitor import ProfileMonitor
from agents.copytrade.safety import SafetyGuard
from agents.polymarket.polymarket import Polymarket


class CopyTradeBot:
    """
    Autonomous copy-trading bot for Polymarket.

    Monitors a target profile and mirrors their trades with:
    - 5% max per trade (configurable)
    - Slippage protection
    - Full audit logging
    """

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)

        # Validate private key before initializing anything
        validate_private_key()

        self.polymarket = Polymarket()
        self.monitor = ProfileMonitor(config)
        self.safety = SafetyGuard(config, self.polymarket)

    def execute_mirror_trade(self, trade: dict) -> None:
        """Execute a single mirror trade based on a target's trade."""
        token_id = (
            trade.get("asset_id")
            or trade.get("token_id")
            or trade.get("market")
            or ""
        )
        side = trade.get("side", "BUY").upper()
        price = float(trade.get("price", 0))
        target_size = float(trade.get("size", 0))

        if not token_id:
            log_event(
                self.logger,
                "SKIP_TRADE",
                f"No token_id found in trade: {trade}",
            )
            return

        market_question = trade.get("market", token_id[:20])
        usdc_balance = self.safety.get_usdc_balance()

        # Validate trade through safety guard
        validation = self.safety.validate_trade(
            token_id=token_id,
            side=side,
            amount=target_size,
            target_price=price,
        )

        if not validation.valid:
            log_trade(
                logger=self.logger,
                action="TRADE_REJECTED",
                target_address=self.monitor.get_target_address(),
                market_question=market_question,
                token_id=token_id,
                side=side,
                amount=target_size,
                price=price,
                usdc_balance=usdc_balance,
                result="REJECTED",
                error=validation.reason,
            )
            return

        amount = validation.adjusted_amount

        if self.config.dry_run:
            log_trade(
                logger=self.logger,
                action="DRY_RUN_TRADE",
                target_address=self.monitor.get_target_address(),
                market_question=market_question,
                token_id=token_id,
                side=side,
                amount=amount,
                price=price,
                usdc_balance=usdc_balance,
                result="DRY_RUN",
            )
            return

        # Execute the trade
        try:
            resp = self.polymarket.execute_order(
                price=price,
                size=amount,
                side=side,
                token_id=token_id,
            )

            self.safety.record_trade(amount)

            log_trade(
                logger=self.logger,
                action="TRADE_EXECUTED",
                target_address=self.monitor.get_target_address(),
                market_question=market_question,
                token_id=token_id,
                side=side,
                amount=amount,
                price=price,
                usdc_balance=usdc_balance,
                result=str(resp),
            )
        except Exception as e:
            log_trade(
                logger=self.logger,
                action="TRADE_FAILED",
                target_address=self.monitor.get_target_address(),
                market_question=market_question,
                token_id=token_id,
                side=side,
                amount=amount,
                price=price,
                usdc_balance=usdc_balance,
                result="ERROR",
                error=str(e),
            )

    def run(self) -> None:
        """Main loop: monitor target and mirror trades."""
        log_event(self.logger, "BOT_START", "Copy-trade bot starting up")

        # Initialize monitor (loads existing trades to skip)
        self.monitor.initialize()

        target = self.monitor.get_target_address()
        balance = self.safety.get_usdc_balance()
        log_event(
            self.logger,
            "BOT_STATUS",
            f"Monitoring target: {target} | "
            f"Balance: ${balance:.2f} USDC | "
            f"Max per trade: {self.config.max_trade_pct:.0%} | "
            f"Dry run: {self.config.dry_run}",
        )

        consecutive_errors = 0
        max_consecutive_errors = 10

        while True:
            try:
                new_trades = self.monitor.detect_new_trades()

                for trade in new_trades:
                    self.execute_mirror_trade(trade)

                consecutive_errors = 0
                time.sleep(self.config.poll_interval_seconds)

            except KeyboardInterrupt:
                log_event(self.logger, "BOT_STOP", "Bot stopped by user")
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(
                    self.config.retry_backoff_base ** consecutive_errors, 300
                )
                log_event(
                    self.logger,
                    "BOT_ERROR",
                    f"Error in main loop ({consecutive_errors}/{max_consecutive_errors}): "
                    f"{e}. Retrying in {backoff:.0f}s",
                )

                if consecutive_errors >= max_consecutive_errors:
                    log_event(
                        self.logger,
                        "BOT_SHUTDOWN",
                        f"Too many consecutive errors ({max_consecutive_errors}). Shutting down.",
                    )
                    break

                time.sleep(backoff)

    def run_once(self) -> list[dict]:
        """Run a single check cycle (useful for testing)."""
        self.monitor.initialize()
        # After init, immediately check for new ones
        new_trades = self.monitor.detect_new_trades()
        for trade in new_trades:
            self.execute_mirror_trade(trade)
        return new_trades

    def status(self) -> dict:
        """Return current bot status."""
        balance = self.safety.get_usdc_balance()
        target = self.monitor.get_target_address()
        pct_amount = balance * self.config.max_trade_pct
        effective_max = min(pct_amount, self.config.max_trade_usd)
        return {
            "target_address": target,
            "target_profile": self.config.target_profile_url,
            "usdc_balance": balance,
            "max_trade_pct": self.config.max_trade_pct,
            "max_trade_usd": self.config.max_trade_usd,
            "max_trade_amount": effective_max,
            "max_daily_loss_usd": self.config.max_daily_loss_usd,
            "daily_spent": self.safety.daily_ledger.spent_today(),
            "poll_interval": self.config.poll_interval_seconds,
            "dry_run": self.config.dry_run,
            "known_trades": len(self.monitor.known_trade_ids),
        }
