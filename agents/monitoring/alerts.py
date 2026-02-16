"""
Alert system for the Polymarket trading bot.
Monitors conditions and triggers alerts.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("polybot.alerts")


@dataclass
class Alert:
    """Represents a triggered alert."""

    alert_type: str  # price_move, position_pnl, risk_limit, market_event
    severity: str  # info, warning, critical
    message: str
    timestamp: float = 0.0
    market_id: str = ""
    token_id: str = ""
    value: float = 0.0
    threshold: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class AlertRule:
    """A rule that triggers alerts when conditions are met."""

    name: str
    alert_type: str
    severity: str
    check_fn: Callable[[], Optional[Alert]]
    cooldown_seconds: float = 60.0
    last_triggered: float = 0.0
    enabled: bool = True


class AlertManager:
    """Manages alert rules and triggered alerts."""

    def __init__(self, max_history: int = 100) -> None:
        self.rules: list[AlertRule] = []
        self.alert_history: list[Alert] = []
        self.max_history = max_history
        self.callbacks: list[Callable[[Alert], None]] = []

    def add_rule(self, rule: AlertRule) -> None:
        """Add an alert rule."""
        self.rules.append(rule)

    def add_callback(self, callback: Callable[[Alert], None]) -> None:
        """Add a callback to be invoked when an alert fires."""
        self.callbacks.append(callback)

    def check_all(self) -> list[Alert]:
        """Check all rules and return triggered alerts."""
        triggered = []
        now = time.time()

        for rule in self.rules:
            if not rule.enabled:
                continue

            if now - rule.last_triggered < rule.cooldown_seconds:
                continue

            try:
                alert = rule.check_fn()
                if alert is not None:
                    rule.last_triggered = now
                    triggered.append(alert)
                    self.alert_history.append(alert)

                    for cb in self.callbacks:
                        try:
                            cb(alert)
                        except Exception as e:
                            logger.error(f"Alert callback error: {e}")

            except Exception as e:
                logger.error(f"Error checking rule '{rule.name}': {e}")

        # Trim history
        if len(self.alert_history) > self.max_history:
            self.alert_history = self.alert_history[-self.max_history :]

        return triggered

    def get_recent_alerts(self, count: int = 10) -> list[Alert]:
        """Get the most recent alerts."""
        return self.alert_history[-count:]


def create_position_alerts(tracker, risk_config) -> list[AlertRule]:
    """Create standard alert rules for position monitoring."""
    rules = []

    # Large loss alert
    def check_large_loss():
        for pos in tracker.positions.values():
            if pos.unrealized_pnl_pct <= -0.10:  # 10% loss
                return Alert(
                    alert_type="position_pnl",
                    severity="warning",
                    message=f"Position down {pos.unrealized_pnl_pct:.1%}: {pos.question}",
                    token_id=pos.token_id,
                    value=pos.unrealized_pnl,
                    threshold=-0.10,
                )
        return None

    rules.append(
        AlertRule(
            name="large_loss",
            alert_type="position_pnl",
            severity="warning",
            check_fn=check_large_loss,
            cooldown_seconds=300,
        )
    )

    # Critical loss alert
    def check_critical_loss():
        for pos in tracker.positions.values():
            if pos.unrealized_pnl_pct <= -risk_config.stop_loss_pct:
                return Alert(
                    alert_type="position_pnl",
                    severity="critical",
                    message=f"STOP LOSS level hit ({pos.unrealized_pnl_pct:.1%}): {pos.question}",
                    token_id=pos.token_id,
                    value=pos.unrealized_pnl,
                    threshold=-risk_config.stop_loss_pct,
                )
        return None

    rules.append(
        AlertRule(
            name="critical_loss",
            alert_type="position_pnl",
            severity="critical",
            check_fn=check_critical_loss,
            cooldown_seconds=60,
        )
    )

    # Take profit alert
    def check_take_profit():
        for pos in tracker.positions.values():
            if pos.unrealized_pnl_pct >= risk_config.take_profit_pct:
                return Alert(
                    alert_type="position_pnl",
                    severity="info",
                    message=f"Take profit level ({pos.unrealized_pnl_pct:.1%}): {pos.question}",
                    token_id=pos.token_id,
                    value=pos.unrealized_pnl,
                    threshold=risk_config.take_profit_pct,
                )
        return None

    rules.append(
        AlertRule(
            name="take_profit",
            alert_type="position_pnl",
            severity="info",
            check_fn=check_take_profit,
            cooldown_seconds=300,
        )
    )

    # Exposure limit alert
    def check_exposure():
        exposure = tracker.get_total_exposure()
        limit = risk_config.max_total_exposure_usdc
        if exposure >= limit * 0.8:
            return Alert(
                alert_type="risk_limit",
                severity="warning",
                message=f"Exposure at {exposure / limit:.0%} of limit (${exposure:.2f} / ${limit:.2f})",
                value=exposure,
                threshold=limit,
            )
        return None

    rules.append(
        AlertRule(
            name="exposure_limit",
            alert_type="risk_limit",
            severity="warning",
            check_fn=check_exposure,
            cooldown_seconds=600,
        )
    )

    # Daily loss limit alert
    def check_daily_loss():
        if tracker.daily_pnl <= -risk_config.max_daily_loss_usdc * 0.8:
            return Alert(
                alert_type="risk_limit",
                severity="critical",
                message=f"Approaching daily loss limit: ${tracker.daily_pnl:.2f} / -${risk_config.max_daily_loss_usdc:.2f}",
                value=tracker.daily_pnl,
                threshold=-risk_config.max_daily_loss_usdc,
            )
        return None

    rules.append(
        AlertRule(
            name="daily_loss",
            alert_type="risk_limit",
            severity="critical",
            check_fn=check_daily_loss,
            cooldown_seconds=300,
        )
    )

    return rules
