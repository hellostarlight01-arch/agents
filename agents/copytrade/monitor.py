import time
from typing import Optional

import httpx

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger


class ProfileMonitor:
    """Monitors a Polymarket profile and detects new trades by tracking position changes."""

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.clob_url = "https://clob.polymarket.com"
        self.target_address: Optional[str] = None
        self.known_trade_ids: set = set()
        # Position tracking: {asset_id: {"size": float, "side": str, ...}}
        self.last_positions: dict = {}

    def resolve_profile_to_address(self) -> str:
        """Resolve a Polymarket profile URL/username to a wallet address."""
        username = self.config.extract_username_from_url()
        log_event(
            self.logger, "RESOLVE_PROFILE", f"Resolving username: {username}"
        )

        # Try Gamma API profile endpoint
        try:
            url = f"{self.gamma_url}/profiles/{username}"
            resp = httpx.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                address = data.get("address") or data.get("proxyWallet")
                if address:
                    self.target_address = address
                    log_event(
                        self.logger,
                        "PROFILE_RESOLVED",
                        f"Username {username} -> {address}",
                    )
                    return address
        except Exception as e:
            log_event(
                self.logger,
                "RESOLVE_PROFILE_ERROR",
                f"Gamma profiles endpoint failed: {e}",
            )

        # Fallback: try the users search endpoint
        try:
            url = f"{self.gamma_url}/users"
            params = {"username": username}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    address = data[0].get("address") or data[0].get(
                        "proxyWallet"
                    )
                    if address:
                        self.target_address = address
                        log_event(
                            self.logger,
                            "PROFILE_RESOLVED",
                            f"Username {username} -> {address}",
                        )
                        return address
                elif isinstance(data, dict):
                    address = data.get("address") or data.get("proxyWallet")
                    if address:
                        self.target_address = address
                        log_event(
                            self.logger,
                            "PROFILE_RESOLVED",
                            f"Username {username} -> {address}",
                        )
                        return address
        except Exception as e:
            log_event(
                self.logger,
                "RESOLVE_PROFILE_ERROR",
                f"Gamma users endpoint failed: {e}",
            )

        raise ValueError(
            f"Could not resolve Polymarket profile for username: {username}. "
            "You can set COPY_TARGET_ADDRESS directly in .env as a fallback."
        )

    def get_target_address(self) -> str:
        """Get the target address, resolving from profile URL if needed."""
        if self.target_address:
            return self.target_address

        import os

        direct_address = os.getenv("COPY_TARGET_ADDRESS", "")
        if direct_address:
            self.target_address = direct_address
            log_event(
                self.logger,
                "TARGET_ADDRESS_SET",
                f"Using direct address from env: {direct_address}",
            )
            return direct_address

        return self.resolve_profile_to_address()

    def fetch_target_positions(self) -> list[dict]:
        """Fetch current positions from the target address via multiple endpoints."""
        address = self.get_target_address()
        positions = []

        # Try Gamma positions endpoint
        try:
            url = f"{self.gamma_url}/positions"
            params = {"user": address}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    positions = data
                    log_event(
                        self.logger,
                        "FETCH_POSITIONS",
                        f"Gamma returned {len(positions)} positions",
                    )
        except Exception as e:
            log_event(
                self.logger,
                "FETCH_POSITIONS_ERROR",
                f"Gamma positions failed: {e}",
            )

        # Also try with checksummed address
        if not positions:
            try:
                from web3 import Web3
                checksummed = Web3.to_checksum_address(address)
                url = f"{self.gamma_url}/positions"
                params = {"user": checksummed}
                resp = httpx.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        positions = data
                        log_event(
                            self.logger,
                            "FETCH_POSITIONS",
                            f"Gamma (checksummed) returned {len(positions)} positions",
                        )
            except Exception as e:
                log_event(
                    self.logger,
                    "FETCH_POSITIONS_ERROR",
                    f"Gamma checksummed positions failed: {e}",
                )

        return positions

    def _positions_to_dict(self, positions: list[dict]) -> dict:
        """Convert positions list to a dict keyed by asset/token ID."""
        result = {}
        for pos in positions:
            # Try different field names for the asset identifier
            asset_id = (
                pos.get("asset_id")
                or pos.get("token_id")
                or pos.get("assetId")
                or pos.get("tokenId")
                or pos.get("conditionId")
                or ""
            )
            if not asset_id:
                continue

            size = float(pos.get("size", 0) or pos.get("amount", 0) or 0)
            result[asset_id] = {
                "size": size,
                "side": pos.get("side", "BUY"),
                "price": float(pos.get("avgPrice", 0) or pos.get("price", 0) or 0),
                "market": pos.get("title", "") or pos.get("market", "") or pos.get("question", ""),
                "asset_id": asset_id,
                "raw": pos,
            }
        return result

    def detect_new_trades(self) -> list[dict]:
        """Detect new trades by comparing position snapshots."""
        current_positions_list = self.fetch_target_positions()
        current_positions = self._positions_to_dict(current_positions_list)

        new_trades = []

        # Compare with last known positions
        for asset_id, current in current_positions.items():
            prev = self.last_positions.get(asset_id)

            if prev is None:
                # Brand new position
                if current["size"] > 0:
                    trade = {
                        "asset_id": asset_id,
                        "side": "BUY",
                        "price": current["price"],
                        "size": current["size"],
                        "market": current["market"],
                        "type": "NEW_POSITION",
                    }
                    new_trades.append(trade)
                    log_event(
                        self.logger,
                        "NEW_POSITION",
                        f"New position: {current['market']} | Size: {current['size']} | Price: {current['price']}",
                    )
            else:
                size_diff = current["size"] - prev["size"]
                if abs(size_diff) > 0.001:  # Meaningful change
                    side = "BUY" if size_diff > 0 else "SELL"
                    trade = {
                        "asset_id": asset_id,
                        "side": side,
                        "price": current["price"],
                        "size": abs(size_diff),
                        "market": current["market"],
                        "type": "POSITION_CHANGE",
                    }
                    new_trades.append(trade)
                    log_event(
                        self.logger,
                        "POSITION_CHANGE",
                        f"{side} {current['market']} | Change: {size_diff:+.4f} | Price: {current['price']}",
                    )

        # Check for closed positions (existed before, gone now)
        for asset_id, prev in self.last_positions.items():
            if asset_id not in current_positions and prev["size"] > 0:
                trade = {
                    "asset_id": asset_id,
                    "side": "SELL",
                    "price": prev["price"],
                    "size": prev["size"],
                    "market": prev["market"],
                    "type": "POSITION_CLOSED",
                }
                new_trades.append(trade)
                log_event(
                    self.logger,
                    "POSITION_CLOSED",
                    f"Closed: {prev['market']} | Size: {prev['size']}",
                )

        # Update snapshot
        self.last_positions = current_positions

        if new_trades:
            log_event(
                self.logger,
                "NEW_TRADES_DETECTED",
                f"Found {len(new_trades)} position changes from target",
            )

        return new_trades

    def initialize(self) -> None:
        """Initialize by taking a snapshot of current positions."""
        address = self.get_target_address()
        log_event(
            self.logger,
            "MONITOR_INIT",
            f"Initializing monitor for target: {address}",
        )

        # Take initial snapshot of positions
        positions = self.fetch_target_positions()
        self.last_positions = self._positions_to_dict(positions)

        log_event(
            self.logger,
            "MONITOR_INIT_COMPLETE",
            f"Snapshot: {len(self.last_positions)} existing positions (will track changes from here)",
        )
        print(f"  Initial positions found: {len(self.last_positions)}")
        for asset_id, pos in self.last_positions.items():
            market = pos["market"] or asset_id[:20]
            print(f"    - {market}: size={pos['size']:.4f} price={pos['price']:.4f}")
