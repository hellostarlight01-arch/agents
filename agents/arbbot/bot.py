"""Latency arbitrage bot for Polymarket 15-minute crypto markets.

Watches Binance prices in real-time, detects significant moves,
and places trades on Polymarket before odds update.
"""

import time
from datetime import date

from agents.arbbot.config import ArbBotConfig
from agents.arbbot.market_finder import MarketFinder
from agents.arbbot.price_feed import BinancePriceFeed
from agents.copytrade.config import validate_private_key
from agents.copytrade.logger import log_event, log_trade, setup_audit_logger
from agents.copytrade.telegram import TelegramNotifier
from agents.polymarket.polymarket import Polymarket


class DailyTracker:
    """Track daily spending for circuit breaker."""

    def __init__(self) -> None:
        self.current_date = ""
        self.total_spent = 0.0
        self.trades_today = 0
        self.wins_today = 0

    def record(self, amount: float) -> None:
        today = date.today().isoformat()
        if today != self.current_date:
            self.current_date = today
            self.total_spent = 0.0
            self.trades_today = 0
            self.wins_today = 0
        self.total_spent += amount
        self.trades_today += 1

    def spent_today(self) -> float:
        today = date.today().isoformat()
        if today != self.current_date:
            return 0.0
        return self.total_spent


class ArbBot:
    """Latency arbitrage bot for Polymarket 15-min crypto markets.

    Flow:
    1. Connect to Binance websocket for real-time prices
    2. Every second, check if any coin has moved significantly
    3. If move detected, find the active 15-min market on Polymarket
    4. Place a limit order on the winning side (maker = 0 fees)
    5. Repeat
    """

    def __init__(self, config: ArbBotConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.telegram = TelegramNotifier()

        validate_private_key()
        self.polymarket = Polymarket()
        self.price_feed = BinancePriceFeed(
            coins=config.coins,
            lookback_seconds=config.lookback_seconds,
        )
        self.market_finder = MarketFinder()
        self.daily = DailyTracker()

        # Track which (coin, window_ts) we've already traded to avoid duplicates
        self._traded_windows: set[tuple[str, int]] = set()

    def _calculate_trade_size(self, price: float) -> float:
        """Calculate trade size, respecting caps."""
        balance = self.polymarket.get_usdc_balance()
        pct_cap = balance * self.config.max_trade_pct
        usd_cap = self.config.max_trade_usd
        daily_remaining = self.config.max_daily_loss_usd - self.daily.spent_today()

        if daily_remaining <= 0:
            return 0.0

        amount = min(pct_cap, usd_cap, daily_remaining)
        if amount < 1.0:
            return 0.0
        return round(amount, 2)

    def _execute_trade(
        self, coin: str, side: str, token_id: str, price: float,
        market_info: dict, move_pct: float, binance_price: float,
    ) -> None:
        """Execute or dry-run a single arb trade."""
        amount = self._calculate_trade_size(price)
        if amount <= 0:
            log_event(self.logger, "TRADE_SKIP", f"Amount too small for {coin} {side}")
            return

        market_question = market_info.get("question", f"{coin.upper()} 15m")
        balance = self.polymarket.get_usdc_balance()

        if self.config.dry_run:
            log_trade(
                logger=self.logger,
                action="DRY_RUN_ARB",
                target_address="SELF",
                market_question=market_question,
                token_id=token_id[:30] + "...",
                side=side,
                amount=amount,
                price=price,
                usdc_balance=balance,
                result=f"DRY_RUN | {coin.upper()} move: {move_pct:+.3f}% | Binance: ${binance_price:,.2f}",
            )
            self.telegram.notify_trade(
                action="DRY_RUN_TRADE",
                side=side,
                amount=amount,
                price=price,
                market=f"ARB: {market_question[:50]}",
                balance=balance,
            )
            self.daily.record(amount)
            print(f"  >> DRY RUN: {side} {coin.upper()} ${amount:.2f} @ {price:.4f} | Move: {move_pct:+.3f}%")
            return

        # Live trade — use limit order (maker = 0 fees)
        try:
            resp = self.polymarket.execute_order(
                price=price,
                size=amount,
                side=side,
                token_id=token_id,
            )
            self.daily.record(amount)
            log_trade(
                logger=self.logger,
                action="ARB_TRADE_EXECUTED",
                target_address="SELF",
                market_question=market_question,
                token_id=token_id[:30] + "...",
                side=side,
                amount=amount,
                price=price,
                usdc_balance=balance,
                result=str(resp),
            )
            self.telegram.notify_trade(
                action="TRADE_EXECUTED",
                side=side,
                amount=amount,
                price=price,
                market=f"ARB: {market_question[:50]}",
                balance=balance,
            )
            print(f"  >> EXECUTED: {side} {coin.upper()} ${amount:.2f} @ {price:.4f}")
        except Exception as e:
            log_trade(
                logger=self.logger,
                action="ARB_TRADE_FAILED",
                target_address="SELF",
                market_question=market_question,
                token_id=token_id[:30] + "...",
                side=side,
                amount=amount,
                price=price,
                usdc_balance=balance,
                result="ERROR",
                error=str(e),
            )
            print(f"  >> FAILED: {side} {coin.upper()} — {e}")

    def _check_and_trade(self, coin: str) -> None:
        """Check a coin for price movement and trade if significant."""
        move_pct, current_price = self.price_feed.get_price_move(coin)

        if abs(move_pct) < self.config.min_move_pct:
            return

        # Check timing within the 15-min window
        secs_in = self.market_finder.seconds_into_window()
        if secs_in < self.config.min_window_seconds:
            return  # Too early in window
        if secs_in > self.config.max_window_seconds:
            return  # Too close to resolution

        # Check daily limit
        if self.daily.spent_today() >= self.config.max_daily_loss_usd:
            return

        # Find the active market
        market = self.market_finder.find_active_market(coin)
        if not market:
            return

        # Check if we already traded this window for this coin
        window_key = (coin, market["window_ts"])
        if window_key in self._traded_windows:
            return

        # Determine side: price went UP -> buy "Up" token, price went DOWN -> buy "Down"
        if move_pct > 0:
            side = "BUY"
            token_id = market["token_up"]
            price = market["price_up"]
        else:
            side = "BUY"
            token_id = market["token_down"]
            price = market["price_down"]

        # Sanity check price
        if not (0.01 < price < 0.99):
            return

        self._traded_windows.add(window_key)

        print(f"\n  ** {coin.upper()} moved {move_pct:+.3f}% (Binance: ${current_price:,.2f}) **")
        print(f"     Market: {market['question'][:60]}")
        print(f"     Buying {'Up' if move_pct > 0 else 'Down'} @ {price:.4f}")

        self._execute_trade(
            coin=coin,
            side=side,
            token_id=token_id,
            price=price,
            market_info=market,
            move_pct=move_pct,
            binance_price=current_price,
        )

    def run(self) -> None:
        """Main loop: watch prices and trade on significant moves."""
        log_event(self.logger, "ARB_BOT_START", "Latency arb bot starting")

        coins_str = ", ".join(c.upper() for c in self.config.coins)
        balance = self.polymarket.get_usdc_balance()

        print(f"\n=== Latency Arb Bot ===")
        print(f"  Coins: {coins_str}")
        print(f"  Balance: ${balance:.2f} USDC")
        print(f"  Max/trade: ${self.config.max_trade_usd:.2f} or {self.config.max_trade_pct:.0%}")
        print(f"  Min move: {self.config.min_move_pct:.2f}%")
        print(f"  Lookback: {self.config.lookback_seconds}s")
        print(f"  Dry run: {self.config.dry_run}")
        print(f"  Connecting to Binance...")

        self.price_feed.start()
        time.sleep(2)  # Let prices accumulate

        status = f"Coins: {coins_str}\nBalance: ${balance:.2f}\nMin move: {self.config.min_move_pct}%\nDry run: {self.config.dry_run}"
        self.telegram.notify_bot_event("BOT_START", f"Arb Bot Started\n{status}")

        print(f"  Monitoring for price movements...\n")

        tick = 0
        consecutive_errors = 0
        last_window_ts = 0

        while True:
            try:
                tick += 1

                # Clean up traded windows and cache on new window
                current_window = self.market_finder.current_window_ts()
                if current_window != last_window_ts:
                    last_window_ts = current_window
                    self.market_finder.clear_cache()
                    # Keep only current window trades
                    self._traded_windows = {
                        k for k in self._traded_windows if k[1] >= current_window - 900
                    }

                # Check each coin
                for coin in self.config.coins:
                    self._check_and_trade(coin)

                # Status update every 60 ticks (~60 seconds)
                if tick % 60 == 0:
                    prices = " | ".join(
                        f"{c.upper()}: ${self.price_feed.latest_price.get(c, 0):,.2f}"
                        for c in self.config.coins
                    )
                    secs_left = self.market_finder.seconds_remaining()
                    trades = self.daily.trades_today
                    spent = self.daily.spent_today()
                    print(f"  [{tick}s] {prices} | Window: {secs_left}s left | Trades: {trades} | Spent: ${spent:.2f}")

                consecutive_errors = 0
                time.sleep(1)  # Check every second

            except KeyboardInterrupt:
                log_event(self.logger, "ARB_BOT_STOP", "Bot stopped by user")
                self.telegram.notify_bot_event("BOT_STOP", "Arb bot stopped by user")
                self.price_feed.stop()
                break
            except Exception as e:
                consecutive_errors += 1
                log_event(self.logger, "ARB_BOT_ERROR", f"Error: {e}")
                if consecutive_errors >= 10:
                    log_event(self.logger, "ARB_BOT_SHUTDOWN", "Too many errors")
                    self.telegram.notify_bot_event("BOT_SHUTDOWN", f"Too many errors: {e}")
                    self.price_feed.stop()
                    break
                time.sleep(min(2 ** consecutive_errors, 30))
