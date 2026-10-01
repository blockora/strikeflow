"""Application logging setup with secret redaction (BLOCKORA §52).

The installed SmartAPI SDK logs full HTTP headers (Authorization bearer JWT,
X-PrivateKey API key) and raw WebSocket payloads through logzero's default
logger at ERROR level, into both stderr and logs/<date>/app.log. This module
installs a logging.Filter on the root logger so any record passing through
the application boundary has known secret patterns redacted before any
handler formats it. Values themselves are never read, printed or stored.
"""

import logging
import os
import re
import sys

# Header/value pairs and token shapes that must never reach a log sink.
# Each pattern keeps a short safe prefix so log lines stay diagnosable
# (BLOCKORA §52: sanitize, never expose).
REDACTION_PATTERNS = [
    # ORDER MATTERS: token-shape patterns first, then keyed values, then
    # header names — a header rule must never eat only the "Bearer" scheme
    # word and leave the secret value behind.
    # Raw JWT bodies
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    # Bearer tokens embedded anywhere
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{8,}"), "Bearer [REDACTED]"),
    # SmartAPI JSON error logs echo request params (quoted key + quoted value,
    # single or double quotes)
    (
        re.compile(
            r"(?i)(['\"](?:password|totp|mpin|pin|otp|secret|clientcode|api_key|apikey|"
            r"feed_token|refresh_token|jwt_token)['\"]\s*[:=]\s*['\"])[^'\"]*"
        ),
        r"\1[REDACTED]",
    ),
    # Header fields in any quote style (dict-repr or raw header lines):
    # "'X-PrivateKey': '...'" / "X-PrivateKey: ..."
    (
        re.compile(
            r"(?i)\b(X-PrivateKey|x-api-key|x-client-code|x-feed-token|Authorization|"
            r"Api-Key|Feed-Token|X-UserType|X-SourceID|X-MACAddress|"
            r"X-ClientLocalIP|X-ClientPublicIP)"
            r"(\s*['\"]?\s*[:=]\s*['\"]?)"
            r"([^'\",}\s]{4})[^'\",}\s]*"
        ),
        r"\1\2[REDACTED]",
    ),
]


class SecretRedactionFilter(logging.Filter):
    """Redact known secret patterns from every emitted log record."""

    def filter(self, record):  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pattern, replacement in REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        return True


def _sdk_log_dir_isolated():
    """Point the SmartAPI SDK's own logzero files into our LOG_DIR tree.

    smartConnect.py and smartWebSocketV2.py create logs/<date>/app.log in the
    CURRENT WORKING DIRECTORY. Keeping that inside our LOG_DIR keeps runtime
    artifacts out of the repository root and under the git-ignored data tree.
    """
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
    os.makedirs(base, exist_ok=True)
    return base


def setup_logging():
    """Configure root logging with the redaction filter; returns the app logger."""
    log_dir = _sdk_log_dir_isolated()
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace default handlers (idempotent across re-imports in tools).
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(os.path.join(log_dir, "app.log"), encoding="utf-8")
    file_handler.setFormatter(formatter)
    redactor = SecretRedactionFilter()
    for handler in (stream_handler, file_handler):
        handler.addFilter(redactor)
        root.addHandler(handler)
    root.addFilter(redactor)

    # The SmartAPI SDK logs through logzero's default logger, which sets
    # propagate=False (root filters/handlers never see its records) and
    # re-creates its own handlers on every SmartConnect/SmartWebSocketV2
    # construction via logzero.logfile(). A filter attached to the logger
    # OBJECT itself survives those handler resets, so attach it there too.
    sdk_logger = logging.getLogger("logzero")
    sdk_logger.addFilter(redactor)
    for handler in list(sdk_logger.handlers):
        handler.addFilter(redactor)

    return logging.getLogger("blockora")
