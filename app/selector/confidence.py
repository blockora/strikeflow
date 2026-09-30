"""Confidence engine (BLOCKORA §28, §65, §66).

Confidence is a separate layer from the raw score, on a 0-100 scale. It is
NOT a probability. Historical evidence uses real outcome samples from SQLite
and is gated by MIN_HISTORICAL_SAMPLES (§65): small samples receive a
reliability penalty instead of being extrapolated (§66).
"""

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

    def calculate(self, best_candidate, data_quality, regime, previous_cycle=None, historical_stats=None):
        if not best_candidate:
            return 0.0, "NO CLEAR SIGNAL"

        score = best_candidate.get("total_score", 0)
        try:
            score = max(0.0, min(100.0, float(score)))
        except (TypeError, ValueError):
            score = 0.0

        rr = best_candidate.get("risk_reward")
        try:
            rr_norm = min(float(rr) / 2.0, 1.0) if rr is not None else 0.0
        except (TypeError, ValueError):
            rr_norm = 0.0

        liquidity = best_candidate.get("scores", {}).get("liquidity", 0)
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0.0

        # Source agreement: real conflict check feeds this in the caller via
        # historical_stats["source_agreement"]; default is unknown, not great.
        source_agreement = 50
        if historical_stats and historical_stats.get("source_agreement") is not None:
            source_agreement = historical_stats["source_agreement"]

        regime_clarity = 85 if regime in ("BULLISH", "BEARISH", "BREAKOUT", "BREAKDOWN") else 55

        # Historical evidence from real outcome samples only (§65, §66).
        historical_evidence = 50
        if historical_stats:
            samples = historical_stats.get("samples", 0)
            if samples and samples >= config.MIN_HISTORICAL_SAMPLES:
                historical_evidence = 50 + min(40.0, (samples / 100.0) * 40.0)
                wins = historical_stats.get("target_reached", 0)
                historical_evidence += min(10.0, (wins / samples) * 100.0 * 0.1)
            else:
                # Small sample: reliability penalty (§66), never 100% confidence.
                historical_evidence = 50

        previous_consistency = 50
        if previous_cycle:
            prev_score = previous_cycle.get("best_score")
            if prev_score is not None:
                if score >= float(prev_score):
                    previous_consistency = 85
                else:
                    previous_consistency = 55

        confidence = (
            score * 0.30
            + data_quality * 0.20
            + rr_norm * 100 * 0.10
            + liquidity * 10 * 0.10
            + source_agreement * 0.10
            + regime_clarity * 0.10
            + historical_evidence * 0.05
            + previous_consistency * 0.05
        )

        confidence = max(0.0, min(100.0, round(confidence, 2)))
        return confidence, self.confidence_band(confidence)
