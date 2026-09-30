import threading
import time
from collections import defaultdict


class MarketCache:
    """Thread-safe in-memory market cache (BLOCKORA §72, §73).

    The WebSocket/REST ingestion layer writes continuously; the 1-minute
    analysis layer reads point-in-time snapshots. SQLite stores history; this
    cache only holds the latest values plus per-key ingest timestamps so stale
    data can be detected per source/field (BLOCKORA §46).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._quotes = {}          # token -> quote payload
        self._underlying = {}      # symbol -> underlying payload
        self._option_chain = {}    # symbol -> chain payload (raw, provenance kept)
        self._last_update = defaultdict(float)

    def update_quote(self, token, payload):
        with self._lock:
            payload = dict(payload)
            payload["local_timestamp"] = time.time()
            self._quotes[token] = payload
            self._last_update[token] = time.time()

    def update_underlying(self, symbol, payload):
        with self._lock:
            payload = dict(payload)
            payload["local_timestamp"] = time.time()
            self._underlying[symbol] = payload
            self._last_update[f"underlying:{symbol}"] = time.time()

    def update_option_chain(self, symbol, payload):
        with self._lock:
            payload = dict(payload)
            payload["local_timestamp"] = time.time()
            self._option_chain[symbol] = payload
            self._last_update[f"chain:{symbol}"] = time.time()

    def get_quote(self, token):
        with self._lock:
            quote = self._quotes.get(token)
            return dict(quote) if quote else None

    def get_underlying(self, symbol):
        with self._lock:
            quote = self._underlying.get(symbol)
            return dict(quote) if quote else None

    def get_option_chain(self, symbol):
        with self._lock:
            chain = self._option_chain.get(symbol)
            return dict(chain) if chain else None

    def get_all_quotes(self):
        with self._lock:
            return {k: dict(v) for k, v in self._quotes.items()}

    def get_age_seconds(self, key):
        with self._lock:
            ts = self._last_update.get(key)
            if not ts:
                return None
            return time.time() - ts

    def underlying_age(self, symbol):
        return self.get_age_seconds(f"underlying:{symbol}")

    def chain_age(self, symbol):
        return self.get_age_seconds(f"chain:{symbol}")

    def quote_age(self, token):
        return self.get_age_seconds(token)

    def status(self):
        """Observability snapshot: keys and ages, no payloads (BLOCKORA §70)."""
        with self._lock:
            return {
                "quote_tokens": len(self._quotes),
                "underlying_symbols": sorted(self._underlying.keys()),
                "chain_symbols": sorted(self._option_chain.keys()),
                "last_update": dict(self._last_update),
            }


cache = MarketCache()
