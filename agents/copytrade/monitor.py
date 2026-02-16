import time
from typing import Optional

import httpx

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger


class ProfileMonitor:
    """Monitors a Polymarket profile and detects new trades via data API."""

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.data_api_url = "https://data-api.polymarket.com"
        self.target_address: Optional[str] = None
        self.proxy_wallet: Optional[str] = None
        self.known_trade_ids: set = set()

    def get_target_address(self) -> str:
        """Get the target address from env."""
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

        raise ValueError("COPY_TARGET_ADDRESS must be set in .env")

    def resolve_proxy_wallet(self) -> str:
        """Find the proxy wallet by checking recent trades from the data API."""
        if self.proxy_wallet:
            return self.proxy_wallet

        address = self.get_target_address()

        # Try the address directly as maker
        try:
            url = f"{self.data_api_url}/trades"
            params = {"maker": address, "limit": 1}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    self.proxy_wallet = address
                    log_event(self.logger, "PROXY_RESOLVED", f"Address works directly: {address}")
                    return address

                # If no results, look up proxy wallet from chain
                proxy = data[0].get("proxyWallet", "") if data else ""
                if proxy:
                    self.proxy_wallet = proxy
                    log_event(self.logger, "PROXY_RESOLVED", f"Found proxy: {proxy}")
                    return proxy
        except Exception as e:
            log_event(self.logger, "PROXY_RESOLVE_ERROR", str(e))

        # Try finding proxy wallet via on-chain lookup
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

            # Polymarket proxy factory - try to get proxy for the address
            # Check recent transactions from the address to Polymarket exchange
            exchange = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"

            # Try the address as proxy wallet in the data API
            url = f"{self.data_api_url}/trades"
            for param_name in ["maker", "taker"]:
                params = {param_name: address, "limit": 1}
                resp = httpx.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        # Check if this trade has a proxyWallet field
                        proxy = data[0].get("proxyWallet", "")
                        if proxy and proxy.lower() != address.lower():
                            self.proxy_wallet = proxy
                            log_event(self.logger, "PROXY_RESOLVED", f"Found proxy from trade: {proxy}")
                            return proxy
        except Exception as e:
            log_event(self.logger, "PROXY_RESOLVE_ERROR", str(e))

        # Default to the address itself
        self.proxy_wallet = address
        return address

    def fetch_target_trades(self) -> list[dict]:
        """Fetch recent trades from the data API."""
        address = self.resolve_proxy_wallet()
        all_trades = []

        # Fetch as maker
        try:
            url = f"{self.data_api_url}/trades"
            params = {"maker": address, "limit": 50}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    all_trades.extend(data)
        except Exception as e:
            log_event(self.logger, "FETCH_TRADES_ERROR", f"Data API maker failed: {e}")

        # Fetch as taker
        try:
            url = f"{self.data_api_url}/trades"
            params = {"taker": address, "limit": 50}
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    all_trades.extend(data)
        except Exception as e:
            log_event(self.logger, "FETCH_TRADES_ERROR", f"Data API taker failed: {e}")

        # Also check with the original address if proxy is different
        original = self.get_target_address()
        if original.lower() != address.lower():
            for role in ["maker", "taker"]:
                try:
                    url = f"{self.data_api_url}/trades"
                    params = {role: original, "limit": 50}
                    resp = httpx.get(url, params=params, timeout=15)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            all_trades.extend(data)
                except Exception as e:
                    log_event(self.logger, "FETCH_TRADES_ERROR", f"Data API {role} (original) failed: {e}")

        # Deduplicate
        seen = set()
        unique = []
        for trade in all_trades:
            tid = trade.get("id") or trade.get("transactionHash") or trade.get("tradeID")
            if tid and tid not in seen:
                seen.add(tid)
                unique.append(trade)

        return unique

    def detect_new_trades(self) -> list[dict]:
        """Detect trades we haven't seen before."""
        all_trades = self.fetch_target_trades()

        new_trades = []
        for trade in all_trades:
            trade_id = trade.get("id") or trade.get("transactionHash") or trade.get("tradeID")
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
        address = self.resolve_proxy_wallet()
        log_event(
            self.logger,
            "MONITOR_INIT",
            f"Initializing monitor for target: {address}",
        )

        existing_trades = self.fetch_target_trades()
        for trade in existing_trades:
            trade_id = trade.get("id") or trade.get("transactionHash") or trade.get("tradeID")
            if trade_id:
                self.known_trade_ids.add(trade_id)

        # Print summary of recent trades
        print(f"  Proxy wallet: {address}")
        print(f"  Loaded {len(self.known_trade_ids)} existing trades (will skip these)")
        if existing_trades:
            print(f"  Recent trades from target:")
            for trade in existing_trades[:5]:
                side = trade.get("side", "?")
                title = trade.get("title", "") or trade.get("market", "unknown")
                size = trade.get("size", "?")
                price = trade.get("price", "?")
                print(f"    - {side} {title} | Size: {size} | Price: {price}")

        log_event(
            self.logger,
            "MONITOR_INIT_COMPLETE",
            f"Loaded {len(self.known_trade_ids)} existing trades (will skip these)",
        )
