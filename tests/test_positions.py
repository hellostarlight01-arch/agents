"""Tests for the position tracker."""

import os
import shutil
import time
import unittest

from agents.monitoring.positions import Position, TradeRecord, PositionTracker


class TestPosition(unittest.TestCase):
    def test_create_position(self):
        pos = Position(
            market_id="123",
            token_id="abc",
            question="Will it rain?",
            outcome="Yes",
            side="BUY",
            entry_price=0.65,
            size=10.0,
            shares=15.38,
            opened_at=time.time(),
        )
        self.assertEqual(pos.side, "BUY")
        self.assertEqual(pos.entry_price, 0.65)

    def test_update_price_buy(self):
        pos = Position(
            market_id="123",
            token_id="abc",
            question="Test",
            outcome="Yes",
            side="BUY",
            entry_price=0.50,
            size=50.0,
            shares=100.0,
        )
        pos.update_price(0.60)
        self.assertAlmostEqual(pos.unrealized_pnl, 10.0)
        self.assertAlmostEqual(pos.unrealized_pnl_pct, 0.20)

    def test_update_price_sell(self):
        pos = Position(
            market_id="123",
            token_id="abc",
            question="Test",
            outcome="No",
            side="SELL",
            entry_price=0.50,
            size=50.0,
            shares=100.0,
        )
        pos.update_price(0.40)
        self.assertAlmostEqual(pos.unrealized_pnl, 10.0)

    def test_to_dict_roundtrip(self):
        pos = Position(
            market_id="123",
            token_id="abc",
            question="Test",
            outcome="Yes",
            side="BUY",
            entry_price=0.50,
            size=50.0,
            shares=100.0,
        )
        d = pos.to_dict()
        pos2 = Position.from_dict(d)
        self.assertEqual(pos.market_id, pos2.market_id)
        self.assertEqual(pos.entry_price, pos2.entry_price)


class TestPositionTracker(unittest.TestCase):
    TEST_DATA_DIR = "./test_data_positions"

    def setUp(self):
        os.makedirs(self.TEST_DATA_DIR, exist_ok=True)
        self.tracker = PositionTracker(data_dir=self.TEST_DATA_DIR)

    def tearDown(self):
        shutil.rmtree(self.TEST_DATA_DIR, ignore_errors=True)

    def test_open_position(self):
        pos = self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Will it rain?",
            outcome="Yes",
            side="BUY",
            price=0.65,
            size=10.0,
        )
        self.assertEqual(pos.side, "BUY")
        self.assertEqual(len(self.tracker.positions), 1)
        self.assertEqual(len(self.tracker.trade_history), 1)

    def test_close_position(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=50.0,
        )
        trade = self.tracker.close_position("tok1", close_price=0.60)
        self.assertIsNotNone(trade)
        self.assertEqual(len(self.tracker.positions), 0)
        self.assertEqual(len(self.tracker.trade_history), 2)
        self.assertGreater(trade.pnl, 0)

    def test_portfolio_summary(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=50.0,
        )
        summary = self.tracker.get_portfolio_summary()
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(summary["total_exposure_usdc"], 50.0)

    def test_check_stop_loss(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=50.0,
        )
        self.tracker.update_position_prices({"tok1": 0.40})
        triggered = self.tracker.check_stop_loss(0.15)
        self.assertEqual(len(triggered), 1)

    def test_check_take_profit(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=50.0,
        )
        self.tracker.update_position_prices({"tok1": 0.70})
        triggered = self.tracker.check_take_profit(0.30)
        self.assertEqual(len(triggered), 1)

    def test_persistence(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=50.0,
        )
        # Create a new tracker from the same data dir
        tracker2 = PositionTracker(data_dir=self.TEST_DATA_DIR)
        self.assertEqual(len(tracker2.positions), 1)
        self.assertEqual(len(tracker2.trade_history), 1)

    def test_total_exposure(self):
        self.tracker.open_position(
            trade_id="t1",
            market_id="m1",
            token_id="tok1",
            question="Test 1",
            outcome="Yes",
            side="BUY",
            price=0.50,
            size=30.0,
        )
        self.tracker.open_position(
            trade_id="t2",
            market_id="m2",
            token_id="tok2",
            question="Test 2",
            outcome="No",
            side="SELL",
            price=0.40,
            size=20.0,
        )
        self.assertAlmostEqual(self.tracker.get_total_exposure(), 50.0)


if __name__ == "__main__":
    unittest.main()
