"""Angel One SmartAPI primary data source (BLOCKORA §4).

Covers: TOTP authentication, session refresh, Scrip Master download/caching,
NFO option-contract identification (token, trading symbol, lot size, expiry),
WebSocket streaming of underlying + option tokens into the shared cache, tick
parsing (paise conversion), tick accounting, and reconnect handling.

Every tick field comes straight from the wire: missing fields stay missing
(BLOCKORA §7). Nothing here fabricates market data.
"""

import json
import logging
import os
import threading
import time
import urllib.request

import pyotp

from app.config import config
from app.data.cache import cache
from app.data.normalizer import normalize_angel_quote

logger = logging.getLogger(__name__)


class AngelDataSource:
    def __init__(self):
        self.api = None
        self.auth_token = None
        self.refresh_token = None
        self.feed_token = None
        self.sws = None
        self.connected = False
        self.scrip_master = None
        self.scrip_master_loaded_at = None
        self._ws_thread = None
        self._ws_lock = threading.Lock()
        self._last_reconnect_attempt = 0.0
        self._reconnect_interval = 10.0
        self._ticks_received = 0
        self._last_tick_ts = None
        self._option_contracts = {}  # "STRIKE|CE|EXPIRY" -> scrip row
        self._missing_contracts = set()  # lookups with no NFO row in the master
        self._missing_warned = set()  # absent contracts already reported
        self.underlying_token = None
        self._reconnect_tick_hook = None
        self._stopping = False  # set on shutdown: suppresses all reconnects

    # --- authentication ---------------------------------------------------

    def _generate_totp(self):
        """Fresh TOTP immediately before each login attempt."""
        return pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()

    def login(self):
        if not (
            config.ANGEL_API_KEY
            and config.ANGEL_CLIENT_ID
            and config.ANGEL_PASSWORD
            and config.ANGEL_TOTP_SECRET
        ):
            raise ValueError("Angel credentials missing in .env")

        from SmartApi import SmartConnect  # deferred import: module-level deps

        self.api = SmartConnect(api_key=config.ANGEL_API_KEY)
        totp = self._generate_totp()
        session = self.api.generateSession(
            config.ANGEL_CLIENT_ID,
            config.ANGEL_PASSWORD,
            totp,
        )
        if not session or not session.get("status"):
            # Never log the raw session payload: it contains tokens (§52).
            raise RuntimeError("Angel login failed: invalid status from SmartAPI")

        data = session["data"]
        self.auth_token = data.get("jwtToken")
        self.refresh_token = data.get("refreshToken")
        try:
            self.feed_token = self.api.getfeedToken()
        except Exception as exc:
            logger.warning("feed token fetch failed: %s", exc)
            self.feed_token = None
        self.connected = True
        logger.info("Angel login OK (client=%s)", config.ANGEL_CLIENT_ID)
        return True

    def refresh_session(self):
        if not self.refresh_token:
            return self.login()
        try:
            resp = self.api.generateToken(self.refresh_token)
            data = resp.get("data", {})
            self.auth_token = data.get("jwtToken", self.auth_token)
            self.refresh_token = data.get("refreshToken", self.refresh_token)
            try:
                self.feed_token = self.api.getfeedToken()
            except Exception as exc:
                logger.warning("feed token refresh failed: %s", exc)
            self.connected = True
            logger.info("Angel session refreshed")
            return True
        except Exception as exc:
            logger.warning("Angel token refresh failed (%s); full re-login", exc)
            return self.login()

    # --- scrip master -----------------------------------------------------

    def load_scrip_master(self):
        """Download and cache the Scrip Master JSON on disk (BLOCKORA §4)."""
        # A newly loaded master invalidates resolved and missing lookups:
        # expiries roll and the traded strike set changes (BLOCKORA §9).
        self._option_contracts = {}
        self._missing_contracts = set()
        self._missing_warned = set()
        cache_dir = os.path.join(config.LOG_DIR, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "scrip_master.json")

        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < 24 * 3600:
                try:
                    with open(cache_path, "r", encoding="utf-8") as fh:
                        self.scrip_master = json.load(fh)
                    self.scrip_master_loaded_at = time.time()
                    logger.info("Scrip master loaded from cache (%d rows)", len(self.scrip_master))
                    return self.scrip_master
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Scrip master cache unreadable: %s", exc)

        with urllib.request.urlopen(config.SCRIP_MASTER_URL, timeout=30) as response:
            raw = response.read().decode("utf-8")
        self.scrip_master = json.loads(raw)
        self.scrip_master_loaded_at = time.time()
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(self.scrip_master, fh)
        except OSError as exc:
            logger.warning("Scrip master cache write failed: %s", exc)
        logger.info("Scrip master downloaded (%d rows)", len(self.scrip_master))
        return self.scrip_master

    @staticmethod
    def _expiry_date(value):
        """Parse a Scrip-Master/chain expiry to a date, or None.

        The master writes '06OCT2026'; the Jugaad chain writes
        '06-Oct-2026'. Expiry must be compared as a calendar date, never as
        raw text (BLOCKORA §9: expiry comes from the chain, never inferred
        from symbol text).
        """
        from datetime import datetime
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%b%y", "%d-%b-%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except (TypeError, ValueError):
                continue
        return None

    def get_instrument_rows(self, name=None, instrumenttype=None, expiry=None, strike=None, option_type=None):
        if self.scrip_master is None:
            self.load_scrip_master()
        rows = self.scrip_master
        if name:
            rows = [r for r in rows if r.get("name") == name]
        if instrumenttype:
            rows = [r for r in rows if r.get("instrumenttype") == instrumenttype]
        if expiry:
            target = self._expiry_date(expiry)
            if target is not None:
                rows = [
                    r for r in rows
                    if self._expiry_date(r.get("expiry")) == target
                ]
            else:
                rows = [r for r in rows if r.get("expiry") == expiry]
        if strike is not None:
            try:
                strike_val = float(strike)
            except (TypeError, ValueError):
                return []

            def _strike_matches(raw):
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    return False
                # Current master stores option strikes in paise
                # ('2500000.000000' for 25000); accept either scale so the
                # comparison follows the actual downloaded data, never an
                # assumption.
                return abs(v - strike_val) < 0.01 or abs(v / 100.0 - strike_val) < 0.01

            rows = [
                r for r in rows
                if r.get("strike") not in (None, "") and _strike_matches(r["strike"])
            ]
        if option_type:
            rows = [r for r in rows if str(r.get("symbol", "")).endswith(option_type)]
        return rows

    def find_option_contract(self, strike, option_type, expiry, warn=True):
        """Resolve an NFO option contract via Scrip Master (BLOCKORA §9).

        Returns the scrip row with token/tradingsymbol/lotsize, or None.
        Expiry comes from the chain data, never inferred from symbol text.

        A miss is an expected outcome, not an anomaly: the NSE chain lists a
        wide strike band for an expiry while the master only carries the
        strikes actually traded for it. Callers that sweep the whole chain
        pass warn=False; a miss on a contract the app actually selected is
        still reported exactly once. Misses are cached so the (large) master
        scan is not repeated for every cycle.
        """
        key = f"{strike}|{option_type}|{expiry}"
        if key in self._option_contracts:
            return self._option_contracts[key]
        if key in self._missing_contracts:
            # Already known to be absent: never rescan the master, but a
            # selected contract must still be reported to the operator.
            if warn and key not in self._missing_warned:
                self._missing_warned.add(key)
                self._warn_missing_contract(strike, option_type, expiry)
            return None

        candidates = self.get_instrument_rows(
            name=config.UNDERLYING,
            instrumenttype="OPTIDX",
            expiry=expiry,
            strike=strike,
            option_type=option_type,
        )
        contract = candidates[0] if candidates else None
        if contract:
            self._option_contracts[key] = contract
        else:
            self._missing_contracts.add(key)
            if warn:
                self._missing_warned.add(key)
                self._warn_missing_contract(strike, option_type, expiry)
        return contract

    def _warn_missing_contract(self, strike, option_type, expiry):
        logger.warning(
            "No NFO contract found for %s %s %s (expiry=%s, parsed=%s)",
            config.UNDERLYING, strike, option_type, expiry,
            self._expiry_date(expiry),
        )

    def underlying_token_row(self):
        """Resolve the underlying index instrument from the fresh Scrip Master.

        Real master layout (verified against a live download, 143339 rows):
        index rows live under exch_seg='NSE' as symbol='NIFTY' (instrumenttype
        '', the SmartAPI market-feed index token) and as 'Nifty 50'
        (instrumenttype 'AMXIDX'). The previous lookup (name +
        instrumenttype='INDEX') matched the CDS-segment 'NIFTY50' INDEX row
        (token 2), which is not an NSE instrument and made every underlying
        poll fail with AB4046. The row is validated against the freshly
        downloaded master before being used or cached; nothing is assumed
        constant and no token is ever invented (BLOCKORA §9).
        """
        if self.scrip_master is None:
            self.load_scrip_master()
        segment = str(config.EXCHANGE).upper()
        name = str(config.UNDERLYING).upper()
        candidates = [
            r for r in self.scrip_master
            if str(r.get("exch_seg", "")).upper() == segment
        ]
        # 1) exact symbol match on the configured exchange
        #    (NIFTY on NSE -> the market-feed index row, token 26000)
        for row in candidates:
            if str(row.get("symbol", "")).strip().upper() == name:
                return row
        # 2) index-series rows (AMXIDX / INDEX) whose name matches
        for row in candidates:
            if (
                str(row.get("instrumenttype", "")).upper() in ("AMXIDX", "INDEX")
                and str(row.get("name", "")).strip().upper() == name
            ):
                return row
        logger.warning(
            "No underlying instrument found in Scrip Master for %s on %s",
            config.UNDERLYING, segment,
        )
        return None

    def set_underlying_token(self, token):
        """Record the underlying token used by reconnect resubscription."""
        self.underlying_token = str(token)

    # --- REST quotes --------------------------------------------------------

    def ltp_data(self, exchange, tradingsymbol, symboltoken):
        if not self.api:
            raise RuntimeError("Angel API not initialized; login first")
        return self.api.ltpData(exchange, tradingsymbol, symboltoken)

    def market_data(self, mode, exchange_tokens):
        if not self.api:
            raise RuntimeError("Angel API not initialized; login first")
        return self.api.getMarketData(mode, exchange_tokens)

    # --- WebSocket ----------------------------------------------------------

    def start_websocket(self, tokens_nse=None, tokens_nfo=None, on_tick=None):
        """Subscribe and stream ticks into the cache (BLOCKORA §4, §72).

        QUOTE mode (2) per the installed SDK supplies last_traded_price,
        last_traded_quantity, volume_trade_for_the_day, total_buy/sell
        quantity and OHLC; prices are paise-scaled and converted in the
        normalizer. OI and best-5 depth exist only in SNAP_QUOTE mode.
        """
        if not self.connected:
            self.login()

        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        correlation_id = f"blockora_{int(time.time())}"
        mode = 2

        token_list = []
        if tokens_nse:
            token_list.append({
                "exchangeType": config.EXCHANGE_TYPE_NSE,
                "tokens": [str(x) for x in tokens_nse],
            })
        if tokens_nfo:
            token_list.append({
                "exchangeType": config.EXCHANGE_TYPE_NFO,
                "tokens": [str(x) for x in tokens_nfo],
            })

        sws = SmartWebSocketV2(
            self.auth_token,
            config.ANGEL_API_KEY,
            config.ANGEL_CLIENT_ID,
            self.feed_token,
            max_retry_attempt=5,
        )

        # Callback-signature compatibility with the INSTALLED websocket-client
        # (verified from vendor sources; no site-packages edits):
        # - websocket/_app.py teardown() dispatches on_close as
        #   callback(wsapp, close_status_code, close_reason), but the installed
        #   SDK's SmartWebSocketV2._on_close accepts only (wsapp). The resulting
        #   TypeError is caught by WebSocketApp._callback and re-routed into
        #   SDK _on_error, which starts the SDK resubscribe loop even during
        #   intentional shutdown (observed on real Termux).
        # - SDK _on_error begins its internal retry BEFORE the application
        #   callback runs, so the stopping gate must wrap it at the instance
        #   level; connect() picks up these wrappers when it constructs the
        #   WebSocketApp (on_close=self._on_close / on_error=self._on_error).
        _sdk_on_close = sws._on_close
        _sdk_on_error = sws._on_error

        def _sdk_on_close_compat(wsapp, *args):
            # websocket-client passes (wsapp, close_status_code, close_reason);
            # the SDK reads only the socket object.
            _sdk_on_close(wsapp)

        def _sdk_on_error_compat(wsapp, error):
            if self._stopping:
                return  # intentional shutdown: never enter SDK retry/resubscribe
            _sdk_on_error(wsapp, error)

        sws._on_close = _sdk_on_close_compat
        sws._on_error = _sdk_on_error_compat

        def _on_open(wsapp):
            logger.info("Angel WebSocket open; subscribing %d groups", len(token_list))
            if not token_list:
                return
            with self._ws_lock:
                client = self.sws
            # wsapp is the SDK's internal websocket.WebSocketApp, which has
            # no subscribe(); the subscribe frame must be sent through the
            # SmartWebSocketV2 instance (installed smartWebSocketV2.py:
            # self.wsapp.send(json.dumps(request_data)) inside subscribe()).
            try:
                if client is not None:
                    client.subscribe(correlation_id, mode, token_list)
            except Exception as exc:
                logger.error("Angel subscribe failed: %s", exc)

        def _on_data(wsapp, message):
            """Cache raw ticks (paise scale) and normalized ticks (rupees).

            The raw SDK field names are preserved under 'raw'; the normalized
            dict uses the field map of app/data/normalizer.py (same parsing
            code path for live and any future replay). No field is invented:
            OI is absent in QUOTE mode and stays missing (§7).
            """
            try:
                payload_batch = message if isinstance(message, list) else [message]
                for tick in payload_batch:
                    token = str(tick.get("token"))
                    cache.update_quote(token, tick)  # raw wire values (paise)
                    normalized = normalize_angel_quote(dict(tick))
                    if normalized:
                        cache.update_quote(f"{token}:n", normalized)  # rupees
                    self._ticks_received += 1
                    self._last_tick_ts = time.time()
                    if on_tick:
                        on_tick(normalized or {"token": token})
            except Exception:
                logger.exception("Angel tick processing failed")

        def _on_error(wsapp, error):
            self.connected = False
            if self._stopping:
                return  # deliberate close during shutdown: no reconnect noise
            logger.error("Angel WebSocket error: %s", error)

        def _on_close(wsapp):
            self.connected = False
            if self._stopping:
                return  # deliberate close during shutdown
            logger.warning("Angel WebSocket closed; will attempt reconnect")

        sws.on_open = _on_open
        sws.on_data = _on_data
        sws.on_error = _on_error
        sws.on_close = _on_close

        with self._ws_lock:
            self.sws = sws
        sws.connect()

    def start_websocket_thread(self, tokens_nse=None, tokens_nfo=None, on_tick=None):
        """Run the WebSocket client on a daemon thread (BLOCKORA §72)."""
        if self._ws_thread and self._ws_thread.is_alive():
            logger.warning("Angel WebSocket thread already running")
            return
        self._ws_thread = threading.Thread(
            target=self.start_websocket,
            args=(tokens_nse, tokens_nfo, on_tick),
            daemon=True,
            name="angel-ws",
        )
        self._ws_thread.start()

    def ensure_connected(self):
        """Reconnect if the WebSocket dropped (BLOCKORA §47, §74).

        Returns True when a live connection is believed present. Reconnects
        are rate-limited; a real SmartWebSocketV2 client is re-created on the
        WebSocket thread (the SDK client is not restartable in place).
        """
        if self._stopping:
            return False  # never reconnect once shutdown has begun
        if self.connected and self.sws is not None:
            return True
        now = time.time()
        if now - self._last_reconnect_attempt < self._reconnect_interval:
            return False
        self._last_reconnect_attempt = now
        logger.warning("Angel disconnected; attempting reconnect")
        try:
            self.refresh_session()
            if self.connected and self._ws_thread is not None:
                # start_websocket_thread refuses while the old daemon thread
                # is alive; since the socket dropped, clear the dead thread
                # handle so a fresh SmartWebSocketV2 client can be created.
                if self._ws_thread is not None and not self._ws_thread.is_alive():
                    self._ws_thread = None
                self.start_websocket_thread(
                    tokens_nse=[str(self.underlying_token)] if getattr(self, "underlying_token", None) else None,
                    on_tick=self._reconnect_tick_hook,
                )
            return self.connected and self.sws is not None
        except Exception as exc:
            logger.error("Angel reconnect failed: %s", exc)
            self.connected = False
            return False

    def stop_websocket(self):
        try:
            with self._ws_lock:
                if self.sws:
                    self.sws.close_connection()
        except Exception as exc:
            logger.warning("Angel WebSocket close failed: %s", exc)
        self.connected = False

    def request_stop(self):
        """Begin deterministic shutdown (no further reconnect attempts).

        Sets the stopping flag BEFORE closing so in-flight callbacks and the
        SDK's internal retry path observe it and stay quiet (§74).
        """
        self._stopping = True
        self.stop_websocket()

    # --- observability -------------------------------------------------------

    def status(self):
        age = None
        if self._last_tick_ts:
            age = time.time() - self._last_tick_ts
        return {
            "connected": self.connected,
            "ticks_received": self._ticks_received,
            "last_tick_age_seconds": age,
            "scrip_master_rows": len(self.scrip_master) if self.scrip_master else 0,
            "option_contracts_cached": len(self._option_contracts),
        }


angel_source = AngelDataSource()
