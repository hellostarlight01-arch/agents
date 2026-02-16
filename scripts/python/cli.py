import json
from datetime import datetime

import typer
from devtools import pprint

from agents.polymarket.polymarket import Polymarket
from agents.connectors.chroma import PolymarketRAG
from agents.connectors.news import News
from agents.application.trade import Trader
from agents.application.executor import Executor
from agents.application.creator import Creator
from agents.config import get_config, setup_logging
from agents.application.strategies import STRATEGIES

app = typer.Typer(
    name="polybot",
    help="Polymarket AI Trading Bot - Autonomous prediction market trading",
)

# Lazy-init globals to avoid crashing when env vars are missing
_polymarket = None
_newsapi_client = None
_polymarket_rag = None


def _get_polymarket():
    global _polymarket
    if _polymarket is None:
        _polymarket = Polymarket()
    return _polymarket


def _get_newsapi():
    global _newsapi_client
    if _newsapi_client is None:
        _newsapi_client = News()
    return _newsapi_client


def _get_rag():
    global _polymarket_rag
    if _polymarket_rag is None:
        _polymarket_rag = PolymarketRAG()
    return _polymarket_rag


# ──────────────────────────────────────────────
#  BOT COMMANDS
# ──────────────────────────────────────────────


@app.command()
def start(
    strategy: str = typer.Option(
        "one_best_trade",
        help=f"Trading strategy to use. Options: {', '.join(STRATEGIES.keys())}",
    ),
    dry_run: bool = typer.Option(
        True, help="Paper trading mode (no real trades)"
    ),
    interval: int = typer.Option(
        300, help="Seconds between trading cycles"
    ),
    model: str = typer.Option(
        "llama-3.3-70b-versatile", help="LLM model to use"
    ),
) -> None:
    """
    Start the autonomous trading bot.

    Runs continuously, analyzing markets and executing trades based on the
    selected strategy. Use --no-dry-run for live trading (use with caution).
    """
    from agents.application.bot import TradingBot

    config = get_config()
    config.dry_run = dry_run
    config.strategy = strategy
    config.trade_interval_seconds = interval
    config.llm_model = model

    bot = TradingBot(config=config)
    bot.start()


@app.command()
def monitor(
    interval: int = typer.Option(
        30, help="Dashboard refresh interval in seconds"
    ),
) -> None:
    """
    Start the live monitoring dashboard.

    Shows real-time positions, P&L, alerts, and risk meters.
    """
    from agents.monitoring.dashboard import LiveDashboard

    config = get_config()
    config.monitor_interval_seconds = interval
    setup_logging(config)

    dashboard = LiveDashboard(config)
    dashboard.start()


@app.command()
def positions() -> None:
    """
    Show current open positions and portfolio summary.
    """
    from agents.monitoring.positions import PositionTracker

    config = get_config()
    tracker = PositionTracker(data_dir=config.data_dir)

    summary = tracker.get_portfolio_summary()

    print("=" * 60)
    print("  PORTFOLIO SUMMARY")
    print("=" * 60)
    print(f"  Open Positions:    {summary['open_positions']}")
    print(f"  Total Exposure:    ${summary['total_exposure_usdc']:,.2f}")
    print(f"  Unrealized PnL:    ${summary['total_unrealized_pnl']:+.2f}")
    print(f"  Realized PnL:      ${summary['total_realized_pnl']:+.2f}")
    print(f"  Daily PnL:         ${summary['daily_pnl']:+.2f}")
    print(f"  Daily Trades:      {summary['daily_trades']}")
    print(f"  Total Trades:      {summary['total_trades']}")
    print("=" * 60)

    if tracker.positions:
        print()
        print(f"  {'Market':<35} {'Side':<5} {'Entry':>7} {'Size':>7} {'PnL':>10}")
        print(f"  {'-'*65}")
        for pos in tracker.positions.values():
            q = pos.question[:33] + ".." if len(pos.question) > 35 else pos.question
            print(
                f"  {q:<35} {pos.side:<5} "
                f"${pos.entry_price:>6.4f} ${pos.size:>6.2f} ${pos.unrealized_pnl:>+9.2f}"
            )


@app.command()
def trades(limit: int = typer.Option(20, help="Number of recent trades to show")) -> None:
    """
    Show recent trade history.
    """
    from agents.monitoring.positions import PositionTracker

    config = get_config()
    tracker = PositionTracker(data_dir=config.data_dir)

    recent = tracker.trade_history[-limit:]
    if not recent:
        print("No trades recorded yet.")
        return

    print(f"  {'Time':<20} {'Market':<25} {'Side':<5} {'Price':>7} {'Size':>7} {'PnL':>10}")
    print(f"  {'-'*75}")
    for trade in reversed(recent):
        ts = datetime.fromtimestamp(trade.timestamp).strftime("%Y-%m-%d %H:%M")
        q = trade.question[:23] + ".." if len(trade.question) > 25 else trade.question
        pnl = f"${trade.pnl:+.2f}" if trade.pnl is not None else "--"
        dry = " (paper)" if trade.dry_run else ""
        print(
            f"  {ts:<20} {q:<25} {trade.side:<5} "
            f"${trade.price:>6.4f} ${trade.size:>6.2f} {pnl:>10}{dry}"
        )


@app.command()
def balance() -> None:
    """
    Check wallet USDC balance.
    """
    poly = _get_polymarket()
    bal = poly.get_usdc_balance()
    print(f"Wallet USDC Balance: ${bal:,.6f}")


@app.command()
def status() -> None:
    """
    Show bot configuration and status.
    """
    config = get_config()
    print("=" * 50)
    print("  BOT CONFIGURATION")
    print("=" * 50)
    print(f"  Mode:             {'DRY RUN' if config.dry_run else 'LIVE'}")
    print(f"  Strategy:         {config.strategy}")
    print(f"  LLM Model:        {config.llm_model}")
    print(f"  Trade Interval:   {config.trade_interval_seconds}s")
    print(f"  Monitor Interval: {config.monitor_interval_seconds}s")
    print()
    print("  RISK PARAMETERS")
    print(f"  Max Position:     ${config.risk.max_position_size_usdc:.2f}")
    print(f"  Max Exposure:     ${config.risk.max_total_exposure_usdc:.2f}")
    print(f"  Max Daily Trades: {config.risk.max_trades_per_day}")
    print(f"  Max Daily Loss:   ${config.risk.max_daily_loss_usdc:.2f}")
    print(f"  Min Confidence:   {config.risk.min_confidence_threshold:.2f}")
    print(f"  Position Size:    {config.risk.position_size_pct:.1%} of balance")
    print(f"  Stop Loss:        {config.risk.stop_loss_pct:.1%}")
    print(f"  Take Profit:      {config.risk.take_profit_pct:.1%}")
    print("=" * 50)


@app.command()
def run_once(
    strategy: str = typer.Option("one_best_trade", help="Strategy to use"),
    dry_run: bool = typer.Option(True, help="Paper trading mode"),
) -> None:
    """
    Run a single trading cycle (useful for testing).
    """
    from agents.application.bot import TradingBot

    config = get_config()
    config.dry_run = dry_run
    config.strategy = strategy

    bot = TradingBot(config=config)
    result = bot.run_once()
    print(json.dumps(result, indent=2))


# ──────────────────────────────────────────────
#  ORIGINAL COMMANDS (preserved)
# ──────────────────────────────────────────────


@app.command()
def get_all_markets(limit: int = 5, sort_by: str = "spread") -> None:
    """
    Query Polymarket's markets.
    """
    polymarket = _get_polymarket()
    markets = polymarket.get_all_markets()
    markets = polymarket.filter_markets_for_trading(markets)
    if sort_by == "spread":
        markets = sorted(markets, key=lambda x: x.spread, reverse=True)
    markets = markets[:limit]
    pprint(markets)


@app.command()
def get_relevant_news(keywords: str) -> None:
    """
    Use NewsAPI to query the internet.
    """
    newsapi_client = _get_newsapi()
    articles = newsapi_client.get_articles_for_cli_keywords(keywords)
    pprint(articles)


@app.command()
def get_all_events(limit: int = 5, sort_by: str = "number_of_markets") -> None:
    """
    Query Polymarket's events.
    """
    polymarket = _get_polymarket()
    events = polymarket.get_all_events()
    events = polymarket.filter_events_for_trading(events)
    if sort_by == "number_of_markets":
        events = sorted(events, key=lambda x: len(x.markets), reverse=True)
    events = events[:limit]
    pprint(events)


@app.command()
def create_local_markets_rag(local_directory: str) -> None:
    """
    Create a local markets database for RAG.
    """
    polymarket_rag = _get_rag()
    polymarket_rag.create_local_markets_rag(local_directory=local_directory)


@app.command()
def query_local_markets_rag(vector_db_directory: str, query: str) -> None:
    """
    RAG over a local database of Polymarket's events.
    """
    polymarket_rag = _get_rag()
    response = polymarket_rag.query_local_markets_rag(
        local_directory=vector_db_directory, query=query
    )
    pprint(response)


@app.command()
def ask_superforecaster(event_title: str, market_question: str, outcome: str) -> None:
    """
    Ask a superforecaster about a trade.
    """
    executor = Executor()
    response = executor.get_superforecast(
        event_title=event_title, market_question=market_question, outcome=outcome
    )
    print(f"Response: {response}")


@app.command()
def create_market() -> None:
    """
    Format a request to create a market on Polymarket.
    """
    c = Creator()
    market_description = c.one_best_market()
    print(f"market_description: {market_description}")


@app.command()
def ask_llm(user_input: str) -> None:
    """
    Ask a question to the LLM and get a response.
    """
    executor = Executor()
    response = executor.get_llm_response(user_input)
    print(f"LLM Response: {response}")


@app.command()
def ask_polymarket_llm(user_input: str) -> None:
    """
    Ask LLM with current market & event data context.
    """
    executor = Executor()
    response = executor.get_polymarket_llm(user_input=user_input)
    print(f"LLM + current markets & events response: {response}")


@app.command()
def run_autonomous_trader() -> None:
    """
    Let an autonomous system trade for you (legacy command).
    """
    trader = Trader()
    trader.one_best_trade()


if __name__ == "__main__":
    app()
