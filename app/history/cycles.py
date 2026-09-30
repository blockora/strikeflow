from datetime import datetime

import pytz

from app.history.database import db

IST = pytz.timezone("Asia/Kolkata")


def _now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


class CycleHistory:
    @staticmethod
    def save_cycle(cycle):
        """Persist one analysis cycle (BLOCKORA §32) and return its cycle_id."""
        cur = db.execute("""
            INSERT INTO cycles (
                timestamp, underlying, spot, expiry, regime, data_quality,
                best_symbol, best_score, confidence, entry, stop_loss,
                target, risk_reward, signal_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _now_str(),
            cycle.get("underlying"),
            cycle.get("spot"),
            cycle.get("expiry"),
            cycle.get("regime"),
            cycle.get("data_quality"),
            cycle.get("best_symbol"),
            cycle.get("best_score"),
            cycle.get("confidence"),
            cycle.get("entry"),
            cycle.get("stop_loss"),
            cycle.get("target"),
            cycle.get("risk_reward"),
            cycle.get("signal_state"),
        ))
        return cur.lastrowid

    @staticmethod
    def latest_cycle():
        return db.fetchone("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1")

    @staticmethod
    def save_market_snapshot(cycle_id, underlying_snapshot, regime, data_quality, source=None):
        db.execute("""
            INSERT INTO market_snapshots (
                cycle_id, timestamp, underlying, spot, source, vwap, atr,
                trend, regime, data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cycle_id,
            _now_str(),
            underlying_snapshot.get("symbol"),
            underlying_snapshot.get("spot"),
            source or "UNKNOWN",
            underlying_snapshot.get("vwap"),
            underlying_snapshot.get("atr"),
            underlying_snapshot.get("trend"),
            regime,
            data_quality,
        ))

    @staticmethod
    def save_option_snapshot(cycle_id, underlying, candidates):
        """Persist per-contract chain observations for the cycle (§60)."""
        ts = _now_str()
        for c in candidates:
            db.execute("""
                INSERT INTO option_snapshots (
                    cycle_id, timestamp, underlying, expiry, strike, option_type,
                    ltp, bid, ask, volume, oi, oi_change, iv, source, data_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle_id,
                ts,
                c.get("underlying"),
                c.get("expiry"),
                c.get("strike"),
                c.get("option_type"),
                c.get("ltp"),
                c.get("bid"),
                c.get("ask"),
                c.get("volume"),
                c.get("oi"),
                c.get("oi_change"),
                c.get("iv"),
                c.get("source"),
                None,
            ))

    @staticmethod
    def save_candidate_scores(cycle_id, candidates):
        """Persist scored candidates (BLOCKORA §62) for the given cycle."""
        for c in candidates:
            scores = c.get("scores", {})
            db.execute("""
                INSERT INTO candidate_scores (
                    cycle_id, symbol, strike, option_type, ltp, bid, ask,
                    volume, oi, oi_change, iv, delta, gamma, theta, vega,
                    momentum_score, oi_score, volume_score, liquidity_score,
                    greeks_score, iv_score, risk_reward_score, total_score, rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle_id,
                f"{c.get('underlying')} {c.get('strike')} {c.get('option_type')}",
                c.get("strike"),
                c.get("option_type"),
                c.get("ltp"),
                c.get("bid"),
                c.get("ask"),
                c.get("volume"),
                c.get("oi"),
                c.get("oi_change"),
                c.get("iv"),
                c.get("delta"),
                c.get("gamma"),
                c.get("theta"),
                c.get("vega"),
                scores.get("option_momentum"),
                scores.get("oi"),
                scores.get("volume"),
                scores.get("liquidity"),
                scores.get("greeks"),
                scores.get("iv"),
                scores.get("risk_reward"),
                c.get("total_score"),
                c.get("rank"),
            ))
