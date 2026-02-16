import ast
import time

from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType

from agents.copytrade.config import CopyTradeConfig, validate_private_key
from agents.copytrade.logger import log_event, log_trade, setup_audit_logger
from agents.copytrade.monitor import ProfileMonitor
from agents.copytrade.safety import SafetyGuard
from agents.copytrade.telegram import TelegramNotifier
from agents.polymarket.polymarket import Polymarket


class CopyTradeBot:
    """
    Autonomous copy-trading bot for Polymarket.

    Monitors a target profile and mirrors their trades with:
    - 5% max per trade (configurable)
    - Slippage protection
    - Full audit logging
    - Telegram notifications (optional)
    """

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.telegram = TelegramNotifier()

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
            self.telegram.notify_trade(
                action="TRADE_REJECTED",
                side=side,
                amount=target_size,
                price=price,
                market=market_question,
                balance=usdc_balance,
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
            self.telegram.notify_trade(
                action="DRY_RUN_TRADE",
                side=side,
                amount=amount,
                price=price,
                market=market_question,
                balance=usdc_balance,
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
            self.telegram.notify_trade(
                action="TRADE_EXECUTED",
                side=side,
                amount=amount,
                price=price,
                market=market_question,
                balance=usdc_balance,
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
            self.telegram.notify_trade(
                action="TRADE_FAILED",
                side=side,
                amount=amount,
                price=price,
                market=market_question,
                balance=usdc_balance,
                error=str(e),
            )

    def run(self) -> None:
        """Main loop: monitor target and mirror trades."""
        log_event(self.logger, "BOT_START", "Copy-trade bot starting up")

        # Initialize monitor (loads existing trades to skip)
        self.monitor.initialize()

        target = self.monitor.get_target_address()
        balance = self.safety.get_usdc_balance()
        status_msg = (
            f"Monitoring: {target}\n"
            f"Balance: ${balance:.2f} USDC\n"
            f"Max/trade: ${self.config.max_trade_usd:.2f} or {self.config.max_trade_pct:.0%}\n"
            f"Daily limit: ${self.config.max_daily_loss_usd:.2f}\n"
            f"Dry run: {self.config.dry_run}"
        )
        log_event(self.logger, "BOT_STATUS", status_msg)
        self.telegram.notify_bot_event("BOT_START", status_msg)

        consecutive_errors = 0
        max_consecutive_errors = 10

        poll_count = 0
        while True:
            try:
                new_trades = self.monitor.detect_new_trades()
                poll_count += 1

                if new_trades:
                    print(f"\n[Poll #{poll_count}] ** Found {len(new_trades)} new trade(s)! **")
                    for trade in new_trades:
                        print(f"  >> {trade.get('side')} {trade.get('market', 'unknown')} | Size: {trade.get('size', 0)} | Price: {trade.get('price', 0)}")
                        self.execute_mirror_trade(trade)
                else:
                    positions_count = len(self.monitor.last_positions)
                    if poll_count % 10 == 0:
                        balance = self.safety.get_usdc_balance()
                        print(f"[Poll #{poll_count}] Monitoring... Balance: ${balance:.2f} | Tracking {positions_count} positions | No changes")
                    elif poll_count <= 3:
                        print(f"[Poll #{poll_count}] Checking... {positions_count} positions tracked")

                consecutive_errors = 0
                time.sleep(self.config.poll_interval_seconds)

            except KeyboardInterrupt:
                log_event(self.logger, "BOT_STOP", "Bot stopped by user")
                self.telegram.notify_bot_event("BOT_STOP", "Bot stopped by user")
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(
                    self.config.retry_backoff_base ** consecutive_errors, 300
                )
                error_msg = (
                    f"Error in main loop ({consecutive_errors}/{max_consecutive_errors}): "
                    f"{e}. Retrying in {backoff:.0f}s"
                )
                log_event(self.logger, "BOT_ERROR", error_msg)

                if consecutive_errors >= max_consecutive_errors:
                    shutdown_msg = f"Too many consecutive errors ({max_consecutive_errors}). Shutting down."
                    log_event(self.logger, "BOT_SHUTDOWN", shutdown_msg)
                    self.telegram.notify_bot_event("BOT_SHUTDOWN", shutdown_msg)
                    break

                # Only notify on Telegram every 3rd error to avoid spam
                if consecutive_errors % 3 == 1:
                    self.telegram.notify_bot_event("BOT_ERROR", error_msg)

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
            "telegram_enabled": self.telegram.enabled,
        }
