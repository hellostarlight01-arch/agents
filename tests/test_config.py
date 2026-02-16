"""Tests for the configuration system."""

import os
import unittest
from unittest.mock import patch

from agents.config import BotConfig, RiskConfig, get_config, setup_logging


class TestRiskConfig(unittest.TestCase):
    def test_defaults(self):
        risk = RiskConfig()
        self.assertEqual(risk.max_position_size_usdc, 50.0)
        self.assertEqual(risk.max_total_exposure_usdc, 200.0)
        self.assertEqual(risk.max_trades_per_day, 10)
        self.assertEqual(risk.stop_loss_pct, 0.15)
        self.assertEqual(risk.take_profit_pct, 0.30)
        self.assertEqual(risk.min_confidence_threshold, 0.6)


class TestBotConfig(unittest.TestCase):
    def test_defaults(self):
        config = BotConfig()
        self.assertTrue(config.dry_run)
        self.assertEqual(config.strategy, "one_best_trade")
        self.assertEqual(config.trade_interval_seconds, 300)
        self.assertEqual(config.monitor_interval_seconds, 30)
        self.assertEqual(config.llm_model, "gpt-3.5-turbo-16k")

    @patch.dict(os.environ, {
        "BOT_DRY_RUN": "false",
        "BOT_STRATEGY": "market_scan",
        "BOT_TRADE_INTERVAL": "120",
        "RISK_MAX_POSITION_SIZE": "100.0",
        "RISK_MAX_TRADES_PER_DAY": "20",
    })
    def test_get_config_from_env(self):
        config = get_config()
        self.assertFalse(config.dry_run)
        self.assertEqual(config.strategy, "market_scan")
        self.assertEqual(config.trade_interval_seconds, 120)
        self.assertEqual(config.risk.max_position_size_usdc, 100.0)
        self.assertEqual(config.risk.max_trades_per_day, 20)


class TestSetupLogging(unittest.TestCase):
    def test_logger_created(self):
        config = BotConfig(log_file=None)  # no file output in tests
        logger = setup_logging(config)
        self.assertEqual(logger.name, "polybot")


if __name__ == "__main__":
    unittest.main()
