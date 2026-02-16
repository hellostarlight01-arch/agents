import os
import time
from typing import Optional

import httpx
from web3 import Web3
from web3.middleware import geth_poa_middleware

from agents.copytrade.config import CopyTradeConfig
from agents.copytrade.logger import log_event, setup_audit_logger


# Polymarket exchange contracts on Polygon
POLYMARKET_EXCHANGES = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # Neg Risk Exchange
}


class ProfileMonitor:
    """Monitors a Polymarket trader by watching on-chain transactions."""

    def __init__(self, config: CopyTradeConfig) -> None:
        self.config = config
        self.logger = setup_audit_logger(config.log_file)
        self.data_api_url = "https://data-api.polymarket.com"
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.target_address: Optional[str] = None
        self.proxy_wallet: Optional[str] = None
        self.known_trade_ids: set = set()
        self.last_nonce: int = 0
        self.last_block: int = 0

    def get_target_address(self) -> str:
        """Get the target proxy wallet address from env."""
        if self.target_address:
            return self.target_address

        direct_address = os.getenv("COPY_TARGET_ADDRESS", "")
        if direct_address:
            self.target_address = direct_address
            return direct_address

        raise ValueError("COPY_TARGET_ADDRESS must be set in .env")

    def fetch_data_api_trades(self, address: str, limit: int = 50) -> list[dict]:
        """Fetch trades from data API as supplementary source."""
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
        return all_trades

    def decode_polymarket_trade(self, tx_hash: str) -> Optional[dict]:
        """Decode a Polymarket trade from transaction receipt."""
        try:
            time.sleep(0.3)  # Rate limit protection
            tx = self.w3.eth.get_transaction(tx_hash)

            to_addr = (tx.get("to") or "").lower()
            is_polymarket = to_addr in {e.lower() for e in POLYMARKET_EXCHANGES}

            if not is_polymarket:
                return None

            # Parse trade from logs
            trade_info = {
                "transactionHash": tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash),
                "from": tx["from"],
                "to": to_addr,
                "block": tx["blockNumber"],
            }

            # Try to get trade details from the data API using tx hash
            try:
                tx_hash_str = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
                url = f"{self.data_api_url}/trades"
                params = {"transactionHash": tx_hash_str, "limit": 5}
                resp = httpx.get(url, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        trade = data[0]
                        trade_info.update({
                            "asset_id": trade.get("asset", ""),
                            "side": trade.get("side", "BUY"),
                            "price": float(trade.get("price", 0)),
                            "size": float(trade.get("size", 0)),
                            "market": trade.get("title", "") or trade.get("market", ""),
                        })
                        return trade_info
            except Exception:
                pass

            # Fallback: return basic info from chain
            trade_info.update({
                "asset_id": "",
                "side": "BUY",
                "price": 0,
                "size": 0,
                "market": "Polymarket Trade (details pending)",
            })
            return trade_info

        except Exception as e:
            log_event(self.logger, "DECODE_ERROR", f"Failed to decode tx {tx_hash}: {e}")
            return None

    def check_new_transactions(self) -> list[dict]:
        """Check for new on-chain transactions from the target."""
        address = self.get_target_address()
        new_trades = []

        try:
            current_nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(address)
            )

            if current_nonce > self.last_nonce:
                new_tx_count = current_nonce - self.last_nonce
                log_event(
                    self.logger,
                    "NEW_TXS",
                    f"Detected {new_tx_count} new transaction(s) (nonce {self.last_nonce} -> {current_nonce})",
                )
                print(f"  ** {new_tx_count} new transaction(s) detected! **")

                # Scan recent blocks for transactions from this address
                current_block = self.w3.eth.block_number
                # Only scan last 10 blocks to avoid rate limits
                scan_from = max(self.last_block, current_block - 10)

                for block_num in range(scan_from, current_block + 1):
                    try:
                        block = self.w3.eth.get_block(block_num, full_transactions=True)
                        for tx in block.transactions:
                            tx_from = (tx.get("from") or "").lower()
                            if tx_from == address.lower():
                                tx_hash = tx["hash"]
                                tx_hash_str = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)

                                if tx_hash_str in self.known_trade_ids:
                                    continue

                                self.known_trade_ids.add(tx_hash_str)
                                trade = self.decode_polymarket_trade(tx_hash)
                                if trade:
                                    new_trades.append(trade)
                        time.sleep(0.3)  # Rate limit protection
                    except Exception as e:
                        log_event(self.logger, "BLOCK_SCAN_ERROR", f"Block {block_num}: {e}")
                        time.sleep(1)  # Back off on errors

                self.last_nonce = current_nonce
                self.last_block = current_block

        except Exception as e:
            log_event(self.logger, "NONCE_CHECK_ERROR", f"Failed to check nonce: {e}")

        return new_trades

    def detect_new_trades(self) -> list[dict]:
        """Detect new trades via on-chain monitoring + data API."""
        new_trades = []

        # Primary: check on-chain transactions
        chain_trades = self.check_new_transactions()
        new_trades.extend(chain_trades)

        # Secondary: also check data API for any trades we might have missed
        address = self.get_target_address()
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
        """Initialize by recording current state."""
        address = self.get_target_address()
        log_event(self.logger, "MONITOR_INIT", f"Initializing for: {address}")

        # Record current nonce and block
        try:
            self.last_nonce = self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(address)
            )
            self.last_block = self.w3.eth.block_number
            print(f"  Target: {address}")
            print(f"  Current nonce: {self.last_nonce}")
            print(f"  Current block: {self.last_block}")
        except Exception as e:
            log_event(self.logger, "INIT_ERROR", f"Failed to get nonce: {e}")
            print(f"  WARNING: Could not get nonce for {address}: {e}")

        # Load existing data API trades into known set
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
