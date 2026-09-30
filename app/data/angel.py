import json
import time
import urllib.request
from datetime import datetime

import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from app.config import config
from app.data.cache import cache


class AngelDataSource:
    def __init__(self):
        self.api = None
        self.auth_token = None
        self.refresh_token = None
        self.feed_token = None
        self.sws = None
        self.connected = False
        self.scrip_master = None
        self._ws_thread = None

    def _generate_totp(self):
        """Generate a fresh TOTP immediately before the login attempt."""
        return pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()

    def login(self):
        if not (config.ANGEL_API_KEY and config.ANGEL_CLIENT_ID and config.ANGEL_PASSWORD and config.ANGEL_TOTP_SECRET):
            raise ValueError("Angel credentials missing in .env")

        self.api = SmartConnect(api_key=config.ANGEL_API_KEY)
        totp = self._generate_totp()
        session = self.api.generateSession(
            config.ANGEL_CLIENT_ID,
            config.ANGEL_PASSWORD,
            totp,
        )
        if not session or not session.get("status"):
            raise RuntimeError(f"Angel login failed: {session}")

        data = session["data"]
        self.auth_token = data["jwtToken"]
        self.refresh_token = data["refreshToken"]
        self.feed_token = self.api.getfeedToken()
        self.connected = True
        return True

    def refresh_session(self):
        if not self.refresh_token:
            return self.login()
        try:
            resp = self.api.generateToken(self.refresh_token)
            data = resp.get("data", {})
            self.auth_token = data.get("jwtToken", self.auth_token)
            self.refresh_token = data.get("refreshToken", self.refresh_token)
            self.feed_token = self.api.getfeedToken()
            self.connected = True
            return True
        except Exception:
            return self.login()

    def load_scrip_master(self):
        with urllib.request.urlopen(config.SCRIP_MASTER_URL, timeout=30) as response:
            raw = response.read().decode("utf-8")
        self.scrip_master = json.loads(raw)
        return self.scrip_master

    def get_instrument_rows(self, name=None, instrumenttype=None):
        if self.scrip_master is None:
            self.load_scrip_master()
        rows = self.scrip_master
        if name:
            rows = [r for r in rows if r.get("name") == name]
        if instrumenttype:
            rows = [r for r in rows if r.get("instrumenttype") == instrumenttype]
        return rows

    def ltp_data(self, exchange, tradingsymbol, symboltoken):
        return self.api.ltpData(exchange, tradingsymbol, symboltoken)

    def market_data(self, mode, exchange_tokens):
        return self.api.getMarketData(mode, exchange_tokens)

    def start_websocket(self, tokens_nse=None, tokens_nfo=None, on_tick=None):
        if not self.connected:
            self.login()

        correlation_id = f"blockora_{int(time.time())}"
        action = 1
        mode = 2  # quote mode; gives more fields than LTP

        token_list = []
        if tokens_nse:
            token_list.append({"exchangeType": config.EXCHANGE_TYPE_NSE, "tokens": [str(x) for x in tokens_nse]})
        if tokens_nfo:
            token_list.append({"exchangeType": config.EXCHANGE_TYPE_NFO, "tokens": [str(x) for x in tokens_nfo]})

        sws = SmartWebSocketV2(
            self.auth_token,
            config.ANGEL_API_KEY,
            config.ANGEL_CLIENT_ID,
            self.feed_token,
            max_retry_attempt=5,
        )

        def _on_open(wsapp):
            if token_list:
                wsapp.subscribe(correlation_id, mode, token_list)

        def _on_data(wsapp, message):
            try:
                token = str(message.get("token"))
                payload = {
                    "source": "ANGEL",
                    "token": token,
                    "exchange_type": message.get("exchange_type"),
                    "ltp": message.get("last_traded_price"),
                    "open": message.get("open_price_of_the_day"),
                    "high": message.get("high_price_of_the_day"),
                    "low": message.get("low_price_of_the_day"),
                    "close": message.get("closed_price"),
                    "exchange_timestamp": message.get("exchange_timestamp"),
                    "raw": message,
                }
                cache.update_quote(token, payload)
                if on_tick:
                    on_tick(payload)
            except Exception:
                pass

        def _on_error(wsapp, error):
            self.connected = False

        def _on_close(wsapp):
            self.connected = False

        sws.on_open = _on_open
        sws.on_data = _on_data
        sws.on_error = _on_error
        sws.on_close = _on_close

        self.sws = sws
        sws.connect()

    def stop_websocket(self):
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception:
            pass
        self.connected = False


angel_source = AngelDataSource()
