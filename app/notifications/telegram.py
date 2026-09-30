import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

from app.config import config


def _escape_markdownv2(text):
    """Escape special characters for Telegram MarkdownV2."""
    if text is None:
        return ""
    escaped = str(text)
    chars = ["_", "*", "[", "]", "(", ")", "~", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
    for char in chars:
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _fingerprint(recommendation):
    """Create a deduplication fingerprint from recommendation fields."""
    if recommendation is None:
        return None

    # Build fingerprint from key fields that uniquely identify a recommendation
    parts = []

    underlying = recommendation.get("underlying")
    if underlying is not None:
        parts.append(_escape_markdownv2(str(underlying)))
    else:
        parts.append("")

    expiry = recommendation.get("expiry")
    if expiry is not None:
        parts.append(_escape_markdownv2(str(expiry)))
    else:
        parts.append("")

    strike = recommendation.get("strike")
    if strike is not None:
        parts.append(f"strike:{strike}")
    else:
        parts.append("strike:")

    opt_type = recommendation.get("option_type")
    if opt_type is not None:
        parts.append(_escape_markdownv2(str(opt_type)))
    else:
        parts.append("")

    direction = recommendation.get("direction", "")
    if direction:
        parts.append(_escape_markdownv2(str(direction)))
    else:
        parts.append("")

    entry = recommendation.get("entry")
    if entry is not None:
        parts.append(f"entry:{entry}")
    else:
        parts.append("entry:")

    return "|".join(parts)


class TelegramNotifier:
    """Telegram notification handler for Option Strike Selector recommendations."""

    def __init__(self):
        self.enabled = (
            config.TELEGRAM_ENABLED == "true"
            and config.TELEGRAM_BOT_TOKEN
            and config.TELEGRAM_CHAT_ID
        )
        self.bot_token = config.TELEGRAM_BOT_TOKEN if self.enabled else None
        self.chat_id = config.TELEGRAM_CHAT_ID if self.enabled else None
        self.last_sent = 0
        self.last_fingerprint = None
        self.min_interval = 300
        self.max_retries = 3
        self.retry_delay = 5

    def _parse_interval(self, interval_str):
        """Parse interval string to seconds."""
        try:
            val = int(interval_str)
            return max(10, val)  # minimum 10 seconds
        except (ValueError, TypeError):
            return 300  # default 5 minutes

    def _is_ratelimited(self):
        """Check if enough time has passed since last sent."""
        elapsed = time.time() - self.last_sent
        return elapsed < self.min_interval

    def _send_message(self, message):
        """Send message via Telegram Bot API. Returns True on success."""
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        params = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "MarkdownV2",
        }

        encoded_params = urllib.parse.urlencode(params)
        full_url = f"{url}?{encoded_params}"

        try:
            req = urllib.request.Request(full_url)
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    result = response.read().decode("utf-8")
                    data = urllib.parse.parse_qs(result)
                    if data.get("ok", [False])[0]:
                        return True
        except urllib.error.HTTPError as e:
            # Log sanitized error - never expose bot token
            try:
                body = e.read().decode("utf-8")[:200]
            except Exception:
                body = "unknown error"
            # Log would go here, but don't expose credentials
            pass
        except urllib.error.URLError:
            # Network error - don't crash
            pass
        except Exception:
            # Any other error - don't crash
            pass

        return False

    def _get_recommendation_text(self, recommendation):
        """Generate formatted Telegram message from recommendation dict."""
        if recommendation is None:
            return None

        lines = []

        # Header
        lines.append("📊 OPTION STRIKE RECOMMENDATION")
        lines.append("")

        # Basic info
        underlying = recommendation.get("underlying", "N/A")
        expiry = recommendation.get("expiry", "N/A")
        lines.append(f"Underlying: {underlying}")
        lines.append(f"Expiry: {expiry}")
        lines.append("")

        # Recommendation block
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("🎯 Recommendation")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("")

        # Option details
        option = recommendation.get("option", "N/A")
        lines.append(f"Option: {option}")
        lines.append("")

        # Strike and type
        strike = recommendation.get("strike", "N/A")
        opt_type = recommendation.get("option_type", "N/A")
        lines.append(f"Strike: {strike}")
        lines.append(f"Type: {opt_type}")
        lines.append("")

        # LTP
        ltp = recommendation.get("ltp")
        if ltp is not None:
            lines.append(f"LTP: ₹{float(ltp):.2f}")
        lines.append("")

        # Entry, SL, Target
        entry = recommendation.get("entry")
        if entry is not None:
            lines.append(f"Entry: ₹{float(entry):.2f}")
        else:
            lines.append("Entry: N/A")

        sl = recommendation.get("stop_loss")
        if sl is not None:
            lines.append(f"Stop Loss: ₹{float(sl):.2f}")
        else:
            lines.append("Stop Loss: N/A")

        target = recommendation.get("target")
        if target is not None:
            lines.append(f"Target: ₹{float(target):.2f}")
        else:
            lines.append("Target: N/A")

        lines.append("")

        # Risk/Reward
        rr = recommendation.get("risk_reward")
        if rr is not None:
            lines.append(f"Risk/Reward: 1:{float(rr):.2f}")
        lines.append("")

        # Score and confidence
        score = recommendation.get("score")
        if score is not None:
            lines.append(f"Score: {int(float(score))}")
        lines.append("")

        confidence = recommendation.get("confidence")
        if confidence is not None:
            lines.append(f"Confidence: {int(float(confidence))}%")
        lines.append("")

        # Regime and momentum
        regime = recommendation.get("regime", "N/A")
        lines.append(f"Market Regime: {regime}")

        momentum = recommendation.get("momentum", "N/A")
        lines.append(f"Momentum: {momentum}")

        lines.append("")

        # Footer disclaimer
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("⚠️ Recommendation Only")
        lines.append("No automatic order has been placed.")
        lines.append("━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)

    def send_recommendation(self, recommendation):
        """Send a recommendation to Telegram. Returns True if sent."""

        if not self.enabled:
            return False

        # Deduplication: check fingerprint
        fingerprint = _fingerprint(recommendation)

        # Skip if same recommendation as last sent (and cooldown passed)
        if fingerprint == self.last_fingerprint and not self._is_ratelimited():
            # Same recommendation, but cooldown hasn't passed - skip
            return True  # Consider it "sent" to avoid double-logging

        # If same fingerprint as last sent but cooldown expired, send new message
        if fingerprint == self.last_fingerprint and self._is_ratelimited():
            # Cooldown active, skip but don't count as error
            return True

        # Materially different recommendation - send new message
        # (always send if fingerprint changed or first time)

        # Generate message text
        message = self._get_recommendation_text(recommendation)
        if message is None:
            return False

        # Send with retries
        for attempt in range(self.max_retries):
            success = self._send_message(message)
            if success:
                self.last_sent = time.time()
                self.last_fingerprint = fingerprint
                return True
            # Exponential backoff
            time.sleep(self.retry_delay * (2 ** attempt))

        return False

    def notify_startup(self):
        """Send startup notification if configured."""
        if not self.enabled:
            return
        message = "🟢 Option Strike Selector started."
        self._send_message(message)

    def notify_shutdown(self):
        """Send shutdown notification if configured."""
        if not self.enabled:
            return
        message = "🔴 Option Strike Selector stopped."
        self._send_message(message)


# Global instance
_telegram_notifier = None


def get_telegram_notifier():
    """Get the global Telegram notifier instance."""
    global _telegram_notifier
    if _telegram_notifier is None:
        _telegram_notifier = TelegramNotifier()
    return _telegram_notifier


def send_recommendation(recommendation):
    """Convenience function to send a recommendation via Telegram."""
    notifier = get_telegram_notifier()
    return notifier.send_recommendation(recommendation)


def notify_startup():
    """Send startup notification."""
    notifier = get_telegram_notifier()
    notifier.notify_startup()


def notify_shutdown():
    """Send shutdown notification."""
    notifier = get_telegram_notifier()
    notifier.notify_shutdown()
