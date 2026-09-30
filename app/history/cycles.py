from datetime import datetime
import pytz

from app.history.database import db

IST = pytz.timezone("Asia/Kolkata")


class CycleHistory:
    @staticmethod
    def save_cycle(cycle):
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute("""
            INSERT INTO cycles (
                timestamp, underlying, spot, expiry, regime, data_quality,
                best_symbol, best_score, confidence, entry, stop_loss,
                target, risk_reward, signal_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ts,
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
    def save_candidate_scores(cycle_id, candidates):
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
