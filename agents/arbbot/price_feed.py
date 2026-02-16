"""Real-time crypto price feed from Binance websocket (no API key needed)."""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import websocket


@dataclass
class PriceSnapshot:
    price: float
    timestamp: float


class BinancePriceFeed:
    """Streams real-time prices from Binance via websocket.

    Maintains a rolling window of price history for each coin
    to detect significant price movements.
    """

    STREAM_URL = "wss://stream.binance.com:9443/stream?streams={streams}"

    def __init__(self, coins: list[str], lookback_seconds: int = 60) -> None:
        self.coins = [c.lower() for c in coins]
        self.lookback_seconds = lookback_seconds
        # Price history: coin -> deque of PriceSnapshot
        self.prices: dict[str, deque] = {
            coin: deque(maxlen=5000) for coin in self.coins
        }
        self.latest_price: dict[str, float] = {coin: 0.0 for coin in self.coins}
        self._ws = None
        self._thread = None
        self._running = False

    def _build_stream_url(self) -> str:
        streams = "/".join(f"{coin}usdt@aggTrade" for coin in self.coins)
        return self.STREAM_URL.format(streams=streams)

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
            if "data" not in data:
                return
            trade = data["data"]
            symbol = trade.get("s", "").lower().replace("usdt", "")
            price = float(trade.get("p", 0))
            ts = trade.get("T", time.time() * 1000) / 1000.0  # ms -> sec

            if symbol in self.prices and price > 0:
                self.prices[symbol].append(PriceSnapshot(price=price, timestamp=ts))
                self.latest_price[symbol] = price
        except Exception:
            pass

    def _on_error(self, ws, error) -> None:
        print(f"  [PriceFeed] WebSocket error: {error}")

    def _on_close(self, ws, close_status, close_msg) -> None:
        if self._running:
            print("  [PriceFeed] Connection lost, reconnecting in 5s...")
            time.sleep(5)
            self._connect()

    def _on_open(self, ws) -> None:
        coins_str = ", ".join(c.upper() for c in self.coins)
        print(f"  [PriceFeed] Connected to Binance: {coins_str}")

    def _connect(self) -> None:
        url = self._build_stream_url()
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def start(self) -> None:
        """Start the price feed in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()
        # Wait for first price to arrive
        for _ in range(50):  # 5 seconds max
            if any(p > 0 for p in self.latest_price.values()):
                break
            time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()

    def get_price_move(self, coin: str) -> tuple[float, float]:
        """Get the price movement for a coin over the lookback window.

        Returns: (move_pct, current_price)
            move_pct: percentage change (positive = up, negative = down)
            current_price: latest price
        """
        coin = coin.lower()
        if coin not in self.prices or not self.prices[coin]:
            return 0.0, 0.0

        now = time.time()
        cutoff = now - self.lookback_seconds
        current = self.latest_price.get(coin, 0.0)

        # Find the oldest price within the lookback window
        oldest_price = current
        for snap in self.prices[coin]:
            if snap.timestamp >= cutoff:
                oldest_price = snap.price
                break

        if oldest_price <= 0 or current <= 0:
            return 0.0, current

        move_pct = (current - oldest_price) / oldest_price * 100.0
        return move_pct, current
