import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(key, default):
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _get_float(key, default):
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _get_bool(key, default=False):
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


class Config:
    # --- Secrets (always from .env only; never hard-coded) ---
    ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
    ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
    ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
    ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- Angel endpoints (non-secret defaults) ---
    ANGEL_BASE_URL = os.getenv("ANGEL_BASE_URL", "https://apiconnect.angelone.in")
    SCRIP_MASTER_URL = os.getenv(
        "SCRIP_MASTER_URL",
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    )

    # --- Instrument (BLOCKORA §8, §9) ---
    UNDERLYING = os.getenv("UNDERLYING", "NIFTY")
    UNDERLYING_TOKEN = os.getenv("UNDERLYING_TOKEN", "26000")
    EXCHANGE = os.getenv("EXCHANGE", "NSE")
    EXCHANGE_TYPE_NSE = _get_int("EXCHANGE_TYPE_NSE", 1)
    EXCHANGE_TYPE_NFO = _get_int("EXCHANGE_TYPE_NFO", 2)
    STRIKE_INTERVAL = _get_int("STRIKE_INTERVAL", 50)
    CANDIDATE_COUNT = _get_int("CANDIDATE_COUNT", 10)
    # EXPIRY_MODE: only "current_weekly" is implemented (BLOCKORA §9).
    EXPIRY_MODE = os.getenv("EXPIRY_MODE", "current_weekly")

    # --- Session (IST, BLOCKORA §39/§69) ---
    MARKET_OPEN = os.getenv("MARKET_OPEN", "09:15")
    MARKET_CLOSE = os.getenv("MARKET_CLOSE", "15:30")
    PRE_OPEN_START = os.getenv("PRE_OPEN_START", "09:00")
    POST_CLOSE_END = os.getenv("POST_CLOSE_END", "15:45")

    # --- Data freshness / polling (BLOCKORA §46, §71, §72) ---
    MAX_STALE_SECONDS = _get_int("MAX_STALE_SECONDS", 20)
    MAX_CHAIN_STALE_SECONDS = _get_int("MAX_CHAIN_STALE_SECONDS", 90)
    JUGAAD_REFRESH_SECONDS = _get_int("JUGAAD_REFRESH_SECONDS", 30)
    ANGEL_UNDERLYING_POLL_SECONDS = _get_int("ANGEL_UNDERLYING_POLL_SECONDS", 5)

    # --- Regime thresholds (BLOCKORA §12; initial documented values) ---
    BREAKOUT_ATR_PCT = _get_float("BREAKOUT_ATR_PCT", 0.35)
    LOW_VOL_RV_THRESHOLD = _get_float("LOW_VOL_RV_THRESHOLD", 0.03)

    # --- Gates (BLOCKORA §57) ---
    MIN_SCORE = _get_float("MIN_SCORE", 75)
    MIN_CONFIDENCE = _get_float("MIN_CONFIDENCE", 70)
    MIN_DATA_QUALITY = _get_float("MIN_DATA_QUALITY", 75)
    MIN_RR = _get_float("MIN_RR", 1.5)
    MAX_SPREAD_PERCENT = _get_float("MAX_SPREAD_PERCENT", 8)

    # --- Source behaviour (BLOCKORA §5, §47, §48) ---
    ALLOW_JUGAAD_FALLBACK = _get_bool("ALLOW_JUGAAD_FALLBACK", True)
    ALLOW_CALCULATED_GREEKS = _get_bool("ALLOW_CALCULATED_GREEKS", True)
    SPOT_TOLERANCE_PCT = _get_float("SPOT_TOLERANCE_PCT", 0.5)

    # --- Risk / target (BLOCKORA §27) ---
    # Target = entry + max(MIN_RR * risk, expected premium move).
    # Expected underlying move = TARGET_ATR_MULTIPLIER * ATR (documented
    # initial value; must be validated via walk-forward before tuning).
    TARGET_ATR_MULTIPLIER = _get_float("TARGET_ATR_MULTIPLIER", 1.5)

    # --- Direction evidence (BLOCKORA Phase 1) ---
    # Directories are read from environment: all real values are read from
    # config so the engine has no embedded constants.
    DIRECTION_MOVE_POINTS = _get_float("DIRECTION_MOVE_POINTS", 10.0)
    DIRECTION_LOOKBACK_MINUTES = _get_int("DIRECTION_LOOKBACK_MINUTES", 5)
    DIRECTION_PERSISTENCE_CYCLES = _get_int("DIRECTION_PERSISTENCE_CYCLES", 2)
    # PERCENTAGE POINTS (0-100 scale), not a 0-1 fraction: the maximum share of
    # the lookback peak-to-trough range that may be given back before a move is
    # treated as a spike/V-shape. 25.0 means "at most 25% retraced".
    DIRECTION_MAX_ADVERSE_PCT = _get_float("DIRECTION_MAX_ADVERSE_PCT", 25.0)
    DIRECTION_REQUIRE_VWAP_AGREE = _get_bool("DIRECTION_REQUIRE_VWAP_AGREE", True)

    # --- Confidence (BLOCKORA §28, §65, §66) ---
    # Minimum outcome samples before historical evidence may raise confidence.
    # Enforced per BLOCKORA §65 ("Minimum sample requirements must be
    # enforced"); configurable, not tuned against current output.
    MIN_HISTORICAL_SAMPLES = _get_int("MIN_HISTORICAL_SAMPLES", 10)

    # --- Storage (BLOCKORA §50, §53) ---
    DB_PATH = os.getenv("DB_PATH", "data/history.db")
    LOG_DIR = os.getenv("LOG_DIR", "data/logs")

    # --- Telegram (BLOCKORA §77; optional, decision-support only) ---
    TELEGRAM_ENABLED = _get_bool("TELEGRAM_ENABLED", False)
    TELEGRAM_MIN_INTERVAL_SECONDS = _get_int("TELEGRAM_MIN_INTERVAL_SECONDS", 300)

    # --- Backtest cost model (BLOCKORA §42, §44) ---
    # Initial conservative values; must be validated before trusting results.
    BACKTEST_TXN_COST_PCT = _get_float("BACKTEST_TXN_COST_PCT", 0.1)
    BACKTEST_SLIPPAGE_PCT = _get_float("BACKTEST_SLIPPAGE_PCT", 0.1)


config = Config()

if config.EXPIRY_MODE != "current_weekly":
    raise ValueError(
        f"Unsupported EXPIRY_MODE={config.EXPIRY_MODE!r}; "
        "only 'current_weekly' is implemented (BLOCKORA §9)"
    )
