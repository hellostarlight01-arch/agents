"""
Position tracker and portfolio management for the Polymarket trading bot.
Tracks open positions, trade history, and calculates P&L.
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("polybot.positions")


@dataclass
class Position:
    """Represents an open position in a market."""

    market_id: str
    token_id: str
    question: str
    outcome: str
    side: str  # BUY or SELL
    entry_price: float
    size: float  # USDC amount
    shares: float  # number of shares
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    opened_at: float = 0.0  # timestamp
    last_updated: float = 0.0

    def update_price(self, new_price: float) -> None:
        """Update current price and recalculate P&L."""
        self.current_price = new_price
        self.last_updated = time.time()

        if self.side == "BUY":
            self.unrealized_pnl = (new_price - self.entry_price) * self.shares
        else:
            self.unrealized_pnl = (self.entry_price - new_price) * self.shares

        if self.size > 0:
            self.unrealized_pnl_pct = self.unrealized_pnl / self.size
        else:
            self.unrealized_pnl_pct = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(**data)


@dataclass
class TradeRecord:
    """Record of an executed trade."""

    trade_id: str
    market_id: str
    token_id: str
    question: str
    outcome: str
    side: str
    price: float
    size: float
    shares: float
    timestamp: float
    dry_run: bool = True
    status: str = "filled"
    pnl: Optional[float] = None  # set when position is closed
    confidence: float = 0.0
    strategy: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRecord":
        return cls(**data)


class PositionTracker:
    """Manages open positions and trade history."""

    def __init__(self, data_dir: str = "./data") -> None:
        self.data_dir = data_dir
        self.positions_file = os.path.join(data_dir, "positions.json")
        self.trades_file = os.path.join(data_dir, "trades.json")

        self.positions: dict[str, Position] = {}
        self.trade_history: list[TradeRecord] = []
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self._daily_reset_timestamp: float = 0.0

        os.makedirs(data_dir, exist_ok=True)
        self._load_state()

    def _load_state(self) -> None:
        """Load positions and trade history from disk."""
        if os.path.exists(self.positions_file):
            try:
                with open(self.positions_file) as f:
                    data = json.load(f)
                    self.positions = {
                        k: Position.from_dict(v) for k, v in data.items()
                    }
                logger.info(f"Loaded {len(self.positions)} positions from disk")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load positions: {e}")
                self.positions = {}

        if os.path.exists(self.trades_file):
            try:
                with open(self.trades_file) as f:
                    data = json.load(f)
                    self.trade_history = [TradeRecord.from_dict(t) for t in data]
                logger.info(
                    f"Loaded {len(self.trade_history)} trades from history"
                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load trade history: {e}")
                self.trade_history = []

    def _save_state(self) -> None:
        """Persist positions and trade history to disk."""
        try:
            with open(self.positions_file, "w") as f:
                json.dump(
                    {k: v.to_dict() for k, v in self.positions.items()},
                    f,
                    indent=2,
                )

            with open(self.trades_file, "w") as f:
                json.dump(
                    [t.to_dict() for t in self.trade_history],
                    f,
                    indent=2,
                )
        except OSError as e:
            logger.error(f"Failed to save state: {e}")

    def open_position(
        self,
        trade_id: str,
        market_id: str,
        token_id: str,
        question: str,
        outcome: str,
        side: str,
        price: float,
        size: float,
        confidence: float = 0.0,
        strategy: str = "",
        dry_run: bool = True,
    ) -> Position:
        """Record a new position opening."""
        shares = size / price if price > 0 else 0.0
        now = time.time()

        position = Position(
            market_id=market_id,
            token_id=token_id,
            question=question,
            outcome=outcome,
            side=side,
            entry_price=price,
            size=size,
            shares=shares,
            current_price=price,
            opened_at=now,
            last_updated=now,
        )

        self.positions[token_id] = position

        trade = TradeRecord(
            trade_id=trade_id,
            market_id=market_id,
            token_id=token_id,
            question=question,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            shares=shares,
            timestamp=now,
            dry_run=dry_run,
            confidence=confidence,
            strategy=strategy,
        )
        self.trade_history.append(trade)
        self.daily_trades += 1

        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}"
            f"OPENED {side} position: {question} | "
            f"{outcome} @ ${price:.4f} | "
            f"Size: ${size:.2f} ({shares:.2f} shares)"
        )

        self._save_state()
        return position

    def close_position(
        self,
        token_id: str,
        close_price: float,
        dry_run: bool = True,
    ) -> Optional[TradeRecord]:
        """Close a position and record the trade."""
        if token_id not in self.positions:
            logger.warning(f"No open position for token {token_id}")
            return None

        position = self.positions[token_id]
        position.update_price(close_price)

        pnl = position.unrealized_pnl
        self.daily_pnl += pnl

        close_side = "SELL" if position.side == "BUY" else "BUY"
        trade = TradeRecord(
            trade_id=f"close-{token_id}-{int(time.time())}",
            market_id=position.market_id,
            token_id=token_id,
            question=position.question,
            outcome=position.outcome,
            side=close_side,
            price=close_price,
            size=position.shares * close_price,
            shares=position.shares,
            timestamp=time.time(),
            dry_run=dry_run,
            pnl=pnl,
            notes=f"Closed position. Entry: ${position.entry_price:.4f}, "
            f"Exit: ${close_price:.4f}, PnL: ${pnl:.2f}",
        )

        self.trade_history.append(trade)
        del self.positions[token_id]

        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}"
            f"CLOSED position: {position.question} | "
            f"PnL: ${pnl:+.2f} ({position.unrealized_pnl_pct:+.1%})"
        )

        self._save_state()
        return trade

    def update_position_prices(self, price_map: dict[str, float]) -> None:
        """Update prices for all open positions."""
        for token_id, price in price_map.items():
            if token_id in self.positions:
                self.positions[token_id].update_price(price)
        self._save_state()

    def get_total_exposure(self) -> float:
        """Total USDC currently exposed across all positions."""
        return sum(p.size for p in self.positions.values())

    def get_total_unrealized_pnl(self) -> float:
        """Total unrealized P&L across all positions."""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def get_portfolio_summary(self) -> dict:
        """Get a summary of the current portfolio."""
        positions = list(self.positions.values())
        total_exposure = self.get_total_exposure()
        total_unrealized = self.get_total_unrealized_pnl()

        winning = [p for p in positions if p.unrealized_pnl > 0]
        losing = [p for p in positions if p.unrealized_pnl < 0]

        # Calculate realized P&L from closed trades
        closed_trades = [t for t in self.trade_history if t.pnl is not None]
        total_realized = sum(t.pnl for t in closed_trades)

        return {
            "open_positions": len(positions),
            "total_exposure_usdc": round(total_exposure, 2),
            "total_unrealized_pnl": round(total_unrealized, 2),
            "total_realized_pnl": round(total_realized, 2),
            "winning_positions": len(winning),
            "losing_positions": len(losing),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_trades": self.daily_trades,
            "total_trades": len(self.trade_history),
        }

    def check_stop_loss(self, stop_loss_pct: float) -> list[str]:
        """Check which positions have hit their stop loss."""
        triggered = []
        for token_id, position in self.positions.items():
            if position.unrealized_pnl_pct <= -stop_loss_pct:
                triggered.append(token_id)
                logger.warning(
                    f"STOP LOSS triggered for {position.question}: "
                    f"{position.unrealized_pnl_pct:.1%}"
                )
        return triggered

    def check_take_profit(self, take_profit_pct: float) -> list[str]:
        """Check which positions have hit their take profit."""
        triggered = []
        for token_id, position in self.positions.items():
            if position.unrealized_pnl_pct >= take_profit_pct:
                triggered.append(token_id)
                logger.info(
                    f"TAKE PROFIT triggered for {position.question}: "
                    f"{position.unrealized_pnl_pct:.1%}"
                )
        return triggered
