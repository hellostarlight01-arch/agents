"""
Live terminal-based monitoring dashboard for the Polymarket trading bot.
Displays positions, P&L, alerts, and market data in real-time.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime

from agents.config import BotConfig, get_config, setup_logging
from agents.polymarket.polymarket import Polymarket
from agents.monitoring.positions import PositionTracker
from agents.monitoring.alerts import AlertManager, Alert, create_position_alerts

logger = logging.getLogger("polybot.dashboard")

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
BG_YELLOW = "\033[43m"


def color_pnl(value: float, as_pct: bool = False) -> str:
    """Color a P&L value green/red."""
    if as_pct:
        text = f"{value:+.1%}"
    else:
        text = f"${value:+.2f}"

    if value > 0:
        return f"{GREEN}{text}{RESET}"
    elif value < 0:
        return f"{RED}{text}{RESET}"
    return f"{DIM}{text}{RESET}"


def severity_color(severity: str) -> str:
    """Get color for alert severity."""
    colors = {
        "info": CYAN,
        "warning": YELLOW,
        "critical": RED,
    }
    return colors.get(severity, WHITE)


class LiveDashboard:
    """Terminal-based live monitoring dashboard."""

    def __init__(self, config: BotConfig = None) -> None:
        self.config = config or get_config()
        self.polymarket = Polymarket()
        self.tracker = PositionTracker(data_dir=self.config.data_dir)
        self.alert_manager = AlertManager()
        self.running = False
        self._last_balance: float = 0.0

        # Set up alerts
        alert_rules = create_position_alerts(
            self.tracker, self.config.risk
        )
        for rule in alert_rules:
            self.alert_manager.add_rule(rule)

        # Alert callback to log
        self.alert_manager.add_callback(self._on_alert)

    def _on_alert(self, alert: Alert) -> None:
        """Handle alert events."""
        color = severity_color(alert.severity)
        logger.info(
            f"{color}[ALERT:{alert.severity.upper()}]{RESET} {alert.message}"
        )

    def start(self) -> None:
        """Start the live monitoring dashboard."""
        self.running = True

        def _shutdown(signum, frame):
            self.running = False

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info("Starting live dashboard. Press Ctrl+C to stop.")

        while self.running:
            try:
                self._update()
                self._render()
                self.alert_manager.check_all()

                for _ in range(self.config.monitor_interval_seconds):
                    if not self.running:
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Dashboard error: {e}", exc_info=True)
                time.sleep(5)

        print(f"\n{DIM}Dashboard stopped.{RESET}")

    def _update(self) -> None:
        """Update position prices from the market."""
        price_map = {}
        for token_id in list(self.tracker.positions.keys()):
            try:
                price = self.polymarket.get_orderbook_price(token_id)
                price_map[token_id] = price
            except Exception:
                pass

        if price_map:
            self.tracker.update_position_prices(price_map)

        # Fetch balance periodically
        try:
            self._last_balance = self.polymarket.get_usdc_balance()
        except Exception:
            pass

    def _render(self) -> None:
        """Render the dashboard to the terminal."""
        # Clear screen
        os.system("clear" if os.name == "posix" else "cls")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = self.tracker.get_portfolio_summary()

        # Header
        print(f"{BOLD}{CYAN}{'=' * 72}{RESET}")
        print(f"{BOLD}{CYAN}  POLYMARKET TRADING BOT - LIVE DASHBOARD{RESET}")
        print(f"{BOLD}{CYAN}{'=' * 72}{RESET}")
        print(f"  {DIM}{now} | Refresh: {self.config.monitor_interval_seconds}s{RESET}")
        print()

        # Wallet & Portfolio Summary
        print(f"{BOLD}  PORTFOLIO SUMMARY{RESET}")
        print(f"  {'─' * 50}")
        print(f"  Wallet Balance:    {BOLD}${self._last_balance:,.2f} USDC{RESET}")
        print(f"  Open Positions:    {BOLD}{summary['open_positions']}{RESET}")
        print(f"  Total Exposure:    {BOLD}${summary['total_exposure_usdc']:,.2f}{RESET}")
        print(f"  Unrealized PnL:    {color_pnl(summary['total_unrealized_pnl'])}")
        print(f"  Realized PnL:      {color_pnl(summary['total_realized_pnl'])}")
        print(f"  Daily PnL:         {color_pnl(summary['daily_pnl'])}")
        print(f"  Daily Trades:      {summary['daily_trades']} / {self.config.risk.max_trades_per_day}")
        print(f"  Total Trades:      {summary['total_trades']}")
        print()

        # Positions Table
        positions = list(self.tracker.positions.values())
        if positions:
            print(f"{BOLD}  OPEN POSITIONS{RESET}")
            print(f"  {'─' * 68}")
            print(
                f"  {DIM}{'Market':<30} {'Side':<5} {'Entry':>7} {'Current':>7} "
                f"{'Size':>7} {'PnL':>10} {'PnL%':>7}{RESET}"
            )
            print(f"  {'─' * 68}")

            for pos in positions:
                question = pos.question[:28] + ".." if len(pos.question) > 30 else pos.question
                pnl_str = color_pnl(pos.unrealized_pnl)
                pnl_pct_str = color_pnl(pos.unrealized_pnl_pct, as_pct=True)

                print(
                    f"  {question:<30} {pos.side:<5} "
                    f"${pos.entry_price:>6.4f} ${pos.current_price:>6.4f} "
                    f"${pos.size:>6.2f} {pnl_str:>19} {pnl_pct_str:>16}"
                )

            print(f"  {'─' * 68}")
        else:
            print(f"  {DIM}No open positions{RESET}")
        print()

        # Recent Trades
        recent_trades = self.tracker.trade_history[-5:]
        if recent_trades:
            print(f"{BOLD}  RECENT TRADES{RESET}")
            print(f"  {'─' * 68}")
            print(
                f"  {DIM}{'Time':<12} {'Market':<25} {'Side':<5} "
                f"{'Price':>7} {'Size':>7} {'PnL':>10}{RESET}"
            )
            print(f"  {'─' * 68}")

            for trade in reversed(recent_trades):
                ts = datetime.fromtimestamp(trade.timestamp).strftime("%H:%M:%S")
                question = trade.question[:23] + ".." if len(trade.question) > 25 else trade.question
                pnl_str = color_pnl(trade.pnl) if trade.pnl is not None else f"{DIM}--{RESET}"
                dry = f" {DIM}(paper){RESET}" if trade.dry_run else ""

                print(
                    f"  {ts:<12} {question:<25} {trade.side:<5} "
                    f"${trade.price:>6.4f} ${trade.size:>6.2f} {pnl_str:>19}{dry}"
                )

            print(f"  {'─' * 68}")
        print()

        # Alerts
        recent_alerts = self.alert_manager.get_recent_alerts(5)
        if recent_alerts:
            print(f"{BOLD}  ALERTS{RESET}")
            print(f"  {'─' * 68}")
            for alert in reversed(recent_alerts):
                ts = datetime.fromtimestamp(alert.timestamp).strftime("%H:%M:%S")
                color = severity_color(alert.severity)
                sev = alert.severity.upper()
                print(
                    f"  {DIM}{ts}{RESET}  {color}[{sev:>8}]{RESET}  {alert.message}"
                )
            print(f"  {'─' * 68}")
        print()

        # Risk meters
        exposure = summary["total_exposure_usdc"]
        max_exp = self.config.risk.max_total_exposure_usdc
        exp_pct = exposure / max_exp if max_exp > 0 else 0

        bar_width = 30
        filled = int(bar_width * min(exp_pct, 1.0))
        bar_color = GREEN if exp_pct < 0.6 else (YELLOW if exp_pct < 0.85 else RED)
        bar = f"{bar_color}{'█' * filled}{DIM}{'░' * (bar_width - filled)}{RESET}"

        print(f"  {BOLD}RISK{RESET}")
        print(f"  Exposure:  [{bar}] {exp_pct:.0%} (${exposure:.2f} / ${max_exp:.2f})")

        daily_loss_pct = abs(self.tracker.daily_pnl / self.config.risk.max_daily_loss_usdc) if self.config.risk.max_daily_loss_usdc > 0 and self.tracker.daily_pnl < 0 else 0
        filled_loss = int(bar_width * min(daily_loss_pct, 1.0))
        loss_bar_color = GREEN if daily_loss_pct < 0.5 else (YELLOW if daily_loss_pct < 0.8 else RED)
        loss_bar = f"{loss_bar_color}{'█' * filled_loss}{DIM}{'░' * (bar_width - filled_loss)}{RESET}"

        print(f"  Daily Loss:[{loss_bar}] {daily_loss_pct:.0%} (${abs(self.tracker.daily_pnl):.2f} / ${self.config.risk.max_daily_loss_usdc:.2f})")
        print()

        print(f"  {DIM}Press Ctrl+C to stop{RESET}")


def run_dashboard(config: BotConfig = None) -> None:
    """Entry point to run the live dashboard."""
    dashboard = LiveDashboard(config)
    dashboard.start()
