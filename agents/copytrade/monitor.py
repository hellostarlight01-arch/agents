import os
import time
from typing import Optional

import httpx
from web3 import Web3
from web3.middleware import geth_poa_middleware

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger


class ProfileMonitor:
    """Monitors a Polymarket trader using nonce tracking + data API.

    Uses only 1 RPC call per poll (nonce check) to avoid rate limits.
    Gets trade details from data API (no rate limit).
    """

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.data_api_url = "https://data-api.polymarket.com"
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.target_address: Optional[str] = None
        self.known_trade_ids: set = set()
        self.last_nonce: int = 0

    def get_target_address(self) -> str:
        if self.target_address:
            return self.target_address
        direct_address = os.getenv("COPY_TARGET_ADDRESS", "")
        if direct_address:
            self.target_address = direct_address
            return direct_address
        raise ValueError("COPY_TARGET_ADDRESS must be set in .env")

    def fetch_data_api_trades(self, address: str, limit: int = 50) -> list[dict]:
        """Fetch trades from data API (no rate limits)."""
        all_trades = []
        for role in ["maker", "taker"]:
            try:
                url = f"{self.data_api_url}/trades"
                params = {role: address, "limit": limit}
                resp = httpx.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        all_trades.extend(data)
            except Exception as e:
                log_event(self.logger, "FETCH_ERROR", f"Data API {role}: {e}")

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
        """Detect new trades: 1 RPC call (nonce) + data API if nonce changed."""
        address = self.get_target_address()
        new_trades = []
        nonce_changed = False

        # Single RPC call: check nonce
        try:
            current_nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(address)
            )
            if current_nonce > self.last_nonce:
                nonce_changed = True
                new_tx_count = current_nonce - self.last_nonce
                print(f"  ** {new_tx_count} new transaction(s) detected (nonce {self.last_nonce} -> {current_nonce}) **")
                log_event(self.logger, "NEW_TXS", f"Nonce: {self.last_nonce} -> {current_nonce}")
                self.last_nonce = current_nonce
        except Exception as e:
            log_event(self.logger, "NONCE_CHECK_ERROR", f"Failed: {e}")
            # Still check data API even if nonce check fails
            nonce_changed = True

        # Only query data API if nonce changed
        if nonce_changed:
            api_trades = self.fetch_data_api_trades(address, limit=20)
            for trade in api_trades:
                trade_id = (
                    trade.get("id")
                    or trade.get("transactionHash")
                    or trade.get("tradeID")
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
        """Initialize: record nonce and load existing trades."""
        address = self.get_target_address()
        log_event(self.logger, "MONITOR_INIT", f"Initializing for: {address}")

        # Record current nonce (1 RPC call)
        try:
            self.last_nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(address)
            )
            print(f"  Target: {address}")
            print(f"  Current nonce: {self.last_nonce}")
        except Exception as e:
            log_event(self.logger, "INIT_ERROR", f"Failed to get nonce: {e}")
            print(f"  WARNING: Could not get nonce: {e}")

        # Load existing trades from data API
        existing = self.fetch_data_api_trades(address, limit=50)
        for trade in existing:
            trade_id = (
                trade.get("id")
                or trade.get("transactionHash")
                or trade.get("tradeID")
            )
            if trade_id:
                self.known_trade_ids.add(trade_id)

        print(f"  Loaded {len(self.known_trade_ids)} existing trades")
        if existing:
            print(f"  Recent trades:")
            for trade in existing[:3]:
                side = trade.get("side", "?")
                title = trade.get("title", "") or trade.get("market", "unknown")
                print(f"    - {side} {title[:60]}")

        log_event(
            self.logger,
            "MONITOR_INIT_COMPLETE",
            f"Nonce: {self.last_nonce}, Known trades: {len(self.known_trade_ids)}",
        )
