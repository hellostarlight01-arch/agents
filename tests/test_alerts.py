"""Tests for the alert system."""

import os
import shutil
import unittest

from agents.config import RiskConfig
from agents.monitoring.positions import PositionTracker
from agents.monitoring.alerts import (
    Alert,
    AlertManager,
    AlertRule,
    create_position_alerts,
)


class TestAlert(unittest.TestCase):
    def test_create_alert(self):
        alert = Alert(
            alert_type="position_pnl",
            severity="warning",
            message="Test alert",
        )
        self.assertEqual(alert.alert_type, "position_pnl")
        self.assertGreater(alert.timestamp, 0)


class TestAlertManager(unittest.TestCase):
    def test_add_and_check_rule(self):
        manager = AlertManager()
        triggered_alerts = []

        def check():
            return Alert(
                alert_type="test",
                severity="info",
                message="Always fires",
            )

        rule = AlertRule(
            name="test_rule",
            alert_type="test",
            severity="info",
            check_fn=check,
            cooldown_seconds=0,
        )
        manager.add_rule(rule)
        alerts = manager.check_all()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].message, "Always fires")

    def test_cooldown(self):
        manager = AlertManager()

        def check():
            return Alert(
                alert_type="test",
                severity="info",
                message="Test",
            )

        rule = AlertRule(
            name="test_rule",
            alert_type="test",
            severity="info",
            check_fn=check,
            cooldown_seconds=600,  # long cooldown
        )
        manager.add_rule(rule)

        # First check should fire
        alerts1 = manager.check_all()
        self.assertEqual(len(alerts1), 1)

        # Second check should not fire (cooldown)
        alerts2 = manager.check_all()
        self.assertEqual(len(alerts2), 0)

    def test_callback(self):
        manager = AlertManager()
        callback_results = []

        def check():
            return Alert(alert_type="test", severity="info", message="cb test")

        def on_alert(alert):
            callback_results.append(alert.message)

        rule = AlertRule(
            name="test",
            alert_type="test",
            severity="info",
            check_fn=check,
            cooldown_seconds=0,
        )
        manager.add_rule(rule)
        manager.add_callback(on_alert)
        manager.check_all()
        self.assertEqual(len(callback_results), 1)

    def test_no_alert_returns_none(self):
        manager = AlertManager()

        def check():
            return None  # no alert condition

        rule = AlertRule(
            name="quiet",
            alert_type="test",
            severity="info",
            check_fn=check,
            cooldown_seconds=0,
        )
        manager.add_rule(rule)
        alerts = manager.check_all()
        self.assertEqual(len(alerts), 0)


class TestPositionAlerts(unittest.TestCase):
    TEST_DATA_DIR = "./test_data_alerts"

    def setUp(self):
        os.makedirs(self.TEST_DATA_DIR, exist_ok=True)
        self.tracker = PositionTracker(data_dir=self.TEST_DATA_DIR)
        self.risk_config = RiskConfig()

    def tearDown(self):
        shutil.rmtree(self.TEST_DATA_DIR, ignore_errors=True)

    def test_creates_rules(self):
        rules = create_position_alerts(self.tracker, self.risk_config)
        self.assertGreater(len(rules), 0)
        rule_names = [r.name for r in rules]
        self.assertIn("large_loss", rule_names)
        self.assertIn("critical_loss", rule_names)
        self.assertIn("take_profit", rule_names)
        self.assertIn("exposure_limit", rule_names)
        self.assertIn("daily_loss", rule_names)

    def test_large_loss_alert_fires(self):
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
        # Set price to trigger a >10% loss
        self.tracker.update_position_prices({"tok1": 0.40})

        rules = create_position_alerts(self.tracker, self.risk_config)
        manager = AlertManager()
        for rule in rules:
            manager.add_rule(rule)

        alerts = manager.check_all()
        loss_alerts = [a for a in alerts if a.alert_type == "position_pnl"]
        self.assertGreater(len(loss_alerts), 0)


if __name__ == "__main__":
    unittest.main()
