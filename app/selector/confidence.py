from app.config import config


class ConfidenceEngine:
    @staticmethod
    def confidence_band(value):
        if value is None:
            return "NO CLEAR SIGNAL"
        if value >= 90:
            return "VERY HIGH"
        if value >= 80:
            return "HIGH"
        if value >= 70:
            return "MEDIUM"
        if value >= 60:
            return "LOW"
        return "NO CLEAR SIGNAL"

    def calculate(self, best_candidate, data_quality, regime, previous_cycle=None):
        if not best_candidate:
            return 0.0, "NO CLEAR SIGNAL"

        # total_score is raw sum 0-90 (from WEIGHTS), used directly
        score = best_candidate.get("total_score", 0)
        # Clamp to valid 0-90 range (defensive, should already be in range)
        score = max(0, min(90, float(score)))

        rr = best_candidate.get("risk_reward") or 0
        # Normalize RR to 0-1 range for the formula
        try:
            rr_norm = min(float(rr) / 2.0, 1.0) if rr is not None else 0.0
        except (TypeError, ValueError):
            rr_norm = 0.0

        liquidity = best_candidate.get("scores", {}).get("liquidity", 0)
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0.0

        source_agreement = 80 if best_candidate.get("source") else 50
        # source_agreement is already 0-100

        regime_clarity = 85 if regime in ("BULLISH", "BEARISH", "BREAKOUT", "BREAKDOWN") else 55
        # regime_clarity is already 0-100

        historical_evidence = 70
        previous_consistency = 70

        if previous_cycle:
            prev_score = previous_cycle.get("best_score")
            curr_score = best_candidate.get("total_score")
            if prev_score is not None and curr_score is not None:
                if curr_score >= prev_score:
                    previous_consistency = 85
                else:
                    previous_consistency = 55

        confidence = (
            score * 0.35 +
            data_quality * 0.20 +
            rr_norm * 100 * 0.10 +
            liquidity * 10 * 0.10 +
            source_agreement * 0.10 +
            regime_clarity * 0.10 +
            historical_evidence * 0.03 +
            previous_consistency * 0.02
        )

        # Clamp final confidence to 0-100 range
        confidence = max(0, min(100, round(confidence, 2)))
        return confidence, self.confidence_band(confidence)
