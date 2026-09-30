"""Jugaad-Data / NSE option-chain retrieval (BLOCKORA §5, §71).

The jugaad-data library no longer exposes a reliable NSE option-chain API, so
this module talks to NSE's NextApi endpoint directly with a rate-limited,
cookie-primed requests session. Every response is validated and tagged with
provenance before it can enter the cache. No data is ever fabricated here:
failures return raw=None plus an explicit error/status.
"""

import json
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

NSE_BASE_URL = "https://www.nseindia.com"
NSE_NEXTAPI_URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


def _failure(source, symbol, error, status):
    return {
        "source": source,
        "symbol": symbol,
        "raw": None,
        "error": error,
        "status": status,
    }


class JugaadDataSource:
    """Rate-limited NSE option-chain source with explicit error reporting."""

    def __init__(self, min_request_interval=2.0, cookie_refresh_seconds=600):
        self.base_url = NSE_BASE_URL
        self.headers = dict(_HEADERS)
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self._cookies_ready = False
        self._last_cookie_refresh = 0.0
        self._cookie_refresh_seconds = cookie_refresh_seconds
        self._last_request_time = 0.0
        self._min_request_interval = min_request_interval
        self._lock = threading.Lock()
        self.last_success_ts = None
        self.last_error = None

    # --- internal helpers -------------------------------------------------

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _prime_cookies(self):
        """Prime NSE session cookies via a normal browser-like page hit."""
        self._rate_limit()
        try:
            resp = self.session.get(
                f"{self.base_url}/option-chain",
                timeout=10,
            )
            self._cookies_ready = resp.status_code == 200
            self._last_cookie_refresh = time.time()
            if not self._cookies_ready:
                logger.warning("NSE cookie priming got HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            self._cookies_ready = False
            logger.warning("NSE cookie priming failed: %s", exc)

    def _ensure_cookies(self):
        now = time.time()
        if not self._cookies_ready or (now - self._last_cookie_refresh) > self._cookie_refresh_seconds:
            self._prime_cookies()

    # --- public API --------------------------------------------------------

    def option_chain_index(self, symbol="NIFTY"):
        """Fetch the NSE derivatives/option-chain payload for one symbol.

        Returns {"source": "JUGAAD", "symbol", "raw", "error", "status"}.
        raw is the validated NSE payload or None. Never raises for network
        conditions; all failures are explicit so the caller can decide
        fallback behaviour (BLOCKORA §47: never silently swap sources).
        """
        with self._lock:
            self._ensure_cookies()
            self._rate_limit()
            try:
                resp = self.session.get(
                    NSE_NEXTAPI_URL,
                    params={
                        "functionName": "getSymbolDerivativesData",
                        "symbol": symbol,
                        "marketType": "N",
                        "series": "EQ",
                    },
                    timeout=10,
                )
            except requests.exceptions.Timeout:
                self.last_error = "request_timeout"
                logger.error("NSE NextApi request timed out")
                return _failure("JUGAAD", symbol, "request_timeout", "failure")
            except requests.exceptions.ConnectionError:
                self.last_error = "connection_error"
                logger.error("NSE NextApi connection error")
                return _failure("JUGAAD", symbol, "connection_error", "failure")
            except requests.RequestException as exc:
                self.last_error = str(exc)
                logger.error("NSE NextApi request failed: %s", exc)
                return _failure("JUGAAD", symbol, str(exc), "failure")

            if resp.status_code != 200:
                # Cookie likely expired: refresh for the next attempt, but do
                # not retry transparently (rate-limit awareness, §71).
                self.last_error = f"http_{resp.status_code}"
                logger.warning("NSE NextApi HTTP %s", resp.status_code)
                self._prime_cookies()
                return _failure("JUGAAD", symbol, f"http_{resp.status_code}", "http_error")

            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                self.last_error = "json_decode_failed"
                logger.error("NSE NextApi response is not valid JSON")
                return _failure("JUGAAD", symbol, "json_decode_failed", "invalid_response")

            if not isinstance(data, dict):
                self.last_error = "empty_response"
                logger.warning("NSE NextApi response is empty or not a dict")
                return _failure("JUGAAD", symbol, "empty_response", "invalid_response")

            if data.get("error"):
                self.last_error = str(data["error"])[:200]
                logger.warning("NSE NextApi returned error: %s", data["error"])
                return _failure("JUGAAD", symbol, data["error"], "invalid_response")

            option_data = data.get("data")
            if option_data is None or not isinstance(option_data, list):
                self.last_error = "missing_or_bad_data_field"
                logger.warning("NSE NextApi 'data' field missing or not a list")
                return _failure("JUGAAD", symbol, "missing_or_bad_data_field", "invalid_response")

            if len(option_data) == 0:
                self.last_error = "empty_data"
                logger.info("NSE NextApi option chain is empty for %s", symbol)
                return _failure("JUGAAD", symbol, "empty_data", "success")

            self.last_success_ts = time.time()
            self.last_error = None
            logger.info("NSE NextApi chain fetched: %d records for %s", len(option_data), symbol)
            return {
                "source": "JUGAAD",
                "symbol": symbol,
                "raw": data,
                "error": None,
                "status": "success",
            }

    def status(self):
        return {
            "connected": self.last_success_ts is not None
            and (time.time() - self.last_success_ts) < self._cookie_refresh_seconds,
            "last_success_ts": self.last_success_ts,
            "last_error": self.last_error,
            "cookies_ready": self._cookies_ready,
        }


jugaad_source = JugaadDataSource()
