class OutcomeEngine:
    @staticmethod
    def detect_state(previous_cycle, current_best, current_score, current_confidence):
        if previous_cycle is None:
            return "NEW"

        prev_symbol = previous_cycle.get("best_symbol")
        curr_symbol = f"{current_best.get('underlying')} {current_best.get('strike')} {current_best.get('option_type')}" if current_best else None

        if curr_symbol and prev_symbol == curr_symbol:
            prev_score = previous_cycle.get("best_score") or 0
            delta = current_score - prev_score
            if delta >= 3:
                return "STRENGTHENING"
            if delta <= -3:
                return "WEAKENING"
            return "STABLE"

        if curr_symbol and prev_symbol != curr_symbol:
            return "NEW BEST"

        return "NO SIGNAL"
