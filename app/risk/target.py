class TargetEngine:
    @staticmethod
    def build_target(candidate, entry_high, stop_loss, min_rr=1.5):
        if entry_high is None or stop_loss is None:
            return None, None, None

        risk = entry_high - stop_loss
        if risk <= 0:
            return None, None, None

        reward = risk * min_rr
        target = entry_high + reward
        rr = reward / risk
        return round(target, 2), round(rr, 2), round(reward, 2)
