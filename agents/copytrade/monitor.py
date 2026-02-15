import time
from typing import Optional

import httpx

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger


class ProfileMonitor:
    """Monitors a Polymarket profile and detects new trades to copy."""

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.clob_url = "https://clob.polymarket.com"
        self.target_address: Optional[str] = None
        self.last_seen_trade_id: Optional[str] = None
        self.known_trade_ids: set = set()

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

    def fetch_target_trades(self) -> list[dict]:
        """Fetch recent trades from the target address via CLOB API."""
        address = self.get_target_address()

        try:
            url = f"{self.clob_url}/trades"
            params = {"maker_address": address, "limit": 50}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log_event(
                self.logger,
                "FETCH_TRADES_ERROR",
                f"CLOB trades endpoint failed: {e}",
            )

        # Fallback: try the data API
        try:
            url = f"{self.clob_url}/data/trades"
            params = {"maker_address": address, "limit": 50}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data
        except Exception as e:
            log_event(
                self.logger,
                "FETCH_TRADES_ERROR",
                f"CLOB data/trades endpoint failed: {e}",
            )

        return []

    def fetch_target_positions(self) -> list[dict]:
        """Fetch current positions from the target address."""
        address = self.get_target_address()

        try:
            url = f"{self.gamma_url}/positions"
            params = {"user": address}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log_event(
                self.logger,
                "FETCH_POSITIONS_ERROR",
                f"Gamma positions endpoint failed: {e}",
            )

        return []

    def detect_new_trades(self) -> list[dict]:
        """Detect trades we haven't seen before."""
        all_trades = self.fetch_target_trades()

        new_trades = []
        for trade in all_trades:
            trade_id = trade.get("id") or trade.get("tradeID") or trade.get(
                "transaction_hash"
            )
            if trade_id and trade_id not in self.known_trade_ids:
                self.known_trade_ids.add(trade_id)
                new_trades.append(trade)

        if new_trades:
            log_event(
                self.logger,
                "NEW_TRADES_DETECTED",
                f"Found {len(new_trades)} new trades from target",
            )

        return new_trades

    def initialize(self) -> None:
        """Initialize by loading current trades into known set (skip existing)."""
        address = self.get_target_address()
        log_event(
            self.logger,
            "MONITOR_INIT",
            f"Initializing monitor for target: {address}",
        )

        existing_trades = self.fetch_target_trades()
        for trade in existing_trades:
            trade_id = trade.get("id") or trade.get("tradeID") or trade.get(
                "transaction_hash"
            )
            if trade_id:
                self.known_trade_ids.add(trade_id)

        log_event(
            self.logger,
            "MONITOR_INIT_COMPLETE",
            f"Loaded {len(self.known_trade_ids)} existing trades (will skip these)",
        )
