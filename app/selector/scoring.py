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

    MAX_TOTAL_SCORE = sum(WEIGHTS.values())  # 90


    def score_candidates(self, candidates, underlying, regime):
        scored = []
        for c in candidates:
            total = 0.0
            reasons = []

            trend_score = self._underlying_trend_score(c, underlying, regime)
            momentum_score = self._momentum_score(c)
            oi_score = self._oi_score(c)
            volume_score = self._volume_score(c)
            liquidity_score = self._liquidity_score(c)
            atm_score = self._atm_score(c, underlying)
            greeks_score = self._greeks_score(c)
            iv_score = self._iv_score(c)
            rr_score = self._rr_score(c)

            total += trend_score
            total += momentum_score
            total += oi_score
            total += volume_score
            total += liquidity_score
            total += atm_score
            total += greeks_score
            total += iv_score
            total += rr_score

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
            # Total score is raw sum 0-90 (per WEIGHTS), used directly in confidence calc
            c["total_score"] = round(total, 2)
            c["reasons"] = reasons
            scored.append(c)

        scored.sort(key=lambda x: x["total_score"], reverse=True)
        for i, c in enumerate(scored, start=1):
            c["rank"] = i
        return scored

    def _underlying_trend_score(self, c, underlying, regime):
        score = 0
        side = c.get("option_type")
        trend = underlying.get("trend") if underlying else "NEUTRAL"

        if regime in ("BULLISH", "BREAKOUT") and side == "CE":
            score += 14
        elif regime in ("BEARISH", "BREAKDOWN") and side == "PE":
            score += 14
        elif trend == "BULLISH" and side == "CE":
            score += 10
        elif trend == "BEARISH" and side == "PE":
            score += 10
        elif trend == "BULLISH" and side == "PE":
            score += 2
        elif trend == "BEARISH" and side == "CE":
            score += 2
        else:
            score += 5

        if regime in ("HIGH_VOLATILITY", "CHOPPY", "NO_CLEAR_REGIME"):
            score = min(score, 8)
        return min(score, 20)

    def _momentum_score(self, c):
        change = c.get("change")
        pchange = c.get("pchange")

        # If both missing -> no evidence of momentum, return neutral 3
        # (lowest meaningful score, not a misleading positive)
        if change is None and pchange is None:
            return 3

        # If pchange is available, use it
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
                return 3  # negative pchange = bearish, low score

        # If only change is available
        if change is not None:
            try:
                cg = float(change)
            except (TypeError, ValueError):
                cg = None
            if cg is not None:
                if cg > 0:
                    return 6
                return 3  # negative change = bearish

        # Default neutral
        return 3

    def _oi_score(self, c):
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
        if oi_change is not None:
            if oi_change > 0:
                score += 8
            elif oi_change < 0:
                score += 2
        return min(score, 15)

    def _volume_score(self, c):
        vol = c.get("volume")
        if vol is None:
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
        ltp = c.get("ltp")
        if bid is None or ask is None or ask <= 0:
            return 2
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
        dist = abs(spot - strike)
        if dist <= 50:
            return 10
        if dist <= 100:
            return 8
        if dist <= 150:
            return 6
        if dist <= 250:
            return 4
        return 2

    def _greeks_score(self, c):
        delta = c.get("delta")
        theta = c.get("theta")
        if delta is None:
            return 4
        side = c.get("option_type")
        score = 0
        if side == "CE" and 0.35 <= abs(delta) <= 0.65:
            score += 7
        elif side == "PE" and 0.35 <= abs(delta) <= 0.65:
            score += 7
        else:
            score += 3
        if theta is not None and theta < 0:
            score += 3 if abs(theta) < 5 else 1
        return min(score, 10)

    def _iv_score(self, c):
        iv = c.get("iv")
        if iv is None:
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
        if rr >= 2.0:
            return 5
        if rr >= config.MIN_RR:
            return 4
        if rr >= 1.0:
            return 2
        return 0
