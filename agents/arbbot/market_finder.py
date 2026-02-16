"""Find currently active 15-minute crypto markets on Polymarket."""

import json
import time

import httpx


class MarketFinder:
    """Discovers the current active 15-min up/down market for a given coin.

    Slug pattern: {coin}-updown-15m-{unix_timestamp}
    Timestamp = floor(time / 900) * 900 (nearest 15-min boundary)
    """

    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self) -> None:
        # Cache: (coin, window_ts) -> market_info
        self._cache: dict[tuple[str, int], dict] = {}

    @staticmethod
    def current_window_ts() -> int:
        """Get the start timestamp of the current 15-min window."""
        return int(time.time() // 900) * 900

    @staticmethod
    def window_end_ts(window_ts: int) -> int:
        return window_ts + 900

    @staticmethod
    def seconds_into_window() -> int:
        """How many seconds into the current 15-min window are we."""
        return int(time.time()) % 900

    @staticmethod
    def seconds_remaining() -> int:
        """Seconds remaining in the current 15-min window."""
        return 900 - (int(time.time()) % 900)

    def _fetch_market_by_slug(self, slug: str) -> dict | None:
        """Fetch a market from Gamma API by slug."""
        try:
            resp = httpx.get(
                f"{self.GAMMA_API}/events",
                params={"slug": slug},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            # Events endpoint returns a list
            event = data[0] if isinstance(data, list) else data
            markets = event.get("markets", [])
            if not markets:
                return None
            return markets[0]
        except Exception:
            return None

    def find_active_market(self, coin: str) -> dict | None:
        """Find the current active 15-min market for a coin.

        Returns dict with keys: token_up, token_down, slug, question,
        price_up, price_down, condition_id, or None if not found.
        """
        coin = coin.lower()
        window_ts = self.current_window_ts()

        # Check cache
        cache_key = (coin, window_ts)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Try current window, then next window (markets may open early)
        for ts in [window_ts, window_ts + 900, window_ts - 900]:
            slug = f"{coin}-updown-15m-{ts}"
            market = self._fetch_market_by_slug(slug)
            if market and market.get("active") and not market.get("closed"):
                result = self._parse_market(market, slug, ts)
                if result:
                    self._cache[cache_key] = result
                    return result

        return None

    def _parse_market(self, market: dict, slug: str, window_ts: int) -> dict | None:
        """Parse market data into a clean dict."""
        try:
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
            outcomes = json.loads(market.get("outcomes", "[]"))
            prices = json.loads(market.get("outcomePrices", "[]"))

            if len(token_ids) < 2 or len(outcomes) < 2:
                return None

            # Match "Up" and "Down" to their token IDs
            up_idx = next(
                (i for i, o in enumerate(outcomes) if o.lower() == "up"), 0
            )
            down_idx = 1 - up_idx

            return {
                "slug": slug,
                "window_ts": window_ts,
                "question": market.get("question", ""),
                "condition_id": market.get("conditionId", ""),
                "token_up": token_ids[up_idx],
                "token_down": token_ids[down_idx],
                "price_up": float(prices[up_idx]) if len(prices) > up_idx else 0.5,
                "price_down": float(prices[down_idx]) if len(prices) > down_idx else 0.5,
                "accepting_orders": market.get("acceptingOrders", True),
            }
        except Exception:
            return None

    def clear_cache(self) -> None:
        """Clear stale cache entries (old windows)."""
        current_ts = self.current_window_ts()
        stale = [k for k in self._cache if k[1] < current_ts - 900]
        for k in stale:
            del self._cache[k]
