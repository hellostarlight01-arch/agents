"""
Core bot engine for the Polymarket trading bot.
Orchestrates strategies, risk management, position tracking, and trade execution.
"""

import logging
import signal
import time
import uuid
from typing import Optional

from agents.config import BotConfig, get_config, setup_logging
from agents.polymarket.polymarket import Polymarket
from agents.monitoring.positions import PositionTracker, TradeRecord
from agents.application.strategies import (
    BaseStrategy,
    TradeSignal,
    get_strategy,
    STRATEGIES,
)

logger = logging.getLogger("polybot.engine")


class TradingBot:
    """
    Main trading bot engine.
    Coordinates strategy execution, risk checks, and trade execution.
    """

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or get_config()
        self.logger = setup_logging(self.config)
        self.polymarket = Polymarket()
        self.tracker = PositionTracker(data_dir=self.config.data_dir)
        self.strategy: BaseStrategy = get_strategy(
            self.config.strategy, self.config, self.polymarket
        )
        self.running = False
        self._cycle_count = 0

        logger.info("=" * 60)
        logger.info("POLYMARKET TRADING BOT")
        logger.info("=" * 60)
        logger.info(f"Mode:     {'DRY RUN (paper trading)' if self.config.dry_run else 'LIVE TRADING'}")
        logger.info(f"Strategy: {self.strategy.name}")
        logger.info(f"LLM:      {self.config.llm_model}")
        logger.info(f"Max position: ${self.config.risk.max_position_size_usdc:.2f}")
        logger.info(f"Max exposure: ${self.config.risk.max_total_exposure_usdc:.2f}")
        logger.info(f"Trade interval: {self.config.trade_interval_seconds}s")
        logger.info("=" * 60)

    def start(self) -> None:
        """Start the trading bot main loop."""
        self.running = True

        # Handle graceful shutdown
        def _shutdown(signum, frame):
            logger.info("Shutdown signal received. Stopping bot...")
            self.running = False

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info("Bot started. Press Ctrl+C to stop.")

        if not self.config.dry_run:
            try:
                balance = self.polymarket.get_usdc_balance()
                logger.info(f"Wallet USDC balance: ${balance:.2f}")
            except Exception as e:
                logger.warning(f"Could not fetch wallet balance: {e}")

        while self.running:
            try:
                self._run_cycle()
                self._cycle_count += 1

                if self.running:
                    logger.info(
                        f"Next cycle in {self.config.trade_interval_seconds}s"
                    )
                    # Sleep in small increments to allow interrupt
                    for _ in range(self.config.trade_interval_seconds):
                        if not self.running:
                            break
                        time.sleep(1)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(30)  # back off on errors

        self._shutdown()

    def _run_cycle(self) -> None:
        """Execute one trading cycle."""
        logger.info(f"--- Cycle #{self._cycle_count + 1} ---")

        # 1. Check risk limits
        if not self._check_risk_limits():
            logger.warning("Risk limits reached. Skipping trade generation.")
            self._manage_positions()
            return

        # 2. Manage existing positions (stop loss / take profit)
        self._manage_positions()

        # 3. Generate trade signals
        logger.info(f"Running strategy: {self.strategy.name}")
        signals = self.strategy.generate_signals()
        logger.info(f"Generated {len(signals)} trade signals")

        # 4. Execute valid signals
        for signal in signals:
            if not self.running:
                break

            if not self._check_risk_limits():
                logger.warning("Risk limits reached mid-cycle")
                break

            self._execute_signal(signal)

        # 5. Log portfolio summary
        summary = self.tracker.get_portfolio_summary()
        logger.info(
            f"Portfolio: {summary['open_positions']} positions | "
            f"Exposure: ${summary['total_exposure_usdc']:.2f} | "
            f"Unrealized PnL: ${summary['total_unrealized_pnl']:+.2f} | "
            f"Daily PnL: ${summary['daily_pnl']:+.2f}"
        )

    def _check_risk_limits(self) -> bool:
        """Check if we're within risk parameters."""
        risk = self.config.risk

        # Check daily trade limit
        if self.tracker.daily_trades >= risk.max_trades_per_day:
            logger.info("Daily trade limit reached")
            return False

        # Check total exposure
        exposure = self.tracker.get_total_exposure()
        if exposure >= risk.max_total_exposure_usdc:
            logger.info(
                f"Max exposure reached: ${exposure:.2f} >= "
                f"${risk.max_total_exposure_usdc:.2f}"
            )
            return False

        # Check daily loss
        if self.tracker.daily_pnl <= -risk.max_daily_loss_usdc:
            logger.warning(
                f"Daily loss limit reached: ${self.tracker.daily_pnl:.2f}"
            )
            return False

        return True

    def _manage_positions(self) -> None:
        """Check and manage existing positions (stop loss / take profit)."""
        if not self.tracker.positions:
            return

        # Update prices for open positions
        price_map = {}
        for token_id in list(self.tracker.positions.keys()):
            try:
                price = self.polymarket.get_orderbook_price(token_id)
                price_map[token_id] = price
            except Exception as e:
                logger.debug(f"Could not get price for {token_id}: {e}")

        self.tracker.update_position_prices(price_map)

        # Check stop losses
        stop_tokens = self.tracker.check_stop_loss(
            self.config.risk.stop_loss_pct
        )
        for token_id in stop_tokens:
            price = price_map.get(token_id)
            if price is not None:
                logger.warning(f"Executing STOP LOSS for {token_id}")
                self._close_position(token_id, price, reason="stop_loss")

        # Check take profits
        tp_tokens = self.tracker.check_take_profit(
            self.config.risk.take_profit_pct
        )
        for token_id in tp_tokens:
            price = price_map.get(token_id)
            if price is not None:
                logger.info(f"Executing TAKE PROFIT for {token_id}")
                self._close_position(token_id, price, reason="take_profit")

    def _execute_signal(self, signal: TradeSignal) -> Optional[TradeRecord]:
        """Execute a trade signal."""
        risk = self.config.risk

        # Calculate position size
        if self.config.dry_run:
            balance = 1000.0  # simulated balance for paper trading
        else:
            try:
                balance = self.polymarket.get_usdc_balance()
            except Exception as e:
                logger.error(f"Could not fetch balance: {e}")
                return None

        size = min(
            balance * signal.size_pct,
            risk.max_position_size_usdc,
            risk.max_loss_per_trade_usdc,
        )

        if size < 1.0:
            logger.info(f"Position size too small: ${size:.2f}")
            return None

        # Check if we already have a position in this market
        if signal.token_id in self.tracker.positions:
            logger.info(
                f"Already have position in {signal.question}, skipping"
            )
            return None

        trade_id = str(uuid.uuid4())[:8]

        logger.info(
            f"{'[DRY RUN] ' if self.config.dry_run else ''}"
            f"Executing {signal.side} signal: {signal.question} | "
            f"{signal.outcome} @ ${signal.price:.4f} | "
            f"Size: ${size:.2f} | Confidence: {signal.confidence:.2f}"
        )

        # Execute the actual trade (or simulate)
        if not self.config.dry_run:
            try:
                resp = self.polymarket.execute_order(
                    price=signal.price,
                    size=size,
                    side=signal.side,
                    token_id=signal.token_id,
                )
                logger.info(f"Trade executed: {resp}")
            except Exception as e:
                logger.error(f"Trade execution failed: {e}", exc_info=True)
                return None

        # Record the position
        position = self.tracker.open_position(
            trade_id=trade_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
            question=signal.question,
            outcome=signal.outcome,
            side=signal.side,
            price=signal.price,
            size=size,
            confidence=signal.confidence,
            strategy=signal.strategy_name,
            dry_run=self.config.dry_run,
        )

        logger.info(f"Position opened: {position.question}")
        return None

    def _close_position(
        self, token_id: str, price: float, reason: str = ""
    ) -> None:
        """Close a position."""
        if not self.config.dry_run:
            position = self.tracker.positions.get(token_id)
            if position:
                try:
                    close_side = "SELL" if position.side == "BUY" else "BUY"
                    self.polymarket.execute_order(
                        price=price,
                        size=position.shares,
                        side=close_side,
                        token_id=token_id,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to close position on-chain: {e}",
                        exc_info=True,
                    )
                    return

        self.tracker.close_position(
            token_id=token_id,
            close_price=price,
            dry_run=self.config.dry_run,
        )

    def run_once(self) -> dict:
        """Run a single trading cycle and return results."""
        logger.info("Running single cycle...")
        self._run_cycle()
        return self.tracker.get_portfolio_summary()

    def get_status(self) -> dict:
        """Get current bot status."""
        summary = self.tracker.get_portfolio_summary()
        return {
            "running": self.running,
            "mode": "dry_run" if self.config.dry_run else "live",
            "strategy": self.strategy.name,
            "cycle_count": self._cycle_count,
            "portfolio": summary,
            "positions": [
                {
                    "question": p.question,
                    "outcome": p.outcome,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "size": p.size,
                    "pnl": round(p.unrealized_pnl, 2),
                    "pnl_pct": f"{p.unrealized_pnl_pct:+.1%}",
                }
                for p in self.tracker.positions.values()
            ],
        }

    def _shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down...")
        summary = self.tracker.get_portfolio_summary()
        logger.info("=" * 60)
        logger.info("FINAL SUMMARY")
        logger.info(f"  Cycles completed: {self._cycle_count}")
        logger.info(f"  Total trades: {summary['total_trades']}")
        logger.info(f"  Open positions: {summary['open_positions']}")
        logger.info(f"  Unrealized PnL: ${summary['total_unrealized_pnl']:+.2f}")
        logger.info(f"  Realized PnL: ${summary['total_realized_pnl']:+.2f}")
        logger.info(f"  Daily PnL: ${summary['daily_pnl']:+.2f}")
        logger.info("=" * 60)
        logger.info("Bot stopped.")
