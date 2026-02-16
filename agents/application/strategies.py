"""
Trading strategy framework for the Polymarket bot.
Strategies analyze markets and produce trade signals.
"""

import ast
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from agents.config import BotConfig
from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.application.executor import Executor
from agents.monitoring.positions import PositionTracker

logger = logging.getLogger("polybot.strategies")


@dataclass
class TradeSignal:
    """A trade signal produced by a strategy."""

    market_id: str
    token_id: str
    question: str
    outcome: str
    side: str  # BUY or SELL
    price: float
    size_pct: float  # % of balance to use
    confidence: float  # 0.0 to 1.0
    reasoning: str = ""
    strategy_name: str = ""


class BaseStrategy(ABC):
    """Base class for all trading strategies."""

    name: str = "base"

    def __init__(self, config: BotConfig, polymarket: Polymarket) -> None:
        self.config = config
        self.polymarket = polymarket

    @abstractmethod
    def generate_signals(self) -> list[TradeSignal]:
        """Analyze markets and generate trade signals."""
        ...

    def validate_signal(self, signal: TradeSignal) -> bool:
        """Validate a trade signal against risk parameters."""
        if signal.confidence < self.config.risk.min_confidence_threshold:
            logger.info(
                f"Signal rejected (low confidence {signal.confidence:.2f}): "
                f"{signal.question}"
            )
            return False

        if signal.price <= 0 or signal.price >= 1:
            logger.info(
                f"Signal rejected (invalid price {signal.price}): "
                f"{signal.question}"
            )
            return False

        return True


class OneBestTradeStrategy(BaseStrategy):
    """
    The original 'one best trade' strategy.
    Uses RAG filtering + LLM superforecasting to find the single best trade.
    """

    name = "one_best_trade"

    def __init__(self, config: BotConfig, polymarket: Polymarket) -> None:
        super().__init__(config, polymarket)
        self.gamma = Gamma()
        self.executor = Executor(default_model=config.llm_model)

    def generate_signals(self) -> list[TradeSignal]:
        signals = []

        try:
            # 1. Get tradeable events
            events = self.polymarket.get_all_tradeable_events()
            logger.info(f"Found {len(events)} tradeable events")

            if not events:
                logger.warning("No tradeable events found")
                return signals

            # 2. Filter events with RAG
            filtered_events = self.executor.filter_events_with_rag(events)
            logger.info(f"RAG filtered to {len(filtered_events)} events")

            if not filtered_events:
                logger.warning("No events passed RAG filter")
                return signals

            # 3. Map to markets
            markets = self.executor.map_filtered_events_to_markets(
                filtered_events
            )
            logger.info(f"Mapped to {len(markets)} markets")

            if not markets:
                logger.warning("No markets found for filtered events")
                return signals

            # 4. Filter markets
            filtered_markets = self.executor.filter_markets(markets)
            logger.info(f"Filtered to {len(filtered_markets)} markets")

            if not filtered_markets:
                logger.warning("No markets passed filter")
                return signals

            # 5. Source best trade
            market = filtered_markets[0]
            best_trade = self.executor.source_best_trade(market)
            logger.info(f"Best trade signal: {best_trade}")

            # 6. Parse the trade signal
            signal = self._parse_trade_signal(best_trade, market)
            if signal and self.validate_signal(signal):
                signals.append(signal)

        except Exception as e:
            logger.error(f"Error in OneBestTrade strategy: {e}", exc_info=True)

        return signals

    def _parse_trade_signal(
        self, trade_text: str, market_object
    ) -> Optional[TradeSignal]:
        """Parse LLM trade output into a TradeSignal."""
        try:
            market_doc = market_object[0].dict() if hasattr(market_object[0], 'dict') else market_object[0]
            if isinstance(market_doc, dict) and "metadata" in market_doc:
                metadata = market_doc["metadata"]
            else:
                metadata = market_doc

            question = metadata.get("question", "Unknown")
            outcomes = metadata.get("outcomes", "[]")
            outcome_prices = metadata.get("outcome_prices", "[]")
            token_ids = metadata.get("clob_token_ids", "[]")
            market_id = str(metadata.get("id", ""))

            # Parse price from LLM output
            price_match = re.search(r"price[:\s]*([0-9]*\.?[0-9]+)", trade_text)
            size_match = re.search(r"size[:\s]*([0-9]*\.?[0-9]+)", trade_text)
            side_match = re.search(r"side[:\s]*(BUY|SELL)", trade_text, re.IGNORECASE)

            if not price_match:
                logger.warning("Could not parse price from trade signal")
                return None

            price = float(price_match.group(1))
            size_pct = float(size_match.group(1)) if size_match else 0.05
            side = side_match.group(1).upper() if side_match else "BUY"

            # Determine outcome based on side
            try:
                outcomes_list = ast.literal_eval(outcomes) if isinstance(outcomes, str) else outcomes
                token_ids_list = ast.literal_eval(token_ids) if isinstance(token_ids, str) else token_ids
            except (ValueError, SyntaxError):
                outcomes_list = ["Yes", "No"]
                token_ids_list = []

            outcome = outcomes_list[0] if side == "BUY" else outcomes_list[-1]
            token_id = token_ids_list[0] if token_ids_list else ""

            return TradeSignal(
                market_id=market_id,
                token_id=token_id,
                question=question,
                outcome=outcome,
                side=side,
                price=price,
                size_pct=size_pct,
                confidence=price if side == "BUY" else (1 - price),
                reasoning=trade_text,
                strategy_name=self.name,
            )

        except Exception as e:
            logger.error(f"Failed to parse trade signal: {e}", exc_info=True)
            return None


class MarketScanStrategy(BaseStrategy):
    """
    Scans all markets for statistical mispricings.
    Looks for markets where orderbook prices diverge from model estimates.
    """

    name = "market_scan"

    def __init__(self, config: BotConfig, polymarket: Polymarket) -> None:
        super().__init__(config, polymarket)
        self.gamma = Gamma()
        self.executor = Executor(default_model=config.llm_model)

    def generate_signals(self) -> list[TradeSignal]:
        signals = []

        try:
            markets = self.polymarket.get_all_markets()
            tradeable = self.polymarket.filter_markets_for_trading(markets)
            logger.info(f"Scanning {len(tradeable)} tradeable markets")

            for market in tradeable[:20]:  # limit scan to top 20
                try:
                    signal = self._analyze_market(market)
                    if signal and self.validate_signal(signal):
                        signals.append(signal)
                except Exception as e:
                    logger.debug(f"Error analyzing market {market.id}: {e}")
                    continue

            # Sort by confidence
            signals.sort(key=lambda s: s.confidence, reverse=True)

        except Exception as e:
            logger.error(f"Error in MarketScan strategy: {e}", exc_info=True)

        return signals[:5]  # return top 5 signals

    def _analyze_market(self, market) -> Optional[TradeSignal]:
        """Analyze a single market for trading opportunity."""
        try:
            outcome_prices = ast.literal_eval(market.outcome_prices)
            outcomes = ast.literal_eval(market.outcomes)
            token_ids = ast.literal_eval(market.clob_token_ids) if market.clob_token_ids else []
        except (ValueError, SyntaxError):
            return None

        if len(outcome_prices) < 2 or len(token_ids) < 1:
            return None

        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1])

        # Look for markets where the spread is wide (potential opportunity)
        if market.spread < 0.03:
            return None  # skip tight markets

        # Look for extreme pricing as potential value
        if yes_price < 0.15 or yes_price > 0.85:
            # Use LLM to evaluate
            forecast = self.executor.get_superforecast(
                event_title=market.question,
                market_question=market.question,
                outcome=outcomes[0],
            )

            # Try to extract probability from forecast
            prob_match = re.search(
                r"likelihood[:\s]*`?([0-9]*\.?[0-9]+)`?", forecast
            )
            if prob_match:
                estimated_prob = float(prob_match.group(1))
                # Normalize if needed (some outputs are 0-100)
                if estimated_prob > 1:
                    estimated_prob = estimated_prob / 100

                edge = estimated_prob - yes_price

                if abs(edge) >= 0.10:  # minimum 10% edge
                    side = "BUY" if edge > 0 else "SELL"
                    confidence = min(abs(edge), 0.95)

                    return TradeSignal(
                        market_id=str(market.id),
                        token_id=token_ids[0],
                        question=market.question,
                        outcome=outcomes[0] if side == "BUY" else outcomes[-1],
                        side=side,
                        price=yes_price,
                        size_pct=self.config.risk.position_size_pct,
                        confidence=confidence,
                        reasoning=f"Model: {estimated_prob:.2f} vs Market: {yes_price:.2f} "
                        f"(edge: {edge:+.2f}). Forecast: {forecast[:200]}",
                        strategy_name=self.name,
                    )

        return None


class SentimentStrategy(BaseStrategy):
    """
    News and sentiment-based trading strategy.
    Uses news data and LLM sentiment analysis to find trades.
    """

    name = "sentiment"

    def __init__(self, config: BotConfig, polymarket: Polymarket) -> None:
        super().__init__(config, polymarket)
        self.executor = Executor(default_model=config.llm_model)

    def generate_signals(self) -> list[TradeSignal]:
        signals = []

        try:
            from agents.connectors.news import News

            news_client = News()
            markets = self.polymarket.get_all_markets()
            tradeable = self.polymarket.filter_markets_for_trading(markets)

            # Focus on markets with significant volume/spread
            interesting = [
                m for m in tradeable if m.spread >= 0.05
            ][:10]

            for market in interesting:
                try:
                    outcomes = ast.literal_eval(market.outcomes)
                    keywords = market.question.split()[:5]  # first 5 words
                    keyword_str = " ".join(keywords)

                    articles = news_client.get_articles_for_cli_keywords(
                        keyword_str
                    )

                    if not articles:
                        continue

                    # Use LLM to analyze sentiment
                    article_text = "\n".join(
                        [
                            f"- {a.title}: {a.description}"
                            for a in articles[:5]
                            if a.title
                        ]
                    )

                    prompt = (
                        f"Based on these recent news articles:\n{article_text}\n\n"
                        f"What is the probability (0.0-1.0) that: {market.question}\n"
                        f"Outcome: {outcomes[0]}\n"
                        f"Respond with only a number between 0.0 and 1.0."
                    )

                    response = self.executor.get_llm_response(prompt)
                    prob_match = re.search(r"([0-9]*\.?[0-9]+)", response)

                    if prob_match:
                        estimated_prob = float(prob_match.group(1))
                        if estimated_prob > 1:
                            estimated_prob /= 100

                        outcome_prices = ast.literal_eval(market.outcome_prices)
                        market_price = float(outcome_prices[0])
                        edge = estimated_prob - market_price

                        if abs(edge) >= 0.10:
                            side = "BUY" if edge > 0 else "SELL"
                            token_ids = (
                                ast.literal_eval(market.clob_token_ids)
                                if market.clob_token_ids
                                else []
                            )

                            signals.append(
                                TradeSignal(
                                    market_id=str(market.id),
                                    token_id=token_ids[0] if token_ids else "",
                                    question=market.question,
                                    outcome=outcomes[0],
                                    side=side,
                                    price=market_price,
                                    size_pct=self.config.risk.position_size_pct,
                                    confidence=min(abs(edge), 0.95),
                                    reasoning=f"Sentiment edge: {edge:+.2f}. "
                                    f"News-based estimate: {estimated_prob:.2f}",
                                    strategy_name=self.name,
                                )
                            )

                except Exception as e:
                    logger.debug(f"Error analyzing market {market.id}: {e}")
                    continue

        except Exception as e:
            logger.error(
                f"Error in Sentiment strategy: {e}", exc_info=True
            )

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals[:3]


# Strategy registry
STRATEGIES: dict[str, type[BaseStrategy]] = {
    "one_best_trade": OneBestTradeStrategy,
    "market_scan": MarketScanStrategy,
    "sentiment": SentimentStrategy,
}


def get_strategy(name: str, config: BotConfig, polymarket: Polymarket) -> BaseStrategy:
    """Get a strategy instance by name."""
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy: {name}. Available: {list(STRATEGIES.keys())}"
        )
    return STRATEGIES[name](config, polymarket)
