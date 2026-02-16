import os
import time
from typing import Optional

import httpx
from web3 import Web3
from web3.middleware import geth_poa_middleware

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger

# Multiple free Polygon RPC endpoints for fallback
RPC_ENDPOINTS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
]


class ProfileMonitor:
    """Monitors a Polymarket trader using nonce tracking + data API.

    Uses only 1 RPC call per poll (nonce check) to avoid rate limits.
    Gets trade details from data API (no rate limit).
    Falls back across multiple RPC providers if one is rate-limited.
    """

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.data_api_url = "https://data-api.polymarket.com"
        self._rpc_index = 0
        self.w3 = self._create_web3(RPC_ENDPOINTS[0])
        self.target_address: Optional[str] = None
        self.known_trade_ids: set = set()
        self.last_nonce: int = 0
        self._nonce_check_count: int = 0

    def _create_web3(self, rpc_url: str) -> Web3:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        return w3

    def _get_nonce_with_fallback(self, address: str) -> int:
        """Try multiple RPC endpoints to get nonce, avoiding rate limits."""
        last_error = None
        for i in range(len(RPC_ENDPOINTS)):
            idx = (self._rpc_index + i) % len(RPC_ENDPOINTS)
            rpc_url = RPC_ENDPOINTS[idx]
            try:
                w3 = self._create_web3(rpc_url)
                nonce = w3.eth.get_transaction_count(
                    Web3.to_checksum_address(address)
                )
                # Success — remember this RPC for next time
                self._rpc_index = idx
                self.w3 = w3
                return nonce
            except Exception as e:
                last_error = e
                log_event(self.logger, "RPC_FALLBACK", f"{rpc_url} failed: {e}")
                continue
        # All RPCs failed
        raise last_error

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
        """Detect new trades: 1 RPC call (nonce) + data API if nonce changed.

        Checks nonce every other poll to halve RPC usage.
        Falls back across multiple RPC endpoints on rate limits.
        """
        address = self.get_target_address()
        new_trades = []
        nonce_changed = False
        self._nonce_check_count += 1

        # Only check nonce every other poll to reduce RPC calls
        skip_nonce = (self._nonce_check_count % 2 == 0)

        if not skip_nonce:
            try:
                current_nonce = self._get_nonce_with_fallback(address)
                if current_nonce > self.last_nonce:
                    nonce_changed = True
                    new_tx_count = current_nonce - self.last_nonce
                    print(f"  ** {new_tx_count} new transaction(s) detected (nonce {self.last_nonce} -> {current_nonce}) **")
                    log_event(self.logger, "NEW_TXS", f"Nonce: {self.last_nonce} -> {current_nonce}")
                    self.last_nonce = current_nonce
            except Exception as e:
                log_event(self.logger, "NONCE_CHECK_ERROR", f"All RPCs failed: {e}")
                # Still check data API even if all RPCs fail
                nonce_changed = True
        else:
            # On skip polls, still check data API (it's free / no rate limit)
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

        # Record current nonce (tries multiple RPCs)
        try:
            self.last_nonce = self._get_nonce_with_fallback(address)
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
