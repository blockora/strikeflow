import threading
import time
from collections import defaultdict


class MarketCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._quotes = {}
        self._underlying = {}
        self._option_chain = {}
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
            self._last_update[symbol] = time.time()

    def update_option_chain(self, symbol, payload):
        with self._lock:
            payload = dict(payload)
            payload["local_timestamp"] = time.time()
            self._option_chain[symbol] = payload
            self._last_update[symbol] = time.time()

    def get_quote(self, token):
        with self._lock:
            return self._quotes.get(token)

    def get_underlying(self, symbol):
        with self._lock:
            return self._underlying.get(symbol)

    def get_option_chain(self, symbol):
        with self._lock:
            return self._option_chain.get(symbol)

    def get_all_quotes(self):
        with self._lock:
            return dict(self._quotes)

    def get_age_seconds(self, key):
        with self._lock:
            ts = self._last_update.get(key)
            if not ts:
                return None
            return time.time() - ts


cache = MarketCache()
