"""Target calculation (BLOCKORA §27).

Target = entry + reward where reward is derived from the expected underlying
movement (ATR-based, a documented initial model) mapped through option
responsiveness, falling back to the MIN_RR multiple. If no valid target can
be calculated the result is None and the candidate cannot become BEST.
"""


class TargetEngine:
    @staticmethod
    def build_target(candidate, entry_high, stop_loss, min_rr=1.5, underlying=None):
        if entry_high is None or stop_loss is None:
            return None, None, None

        risk = entry_high - stop_loss
        if risk <= 0:
            return None, None, None

        expected_underlying_move = None
        atr = underlying.get("atr") if underlying else None
        spot = underlying.get("spot") if underlying else None
        if atr is not None and spot:
            expected_underlying_move = atr * 1.5  # documented initial multiplier

        # Option responsiveness proxy: |delta| when calculable, else None.
        delta = candidate.get("delta")
        responsiveness = None
        if delta is not None:
            try:
                responsiveness = abs(float(delta))
            except (TypeError, ValueError):
                responsiveness = None

        if expected_underlying_move and responsiveness:
            expected_premium_move = expected_underlying_move * responsiveness
            reward = max(risk * min_rr, expected_premium_move)
        else:
            reward = risk * min_rr

        target = entry_high + reward
        rr = reward / risk
        return round(target, 2), round(rr, 2), round(reward, 2)
