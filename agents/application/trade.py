import logging
import shutil

from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket

logger = logging.getLogger("polybot.trader")

MAX_RETRIES = 3


class Trader:
    def __init__(self):
        self.polymarket = Polymarket()
        self.gamma = Gamma()
        self.agent = Agent()

    def pre_trade_logic(self) -> None:
        self.clear_local_dbs()

    def clear_local_dbs(self) -> None:
        try:
            shutil.rmtree("local_db_events")
        except OSError:
            pass
        try:
            shutil.rmtree("local_db_markets")
        except OSError:
            pass

    def one_best_trade(self, retries: int = 0) -> None:
        """
        one_best_trade is a strategy that evaluates all events, markets, and orderbooks,
        leverages all available information sources accessible to the autonomous agent,
        then executes that trade without any human intervention.
        """
        try:
            self.pre_trade_logic()

            events = self.polymarket.get_all_tradeable_events()
            logger.info(f"1. FOUND {len(events)} EVENTS")

            filtered_events = self.agent.filter_events_with_rag(events)
            logger.info(f"2. FILTERED {len(filtered_events)} EVENTS")

            markets = self.agent.map_filtered_events_to_markets(filtered_events)
            logger.info(f"3. FOUND {len(markets)} MARKETS")

            filtered_markets = self.agent.filter_markets(markets)
            logger.info(f"4. FILTERED {len(filtered_markets)} MARKETS")

            market = filtered_markets[0]
            best_trade = self.agent.source_best_trade(market)
            logger.info(f"5. CALCULATED TRADE {best_trade}")

            amount = self.agent.format_trade_prompt_for_execution(best_trade)
            logger.info(f"6. TRADE AMOUNT: ${amount:.2f}")

            # Please refer to TOS before uncommenting: polymarket.com/tos
            # trade = self.polymarket.execute_market_order(market, amount)
            # logger.info(f"7. TRADED {trade}")

        except Exception as e:
            logger.error(f"Error: {e}")
            if retries < MAX_RETRIES:
                logger.info(f"Retrying ({retries + 1}/{MAX_RETRIES})...")
                self.one_best_trade(retries=retries + 1)
            else:
                logger.error("Max retries reached. Giving up.")

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
