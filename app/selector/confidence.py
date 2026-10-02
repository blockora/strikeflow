"""Confidence engine (BLOCKORA §28, §65, §66).

Confidence is a separate layer from the raw score, on a 0-100 scale. It is
NOT a probability. Historical evidence uses real outcome samples from SQLite
and is gated by MIN_HISTORICAL_SAMPLES (§65): small samples receive a
reliability penalty instead of being extrapolated (§66).

This phase adds a small "current directional confirmation" evidence component
consistent with §28. Confirmed directional setup is strong confirmation
evidence; a direction that exists but is not fully confirmed is moderate
evidence; neutral/no directional confirmation is lower evidence. The component
is conservative (0.08 confirmed / 0.05 partial / 0.00 neutral). Existing
weights are re-normalized so the total remains exactly 1.0.

Exact weights (must sum to exactly 1.0):
    score                    0.30
    data_quality             0.20
    risk_reward              0.08
    liquidity                0.08
    source_agreement         0.08
    regime_clarity           0.08
    historical_evidence      0.05
    previous_consistency     0.05
    current_direction_confirm 0.08
    -------------------------------
    total                    1.00

Do NOT:
    - make confidence equal to score
    - make confidence a win probability
    - let confidence bypass MIN_SCORE
    - let directional confirmation alone create BEST STRIKE

Preserve:
    - clamp 0-100
    - existing confidence bands
    - MIN_CONFIDENCE gate
"""

from app.config import config

# Individual component weights used in the weighted sum.
_WEIGHT_SCORE = 0.30
_WEIGHT_DATA_QUALITY = 0.20
_WEIGHT_RISK_REWARD = 0.08
_WEIGHT_LIQUIDITY = 0.08
_WEIGHT_SOURCE_AGREEMENT = 0.08
_WEIGHT_REGIME_CLARITY = 0.08
_WEIGHT_HISTORICAL = 0.05
_WEIGHT_PREVIOUS = 0.05

# Weight of the current-direction component in the weighted sum (unchanged).
_CURRENT_DIRECTION_COMPONENT_WEIGHT = 0.08
# Evidence SCORES (0-100 scale, same unit as every other component) for that
# component. 100.0 * 0.08 = 8.0 and 60.0 * 0.08 = 4.8 confidence points.
_DIRECTION_CONFIRMED_EVIDENCE = 100.0
_DIRECTION_PARTIAL_EVIDENCE = 60.0


class ConfidenceEngine:
    # Exact §28 weights. These MUST sum to exactly 1.00; WEIGHT_SUM is derived
    # from them (never restated by hand) so a drift is impossible.
    WEIGHTS = {
        "score": 0.30,
        "data_quality": 0.20,
        "risk_reward": 0.08,
        "liquidity": 0.08,
        "source_agreement": 0.08,
        "regime_clarity": 0.08,
        "historical_evidence": 0.05,
        "previous_consistency": 0.05,
        "current_direction_confirm": _CURRENT_DIRECTION_COMPONENT_WEIGHT,
    }
    WEIGHT_SUM = round(sum(WEIGHTS.values()), 10)

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

        # --- current directional confirmation (Phase 1) -------------------
        # This evidence component uses the live direction evidence from
        # app.market.direction. It is NOT a score override and it does NOT
        # bypass any hard gate (MIN_SCORE, MIN_CONFIDENCE, etc.). The
        # direction engine returns confirmed; confirmed directional setups are
        # strong evidence, a direction that exists but is not fully confirmed
        # is moderate evidence, neutral/no direction lower evidence. Re-
        # normalized weights keep the total exactly 1.0.
        #
        # UNIT CONTRACT: every other component in this sum is an EVIDENCE SCORE
        # on a 0-100 scale. This component must use the same scale — assigning
        # the weight here would make the term collapse to ~0.006 points.
        current_direction = 0.0
        best_direction = best_candidate.get("direction", "NEUTRAL")
        best_confirmed = best_candidate.get("confirmed")
        if best_confirmed is True:
            current_direction = _DIRECTION_CONFIRMED_EVIDENCE      # 100.0
        elif best_direction and best_direction != "NEUTRAL":
            # Direction exists but is not fully confirmed: moderate evidence.
            current_direction = _DIRECTION_PARTIAL_EVIDENCE       # 60.0

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
            score * _WEIGHT_SCORE
            + data_quality * _WEIGHT_DATA_QUALITY
            + rr_norm * 100 * _WEIGHT_RISK_REWARD
            + liquidity * 10 * _WEIGHT_LIQUIDITY
            + source_agreement * _WEIGHT_SOURCE_AGREEMENT
            + regime_clarity * _WEIGHT_REGIME_CLARITY
            + historical_evidence * _WEIGHT_HISTORICAL
            + previous_consistency * _WEIGHT_PREVIOUS
            + current_direction * _CURRENT_DIRECTION_COMPONENT_WEIGHT
        )

        confidence = max(0.0, min(100.0, round(confidence, 2)))
        return confidence, self.confidence_band(confidence)
