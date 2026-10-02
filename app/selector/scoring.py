"""Scoring engine (BLOCKORA §15, §16, §17, §18, §19, §21, §22, §23).

The §15 weights are the BLOCKORA §15 initial architecture exactly; they must be
tuned exclusively through validated walk-forward testing (§67, §68) — never
adjusted to improve current output. WEIGHTS and MAX_TOTAL_SCORE are unchanged.

Every component below returns a value NORMALIZED TO 0-100 (§15 "Every candidate
receives a normalized score from 0 to 100"). The weighted total is applied once:

    total = sum(normalized_component * WEIGHTS[component] / 100)

so the final candidate score is still 0-100 and the §15 weights still sum to
100. Components never add up to the score directly.

Missing data rule (BLOCKORA §7, §8): a component whose real inputs are absent
degrades to a documented neutral value and records that its evidence is
unavailable. Nothing is estimated, interpolated or defaulted into a value that
could read as an observation.
"""

from app.config import config

# Underlying push deadband (percentage points). Inside +/- this the underlying
# counts as FLAT rather than weakly directional, so BLOCKORA §17's "premium
# rising while the underlying is flat" is a real, separable state.
_FLAT_BAND_PCT = 0.02


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _clamp(value, low, high):
    return max(low, min(high, value))


class ScoringEngine:
    # §15 initial architecture. DO NOT CHANGE (BLOCKORA §15, §67).
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

    # ------------------------------------------------------------- pipeline

    def score_candidates(self, candidates, underlying, regime):
        peers = self._peer_stats(candidates)
        underlying = underlying or {}

        scored = []
        for c in candidates:
            trend_score = self._underlying_trend_score(c, underlying, regime)
            momentum_score = self._momentum_score(c, underlying)
            oi_score = self._oi_score(c, peers)
            volume_score = self._volume_score(c)
            liquidity_score = self._liquidity_score(c)
            atm_score = self._atm_score(c, underlying)
            greeks_score = self._greeks_score(c, peers)
            iv_score = self._iv_score(c, underlying)
            rr_score = self._rr_score(c)

            normalized = {
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

            total = sum(
                normalized[name] * self.WEIGHTS[name] / 100.0 for name in self.WEIGHTS
            )

            c = dict(c)
            c["scores"] = {k: round(v, 2) for k, v in normalized.items()}
            c["scores_normalized"] = {k: round(v, 2) for k, v in normalized.items()}
            c["total_score"] = round(total, 2)
            c["reasons"] = self._reasons(normalized, c)
            scored.append(c)

        scored.sort(key=lambda x: x["total_score"], reverse=True)
        for i, c in enumerate(scored, start=1):
            c["rank"] = i
        return scored

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _side_sign(option_type):
        """+1 for CE (benefits when the underlying rises), -1 for PE.

        Every directional component is written once against this sign, which is
        what makes CE and PE evaluation symmetric by construction.
        """
        return -1.0 if option_type == "PE" else 1.0

    def _peer_stats(self, candidates):
        """Per (expiry, option_type) medians used for nearby-strike context.

        BLOCKORA §18 asks for comparison against nearby strikes of the same
        expiry and option type; §14 is explicit that high OI is a liquidity /
        positioning fact, NOT a direction signal.
        """
        buckets = {}
        for c in candidates:
            key = (c.get("expiry"), c.get("option_type"))
            buckets.setdefault(key, []).append(c)

        stats = {}
        for key, group in buckets.items():
            ois = []
            gammas = []
            vegas = []
            for c in group:
                for field, sink in (("oi", ois), ("gamma", gammas), ("vega", vegas)):
                    v = c.get(field)
                    if v is None:
                        continue
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                    sink.append(v)
            stats[key] = {
                "count": len(group),
                "oi_median": _median(ois),
                "gamma_median": _median(gammas),
                "vega_median": _median(vegas),
                "strikes": sorted(
                    {c.get("strike") for c in group if c.get("strike") is not None}
                ),
            }
        return stats

    @staticmethod
    def _peer_for(c, peers):
        return peers.get((c.get("expiry"), c.get("option_type")))

    @staticmethod
    def _option_returns(c):
        vals = []
        for key in ("opt_return_1m", "opt_return_3m", "opt_return_5m"):
            v = c.get(key)
            if v is None:
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return vals

    @staticmethod
    def _premium_up(c):
        """True/False for 'premium rising', or None when unknown."""
        for key in ("opt_return_1m", "opt_return_3m", "opt_return_5m"):
            v = c.get(key)
            if v is not None:
                try:
                    return float(v) > 0
                except (TypeError, ValueError):
                    continue
        for key in ("pchange", "change"):
            v = c.get(key)
            if v is not None:
                try:
                    return float(v) > 0
                except (TypeError, ValueError):
                    continue
        return None

    def _underlying_push(self, underlying, sign):
        """Classify the underlying as ALIGNED / FLAT / OPPOSED / None.

        ALIGNED means the underlying is moving in the direction that benefits
        this option type. Used by §17 and §23.
        """
        returns = []
        for key in ("return_1m", "return_3m", "return_5m"):
            v = (underlying or {}).get(key)
            if v is None:
                continue
            try:
                returns.append(sign * float(v))
            except (TypeError, ValueError):
                continue
        if not returns:
            return None
        net = sum(returns) / len(returns)
        if net > _FLAT_BAND_PCT:
            return "ALIGNED"
        if net < -_FLAT_BAND_PCT:
            return "OPPOSED"
        return "FLAT"

    @staticmethod
    def _reasons(normalized, c):
        """Human-readable reasons, keyed to the same thresholds as before."""
        reasons = []

        def strong(name, fraction):
            return normalized[name] / 100.0 >= fraction

        if strong("underlying_trend", 0.70):
            reasons.append("Underlying trend supportive")
        if strong("option_momentum", 0.667):
            reasons.append("Option momentum positive")
        if strong("oi", 0.667):
            reasons.append("OI confirmation")
        if strong("volume", 0.70):
            reasons.append("Volume confirmation")
        if strong("liquidity", 0.70):
            reasons.append("Acceptable liquidity/spread")
        if strong("atm_suitability", 0.70):
            reasons.append("Good ATM proximity")
        if strong("greeks", 0.70):
            reasons.append("Suitable Greeks")
        if strong("iv", 0.60):
            reasons.append("IV behaviour acceptable")
        if strong("risk_reward", 0.60):
            reasons.append("Valid risk/reward")
        return reasons

    # -------------------------------------------------- §16 underlying trend

    def _underlying_trend_score(self, c, underlying, regime):
        """Directional alignment with the underlying (BLOCKORA §16).

        Normalized 0-100. The penalty for a counter-directional side is not
        absolute, per §16.
        """
        side = c.get("option_type")
        trend = (underlying or {}).get("trend", "NEUTRAL")
        spot = (underlying or {}).get("spot")
        vwap = (underlying or {}).get("vwap")

        if regime in ("BULLISH", "BREAKOUT") and side == "CE":
            score = 60.0
        elif regime in ("BEARISH", "BREAKDOWN") and side == "PE":
            score = 60.0
        elif regime in ("BEARISH", "BREAKDOWN") and side == "CE":
            score = 10.0
        elif regime in ("BULLISH", "BREAKOUT") and side == "PE":
            score = 10.0
        else:
            score = 30.0

        if trend == "BULLISH":
            score += 25.0 if side == "CE" else 0.0
        elif trend == "BEARISH":
            score += 25.0 if side == "PE" else 0.0
        else:
            score += 10.0

        if spot is not None and vwap is not None:
            above = spot > vwap
            if (side == "CE" and above) or (side == "PE" and not above):
                score += 15.0

        if regime in ("HIGH_VOLATILITY", "CHOPPY", "NO_CLEAR_REGIME"):
            score = min(score, 40.0)

        return round(_clamp(score, 0.0, 100.0), 2)

    # ------------------------------------------------------- §17 momentum

    def _momentum_score(self, c, underlying):
        """Option momentum from real wall-clock evidence (BLOCKORA §17).

        Sub-factors (normalized, 0-100 total):
          premium direction      0-40
          underlying alignment  0-35
          acceleration          0-15
          relative volume       0-10

        Distinguishes the documented cases:
          A premium rising + underlying moving in the expected direction  (best)
          B premium rising + underlying flat                            (moderate)
          C premium rising against the underlying direction            (penalized)
        """
        sign = self._side_sign(c.get("option_type"))
        opt_returns = self._option_returns(c)
        push = self._underlying_push(underlying, sign)

        evidence = []

        # --- premium direction (0-40) ---------------------------------
        # A RISING premium is constructive for BOTH sides: a CE gains when the
        # underlying rises, a PE gains when it falls, and in both cases the
        # contract's own premium is what moves up. The side sign therefore
        # applies to the UNDERLYING (see _underlying_push), never to the
        # premium itself — applying it here would invert the read for PE.
        if opt_returns:
            premium_mean = sum(opt_returns) / len(opt_returns)
            premium = _clamp(20.0 + premium_mean * 20.0, 0.0, 40.0)
            evidence.append("premium %s (wall-clock)" % (
                "rising" if premium_mean > 0 else "falling"))
        else:
            # No option history yet: fall back to the day change that has
            # always been available, and say so.
            legacy = c.get("pchange")
            if legacy is None:
                legacy = c.get("change")
            try:
                legacy = float(legacy) if legacy is not None else None
            except (TypeError, ValueError):
                legacy = None
            if legacy is None:
                premium = 20.0
                evidence.append("premium momentum unavailable")
            else:
                premium = _clamp(20.0 + legacy * 2.0, 0.0, 40.0)
                evidence.append("premium from day change only")

        # --- underlying alignment (0-35) -------------------------------
        # §17 cases, read against the underlying's push in this option's own
        # favourable direction:
        #   A premium rising + underlying moving in that direction  -> 35
        #   B premium rising + underlying flat                        -> 20
        #   C premium rising against the underlying direction         ->  5
        # A premium that is NOT rising earns no alignment credit at all.
        opt_up = self._premium_up(c)
        if opt_up is None:
            alignment = 17.5
            evidence.append("premium direction unavailable")
        elif not opt_up:
            alignment = 0.0
            evidence.append("premium not rising")
        elif push == "ALIGNED":
            alignment = 35.0
            evidence.append("premium + underlying aligned")
        elif push == "OPPOSED":
            alignment = 5.0
            evidence.append("premium against underlying")
        elif push == "FLAT":
            alignment = 20.0
            evidence.append("premium up, underlying flat")
        else:
            alignment = 22.0
            evidence.append("premium up, underlying direction unknown")

        # --- acceleration (0-15) ---------------------------------------
        accel = c.get("opt_acceleration")
        try:
            accel = float(accel) if accel is not None else None
        except (TypeError, ValueError):
            accel = None
        if accel is None:
            acceleration = 7.5
            evidence.append("acceleration unavailable")
        else:
            acceleration = _clamp(7.5 + accel * 75.0, 0.0, 15.0)
            evidence.append("accelerating" if accel > 0 else "decelerating")

        # --- relative volume (0-10) ------------------------------------
        rv = c.get("relative_volume")
        try:
            rv = float(rv) if rv is not None else None
        except (TypeError, ValueError):
            rv = None
        if rv is None:
            relvol = 5.0
            evidence.append("relative volume unavailable")
        else:
            relvol = _clamp(rv * 5.0, 0.0, 10.0)

        c["momentum_evidence"] = "; ".join(evidence)
        return round(_clamp(premium + alignment + acceleration + relvol, 0.0, 100.0), 2)

    # ------------------------------------------------------- §18 OI + change

    def _oi_score(self, c, peers):
        """OI interpreted with price, volume and peers (BLOCKORA §18).

        Explicitly NOT "high OI = BUY". High OI only contributes through the
        nearby-strike positioning/liquidity factor (BLOCKORA §14).

        Sub-factors (0-100 total):
          OI repositioning       0-30
          price / OI agreement   0-30
          volume agreement       0-20
          nearby-strike OI       0-20
        """
        evidence = []

        oi = _safe_num(c.get("oi"))
        oi_change = _safe_num(c.get("oi_change"))
        oi_change_pct = _safe_num(c.get("oi_change_pct"))
        if oi_change_pct is None and oi_change is None and oi is None:
            c["oi_evidence"] = "OI unavailable"
            return 50.0  # neutral, degraded safely

        # --- OI repositioning magnitude (0-30) -------------------------
        # OI change magnitude is a POSITIONING fact, not a directional one
        # (BLOCKORA §14, §18). The direction of a move comes from combining it
        # with PRICE below, never from the sign of OI alone, and never from
        # flipping the sign for CE vs PE.
        if oi_change_pct is not None:
            build = _clamp(10.0 + min(abs(oi_change_pct), 200.0) * 0.10, 0.0, 30.0)
            evidence.append("OI change %+.1f%% (%s)" % (
                oi_change_pct, "building" if oi_change_pct > 0 else "unwinding"))
        elif oi_change is not None:
            build = _clamp(10.0 + min(abs(oi_change) / 10000.0, 1.0) * 20.0, 0.0, 30.0)
            evidence.append("OI change %+.0f (absolute only)" % oi_change)
        else:
            build = 12.0
            evidence.append("OI change unavailable")

        # --- price / OI agreement (0-30) ------------------------------
        premium_up = self._premium_up(c)
        oi_dir_up = None
        if oi_change_pct is not None:
            oi_dir_up = oi_change_pct > 0
        elif oi_change is not None:
            oi_dir_up = oi_change > 0

        if premium_up is None or oi_dir_up is None:
            agreement = 15.0
            evidence.append("price/OI agreement unavailable")
        elif premium_up and oi_dir_up:
            agreement = 30.0      # price up + OI up: new positions
            evidence.append("price+OI both up")
        elif premium_up and not oi_dir_up:
            agreement = 12.0      # price up + OI down: covering, weaker
            evidence.append("price up, OI down (covering)")
        elif not premium_up and oi_dir_up:
            agreement = 8.0       # pressure building against the premium
            evidence.append("price down, OI up (resistance)")
        else:
            agreement = 0.0       # liquidation
            evidence.append("price down, OI down")

        # --- volume agreement (0-20) ----------------------------------
        rv = _safe_num(c.get("relative_volume"))
        if rv is not None:
            volume_agreement = _clamp(rv * 10.0, 0.0, 20.0)
            evidence.append("rel volume %.2fx" % rv)
        else:
            volume_agreement = 10.0
            evidence.append("volume agreement unavailable")

        # --- nearby-strike OI (0-20) ----------------------------------
        peer = self._peer_for(c, peers)
        peer_median = peer.get("oi_median") if peer else None
        if oi is not None and peer_median and peer_median > 0 and oi > 0:
            ratio = oi / peer_median
            nearby = _clamp(ratio * 10.0, 0.0, 20.0)
            evidence.append("OI %.2fx same-expiry %s median" % (
                ratio, c.get("option_type")))
        else:
            nearby = 10.0
            evidence.append("nearby-strike OI unavailable")

        c["oi_evidence"] = "; ".join(evidence)
        return round(_clamp(build + agreement + volume_agreement + nearby, 0.0, 100.0), 2)

    # ---------------------------------------------------------- §19 volume

    def _volume_score(self, c):
        """Current volume, relative volume and acceleration (BLOCKORA §19).

        Sub-factors (0-100 total): absolute floor 0-40, relative 0-40,
        acceleration 0-20.
        """
        volume = _safe_num(c.get("volume"))
        if volume is None or volume < 0:
            c["volume_evidence"] = "volume unavailable"
            return 30.0  # same neutral-low as the previous absolute-only rule

        if volume >= 100000:
            absolute = 40.0
        elif volume >= 50000:
            absolute = 32.0
        elif volume >= 10000:
            absolute = 24.0
        elif volume >= 1000:
            absolute = 14.0
        else:
            absolute = 6.0

        evidence = ["volume %.0f" % volume]

        rv = _safe_num(c.get("relative_volume"))
        if rv is not None:
            relative = _clamp(rv * 20.0, 0.0, 40.0)
            evidence.append("rel volume %.2fx" % rv)
        else:
            relative = 0.0
            evidence.append("relative volume unavailable")

        accel = _safe_num(c.get("volume_acceleration"))
        if accel is not None and accel >= 0:
            acceleration = _clamp(accel * 12.5, 0.0, 20.0)
            evidence.append("volume %s" % ("expanding" if accel > 1 else "steady"))
        elif rv is not None:
            acceleration = 10.0
        else:
            acceleration = 0.0
            evidence.append("acceleration unavailable")

        c["volume_evidence"] = "; ".join(evidence)
        return round(_clamp(absolute + relative + acceleration, 0.0, 100.0), 2)

    # --------------------------------------------------------- liquidity

    def _liquidity_score(self, c):
        bid = _safe_num(c.get("bid"))
        ask = _safe_num(c.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return 20.0
        spread = ask - bid
        mid = (ask + bid) / 2.0
        if mid <= 0:
            return 20.0
        spread_pct = (spread / mid) * 100.0
        if spread_pct <= 2:
            return 100.0
        if spread_pct <= 4:
            return 80.0
        if spread_pct <= 6:
            return 50.0
        if spread_pct <= config.MAX_SPREAD_PERCENT:
            return 30.0
        return 0.0

    # ------------------------------------------------- §21 ATM / ITM / OTM

    @staticmethod
    def classify_moneyness(spot, strike, option_type, interval=None):
        """ITM / ATM / OTM from real spot and strike (BLOCKORA §21).

        ATM band is half a strike interval either side of spot, which is the
        granularity at which the strike grid can meaningfully distinguish ATM.
        Returns None when spot or strike is unknown — never a guess.
        """
        if spot is None or strike is None or option_type not in ("CE", "PE"):
            return None
        try:
            spot = float(spot)
            strike = float(strike)
        except (TypeError, ValueError):
            return None
        diff = strike - spot
        if diff == 0:
            return "ATM"
        band = (interval or 0) / 2.0
        if band and abs(diff) <= band:
            return "ATM"
        if option_type == "CE":
            return "ITM" if diff < 0 else "OTM"
        return "ITM" if diff > 0 else "OTM"

    def _atm_score(self, c, underlying):
        """Strike/ATM suitability (BLOCKORA §21).

        Owns distance-from-ATM and moneyness/probability only. Liquidity is
        scored by its own §14 component and risk/reward by §24, so this
        component deliberately does not re-use them.

        Sub-factors (0-100 total): distance 0-55, moneyness 0-45.
        """
        spot = _safe_num((underlying or {}).get("spot"))
        strike = _safe_num(c.get("strike"))
        interval = config.STRIKE_INTERVAL or 50
        moneyness = self.classify_moneyness(spot, strike, c.get("option_type"), interval)
        c["moneyness"] = moneyness
        c["strike_interval"] = interval

        if spot is None or strike is None:
            c["atm_evidence"] = "spot/strike unavailable"
            return 25.0

        dist_steps = abs(spot - strike) / interval
        c["distance_steps"] = round(dist_steps, 3)

        if dist_steps <= 1:
            distance = 55.0
        elif dist_steps <= 2:
            distance = 45.0
        elif dist_steps <= 3:
            distance = 33.0
        elif dist_steps <= 5:
            distance = 20.0
        else:
            distance = 8.0

        if moneyness == "ATM":
            probability = 45.0
        elif moneyness == "ITM":
            # In-the-money carries higher probability but the least need for a
            # move; deep ITM converges toward the intrinsic value.
            probability = 38.0
        elif moneyness == "OTM":
            # Far OTM needs a bigger underlying move: §21 penalizes it.
            probability = _clamp(45.0 * (1.0 - dist_steps / 5.0), 0.0, 30.0)
        else:
            probability = 22.5

        c["atm_evidence"] = "%s, %.2f steps from ATM" % (moneyness or "unknown", dist_steps)
        return round(_clamp(distance + probability, 0.0, 100.0), 2)

    # ----------------------------------------------------------- §22 Greeks

    def _greeks_score(self, c, peers):
        """Delta / gamma / theta / vega read against the intended move (§22).

        Requires real Greeks. attach_greeks() only writes them when a valid IV
        exists, so an absent delta means "not calculable", never zero.

        Sub-factors (0-100 total): delta 0-40, gamma 0-25, theta 0-20,
        vega 0-15.
        """
        delta = _safe_num(c.get("delta"))
        gamma = _safe_num(c.get("gamma"))
        theta = _safe_num(c.get("theta"))
        vega = _safe_num(c.get("vega"))

        if delta is None:
            c["greeks_evidence"] = "Greeks unavailable (no valid IV)"
            return 50.0  # neutral, degraded safely

        sign = self._side_sign(c.get("option_type"))
        # CE delta in [0,1]; PE delta in [-1,0]. sign*delta is therefore the
        # directional usefulness of the delta for BOTH sides -> symmetry.
        adj_delta = sign * delta
        evidence = ["delta %.3f (%s)" % (delta, c.get("option_type"))]

        if 0.30 <= adj_delta <= 0.70:
            delta_score = 40.0
            evidence.append("delta in useful band")
        elif 0.15 <= adj_delta <= 0.85:
            delta_score = 28.0
        else:
            delta_score = 12.0
            evidence.append("delta weakly directional")

        peer = self._peer_for(c, peers)

        gamma_median = peer.get("gamma_median") if peer else None
        if gamma is not None and gamma_median and gamma_median > 0:
            gamma_score = _clamp((gamma / gamma_median) * 12.5, 0.0, 25.0)
            evidence.append("gamma %.2fx peer median" % (gamma / gamma_median))
        else:
            gamma_score = 12.5
            evidence.append("gamma unavailable")

        ltp = _safe_num(c.get("ltp"))
        if theta is not None and ltp and ltp > 0:
            theta_pct = abs(theta) / ltp * 100.0
            # Lower daily decay burden relative to premium scores higher.
            if theta_pct <= 1.0:
                theta_score = 20.0
            elif theta_pct >= 4.0:
                theta_score = 0.0
            else:
                theta_score = _clamp((4.0 - theta_pct) / 3.0 * 20.0, 0.0, 20.0)
            evidence.append("theta %.2f%%/day of premium" % theta_pct)
        else:
            theta_score = 10.0
            evidence.append("theta unavailable")

        vega_median = peer.get("vega_median") if peer else None
        if vega is not None and vega_median and vega_median > 0:
            vega_score = _clamp((vega / vega_median) * 7.5, 0.0, 15.0)
            evidence.append("vega %.2fx peer median" % (vega / vega_median))
        else:
            vega_score = 7.5
            evidence.append("vega unavailable")

        c["greeks_evidence"] = "; ".join(evidence)
        return round(
            _clamp(delta_score + gamma_score + theta_score + vega_score, 0.0, 100.0), 2
        )

    # --------------------------------------------------------------- §23 IV

    def _iv_score(self, c, underlying):
        """IV level, IV change and IV vs recent (BLOCKORA §23).

        §23 requires that a premium rise caused ONLY by IV expansion be treated
        differently from a move supported by the underlying, so the change
        factor reads the underlying push directly.

        Sub-factors (0-100 total): level 0-30, change reading 0-50,
        recent percentile 0-20.
        """
        iv = _safe_num(c.get("iv"))
        if iv is None or iv <= 0:
            c["iv_evidence"] = "IV unavailable"
            return 50.0  # neutral, degraded safely

        if 8 <= iv <= 25:
            level = 30.0
        elif 5 <= iv <= 35:
            level = 20.0
        else:
            level = 10.0

        sign = self._side_sign(c.get("option_type"))
        push = self._underlying_push(underlying, sign)
        iv_change = _safe_num(c.get("iv_change"))
        evidence = ["IV %.2f" % iv]

        # --- IV change reading (0-50) ---------------------------------
        if iv_change is None:
            change_score = 25.0
            evidence.append("IV change unavailable")
        elif iv_change == 0:
            change_score = 25.0
            evidence.append("IV unchanged")
        elif iv_change > 0:
            if push == "ALIGNED":
                change_score = 50.0       # premium + IV up, underlying agrees
                evidence.append("IV up with underlying move")
            elif push == "OPPOSED":
                change_score = 12.0
                evidence.append("IV up, underlying opposed")
            else:
                change_score = 8.0        # premium + IV up, underlying FLAT
                evidence.append("IV expansion only, underlying flat")
        else:
            if push == "ALIGNED":
                change_score = 35.0
                evidence.append("IV down, underlying move leads")
            elif push == "OPPOSED":
                change_score = 5.0
            else:
                change_score = 15.0
                evidence.append("IV contracting")
            evidence.append("IV change %+.2f" % iv_change)

        # --- IV vs recent observations (0-20) -------------------------
        percentile = _safe_num(c.get("iv_percentile"))
        samples = c.get("iv_samples") or 0
        if percentile is not None:
            if percentile <= 25:
                recent = 20.0
            elif percentile >= 80:
                recent = 4.0
            else:
                recent = _clamp(20.0 - (percentile - 25.0) / 55.0 * 16.0, 4.0, 20.0)
            evidence.append("IV percentile %.0f (n=%s)" % (percentile, samples))
        else:
            # BLOCKORA §23/§65: never invent a percentile from too few samples.
            recent = 10.0
            evidence.append("IV percentile unavailable (n=%s < %s)" % (
                samples, config.IV_PERCENTILE_MIN_SAMPLES))

        c["iv_evidence"] = "; ".join(evidence)
        return round(_clamp(level + change_score + recent, 0.0, 100.0), 2)

    # --------------------------------------------------------- §24 risk/reward

    def _rr_score(self, c):
        rr = _safe_num(c.get("risk_reward"))
        if rr is None:
            return 20.0
        if rr >= 2.0:
            return 100.0
        if rr >= config.MIN_RR:
            return 80.0
        if rr >= 1.0:
            return 40.0
        return 0.0


def _safe_num(value):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v