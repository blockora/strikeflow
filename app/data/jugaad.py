import time
import math
import requests
import logging
import json

logger = logging.getLogger(__name__)


class JugaadDataSource:
    def __init__(self):
        self.base_url = "https://www.nseindia.com"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=NIFTY",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self._cookies_ready = False
        self._last_request_time = 0
        self._min_request_interval = 2.0  # minimum 2 seconds between requests

    def _rate_limit(self):
        """Enforce minimum interval between requests to avoid rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            sleep_time = self._min_request_interval - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _prime_cookies(self):
        """Prime NSE session cookies by accessing the equity quotes page."""
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/get-quotes/equity?symbol=NIFTY",
                timeout=10,
            )
            self._cookies_ready = resp.status_code == 200
        except Exception:
            self._cookies_ready = False

    def option_chain_index(self, symbol="NIFTY"):
        """Fetch NSE option chain data for the given symbol.

        Uses the NSE NextApi endpoint which is currently functional.
        Returns a dict with 'source', 'symbol', 'raw' (option chain data),
        'error', and 'status' fields. On failure, 'raw' is None and 'error'
        describes the problem.

        The endpoint used is:
            https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi
        with payload: functionName=getSymbolDerivativesData, symbol=<symbol>
        """
        self._rate_limit()

        nextapi_url = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
        payload = {
            "functionName": "getSymbolDerivativesData",
            "symbol": symbol,
            "marketType": "N",
            "series": "EQ",
        }
        try:
            resp = self.session.get(nextapi_url, params=payload, timeout=10)

            # Handle non-200 status codes
            if resp.status_code != 200:
                error_msg = f"NSE NextApi HTTP {resp.status_code}"
                logger.warning(f"{error_msg}: {resp.text[:200]}")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": error_msg,
                    "status": "http_error",
                }

            # Parse JSON response
            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.error("NSE NextApi response is not valid JSON")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": "json_decode_failed",
                    "status": "invalid_response",
                }

            # Validate that we got option chain data
            if not data or not isinstance(data, dict):
                logger.warning("NSE NextApi response is empty or not a dict")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": "empty_response",
                    "status": "invalid_response",
                }

            # Check for error in response
            if data.get("error"):
                logger.warning(f"NSE NextApi returned error: {data['error']}")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": data["error"],
                    "status": "invalid_response",
                }

            # Validate we have option data
            option_data = data.get("data", [])
            if option_data is None:
                logger.warning("NSE NextApi response missing 'data' field")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": "missing_data_field",
                    "status": "invalid_response",
                }

            if not isinstance(option_data, list):
                logger.warning("NSE NextApi 'data' field is not a list")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": "data_not_list",
                    "status": "invalid_response",
                }

            if len(option_data) == 0:
                logger.debug("NSE NextApi option chain data is empty")
                self._prime_cookies()
                return {
                    "source": "JUGAAD",
                    "symbol": symbol,
                    "raw": None,
                    "error": "empty_data",
                    "status": "success",
                }

            logger.info(
                f"NSE NextApi option chain fetched: {len(option_data)} records for {symbol}"
            )
            return {
                "source": "JUGAAD",
                "symbol": symbol,
                "raw": data,
                "error": None,
                "status": "success",
            }

        except requests.exceptions.Timeout:
            logger.error("NSE NextApi request timed out")
            self._prime_cookies()
            return {
                "source": "JUGAAD",
                "symbol": symbol,
                "raw": None,
                "error": "request_timeout",
                "status": "failure",
            }
        except requests.exceptions.ConnectionError:
            logger.error("NSE NextApi connection error")
            self._prime_cookies()
            return {
                "source": "JUGAAD",
                "symbol": symbol,
                "raw": None,
                "error": "connection_error",
                "status": "failure",
            }
        except Exception as exc:
            logger.error(f"NSE NextApi unexpected error: {exc}")
            self._prime_cookies()
            return {
                "source": "JUGAAD",
                "symbol": symbol,
                "raw": None,
                "error": str(exc),
                "status": "failure",
            }


jugaad_source = JugaadDataSource()