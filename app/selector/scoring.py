"""Scoring engine (BLOCKORA §15).

Weights are the BLOCKORA §15 initial architecture exactly; each component is
normalized to its own weight so the total score spans 0-100. Weights are
initial values only and must be tuned exclusively through validated
walk-forward testing (§67, §68) — never adjusted to improve current output.
"""

from app.config import config


class ScoringEngine:
    WEIGHTS = {
        "underlying_trend": 20,
        "option_momentum": 15,
        "oi": 15,
        "volume": 10,
        "liquidity": 10,
        "atm_suitability": 10,
        "greeks": 10,
        "iv": 5,
        "risk_reward": 5,
    }

    MAX_TOTAL_SCORE = sum(WEIGHTS.values())  # 100 per BLOCKORA §15

    def score_candidates(self, candidates, underlying, regime):
        scored = []
        for c in candidates:
            trend_score = self._underlying_trend_score(c, underlying, regime)
            momentum_score = self._momentum_score(c)
            oi_score = self._oi_score(c)
            volume_score = self._volume_score(c)
            liquidity_score = self._liquidity_score(c)
            atm_score = self._atm_score(c, underlying)
            greeks_score = self._greeks_score(c)
            iv_score = self._iv_score(c)
            rr_score = self._rr_score(c)

            total = (
                trend_score + momentum_score + oi_score + volume_score
                + liquidity_score + atm_score + greeks_score + iv_score + rr_score
            )

            reasons = []
            if trend_score >= 14:
                reasons.append("Underlying trend supportive")
            if momentum_score >= 10:
                reasons.append("Option momentum positive")
            if oi_score >= 10:
                reasons.append("OI confirmation")
            if volume_score >= 7:
                reasons.append("Volume confirmation")
            if liquidity_score >= 7:
                reasons.append("Acceptable liquidity/spread")
            if atm_score >= 7:
                reasons.append("Good ATM proximity")
            if greeks_score >= 7:
                reasons.append("Suitable Greeks")
            if iv_score >= 3:
                reasons.append("IV behaviour acceptable")
            if rr_score >= 3:
                reasons.append("Valid risk/reward")

            c = dict(c)
            c["scores"] = {
                "underlying_trend": trend_score,
                "option_momentum": momentum_score,
                "oi": oi_score,
                "volume": volume_score,
                "liquidity": liquidity_score,
                "atm_suitability": atm_score,
                "greeks": greeks_score,
                "iv": iv_score,
                "risk_reward": rr_score,
            }
            c["total_score"] = round(total, 2)
            c["reasons"] = reasons
            scored.append(c)

        scored.sort(key=lambda x: x["total_score"], reverse=True)
        for i, c in enumerate(scored, start=1):
            c["rank"] = i
        return scored

    # --- components ---------------------------------------------------------

    def _underlying_trend_score(self, c, underlying, regime):
        """Directional alignment, normalized to the 20-point weight."""
        side = c.get("option_type")
        trend = underlying.get("trend") if underlying else "NEUTRAL"
        spot = underlying.get("spot") if underlying else None
        vwap = underlying.get("vwap") if underlying else None

        # Regime agreement contributes up to 12 points.
        if regime in ("BULLISH", "BREAKOUT") and side == "CE":
            score = 12
        elif regime in ("BEARISH", "BREAKDOWN") and side == "PE":
            score = 12
        elif regime in ("BEARISH", "BREAKDOWN") and side == "CE":
            score = 2
        elif regime in ("BULLISH", "BREAKOUT") and side == "PE":
            score = 2
        else:
            score = 6

        # Trend-structure agreement contributes up to 5 points.
        if trend == "BULLISH":
            score += 5 if side == "CE" else 0
        elif trend == "BEARISH":
            score += 5 if side == "PE" else 0
        else:
            score += 2

        # VWAP relationship contributes up to 3 points.
        if spot is not None and vwap is not None:
            above = spot > vwap
            if (side == "CE" and above) or (side == "PE" and not above):
                score += 3

        # Unclear regime caps directional conviction (penalty, not absolute).
        if regime in ("HIGH_VOLATILITY", "CHOPPY", "NO_CLEAR_REGIME"):
            score = min(score, 8)
        return min(score, self.WEIGHTS["underlying_trend"])

    def _momentum_score(self, c):
        """Premium change momentum, normalized to the 15-point weight."""
        pchange = c.get("pchange")
        change = c.get("change")
        if pchange is not None:
            try:
                p = float(pchange)
            except (TypeError, ValueError):
                p = None
            if p is not None:
                if p > 10:
                    return 15
                if p > 5:
                    return 12
                if p > 2:
                    return 9
                if p > 0:
                    return 6
                if p > -5:
                    return 3
                return 0
        if change is not None:
            try:
                cg = float(change)
            except (TypeError, ValueError):
                cg = None
            if cg is not None:
                if cg > 0:
                    return 9
                if cg == 0:
                    return 3
                return 0
        # No momentum evidence: neutral-low, never a fabricated positive.
        return 3

    def _oi_score(self, c):
        """OI + OI change interpreted together (BLOCKORA §18)."""
        oi = c.get("oi")
        oi_change = c.get("oi_change")
        if oi is None and oi_change is None:
            return 3
        score = 0
        if oi is not None:
            if oi >= 100000:
                score += 7
            elif oi >= 50000:
                score += 5
            elif oi >= 10000:
                score += 3
            else:
                score += 1
        if oi_change is not None:
            if oi_change > 0:
                score += 8
            elif oi_change < 0:
                score += 2
        return min(score, self.WEIGHTS["oi"])

    def _volume_score(self, c):
        vol = c.get("volume")
        if vol is None:
            return 3
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            return 3
        if vol >= 100000:
            return 10
        if vol >= 50000:
            return 8
        if vol >= 10000:
            return 6
        if vol >= 1000:
            return 4
        return 2

    def _liquidity_score(self, c):
        bid = c.get("bid")
        ask = c.get("ask")
        if bid is None or ask is None:
            return 2
        try:
            bid, ask = float(bid), float(ask)
        except (TypeError, ValueError):
            return 2
        if bid <= 0 or ask <= 0 or ask < bid:
            return 0
        spread = ask - bid
        mid = (ask + bid) / 2.0
        spread_pct = (spread / mid) * 100.0 if mid else None
        if spread_pct is None:
            return 2
        if spread_pct <= 2:
            return 10
        if spread_pct <= 4:
            return 8
        if spread_pct <= 6:
            return 5
        if spread_pct <= config.MAX_SPREAD_PERCENT:
            return 3
        return 0

    def _atm_score(self, c, underlying):
        spot = underlying.get("spot") if underlying else None
        strike = c.get("strike")
        if spot is None or strike is None:
            return 4
        interval = config.STRIKE_INTERVAL or 50
        dist_steps = abs(spot - strike) / interval
        if dist_steps <= 1:
            return 10
        if dist_steps <= 2:
            return 8
        if dist_steps <= 3:
            return 6
        if dist_steps <= 5:
            return 4
        return 2

    def _greeks_score(self, c):
        delta = c.get("delta")
        theta = c.get("theta")
        if delta is None:
            return 4
        side = c.get("option_type")
        score = 0
        if 0.35 <= abs(delta) <= 0.65:
            score += 7
        else:
            score += 3
        if theta is not None:
            try:
                theta = float(theta)
            except (TypeError, ValueError):
                theta = None
            if theta is not None and theta < 0:
                score += 3 if abs(theta) < 5 else 1
        return min(score, self.WEIGHTS["greeks"])

    def _iv_score(self, c):
        iv = c.get("iv")
        if iv is None:
            return 2
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            return 2
        if 8 <= iv <= 25:
            return 5
        if 5 <= iv <= 35:
            return 3
        return 1

    def _rr_score(self, c):
        rr = c.get("risk_reward")
        if rr is None:
            return 2
        try:
            rr = float(rr)
        except (TypeError, ValueError):
            return 2
        if rr >= 2.0:
            return 5
        if rr >= config.MIN_RR:
            return 4
        if rr >= 1.0:
            return 2
        return 0
