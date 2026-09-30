import os
from dotenv import load_dotenv

load_dotenv()


def _get_int(key, default):
    try:
        return int(os.getenv(key, default))
    except Exception:
        return default


def _get_float(key, default):
    try:
        return float(os.getenv(key, default))
    except Exception:
        return default


def _get_bool(key, default=False):
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


class Config:
    # Angel
    ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
    ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
    ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
    ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")
    ANGEL_BASE_URL = os.getenv("ANGEL_BASE_URL", "https://apiconnect.angelone.in")
    SCRIP_MASTER_URL = os.getenv(
        "SCRIP_MASTER_URL",
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    )

    # Market
    UNDERLYING = os.getenv("UNDERLYING", "NIFTY")
    UNDERLYING_TOKEN = os.getenv("UNDERLYING_TOKEN", "26000")
    EXCHANGE = os.getenv("EXCHANGE", "NSE")
    EXCHANGE_TYPE_NSE = _get_int("EXCHANGE_TYPE_NSE", 1)
    EXCHANGE_TYPE_NFO = _get_int("EXCHANGE_TYPE_NFO", 2)
    STRIKE_INTERVAL = _get_int("STRIKE_INTERVAL", 50)
    CANDIDATE_COUNT = _get_int("CANDIDATE_COUNT", 10)
    EXPIRY_MODE = os.getenv("EXPIRY_MODE", "current_weekly")

    # Session
    MARKET_OPEN = os.getenv("MARKET_OPEN", "09:15")
    MARKET_CLOSE = os.getenv("MARKET_CLOSE", "15:30")
    PRE_OPEN_START = os.getenv("PRE_OPEN_START", "09:00")
    POST_CLOSE_END = os.getenv("POST_CLOSE_END", "15:45")

    # Cycle
    CYCLE_SECONDS = _get_int("CYCLE_SECONDS", 60)

    # Gates
    MIN_SCORE = _get_float("MIN_SCORE", 75)
    MIN_CONFIDENCE = _get_float("MIN_CONFIDENCE", 70)
    MIN_DATA_QUALITY = _get_float("MIN_DATA_QUALITY", 75)
    MIN_RR = _get_float("MIN_RR", 1.5)
    MAX_SPREAD_PERCENT = _get_float("MAX_SPREAD_PERCENT", 8)
    MAX_STALE_SECONDS = _get_int("MAX_STALE_SECONDS", 20)

    # Source behavior
    ALLOW_JUGAAD_FALLBACK = _get_bool("ALLOW_JUGAAD_FALLBACK", True)
    ALLOW_CALCULATED_GREEKS = _get_bool("ALLOW_CALCULATED_GREEKS", True)

    # Storage
    DB_PATH = os.getenv("DB_PATH", "data/history.db")
    LOG_DIR = os.getenv("LOG_DIR", "data/logs")
    TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


config = Config()

