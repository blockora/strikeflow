"""Backtesting over stored historical data (BLOCKORA §41, §42, §44).

Integrity rules enforced by this module:

- Only data stored in SQLite (cycles / market_snapshots / option_snapshots)
  is used. There is NO live fetching here; today's chain must never be
  replayed as history.
- Inputs for a historical decision are FROZEN at that cycle: only rows stored
  for that cycle_id are visible to the engine.
- Outcomes are resolved strictly from LATER stored snapshots (cycle_id >),
  never from the decision cycle itself (no look-ahead).
- Execution realism: entries fill at ask + slippage, exits at bid - slippage,
  plus round-trip transaction costs (§44).
- If the required historical option-chain snapshots do not exist, this module
  prints BACKTEST NOT READY and states exactly what is missing. It never
  manufactures historical data.

This is a CLI: python -m app.backtesting [--limit N]
"""

import argparse
import sys
from datetime import datetime

import pytz

from app.config import config
from app.history.database import db
from app.market.regime import MarketRegimeEngine
from app.market.underlying import UnderlyingEngine
from app.risk.entry import EntryEngine
from app.risk.stoploss import StopLossEngine
from app.risk.target import TargetEngine
from app.selector.ranking import RankingEngine
from app.selector.scoring import ScoringEngine

IST = pytz.timezone("Asia/Kolkata")


def historical_data_status():
    """Report exactly what historical data exists for backtesting."""
    cycles = db.fetchone("SELECT COUNT(*) AS n FROM cycles")
    snaps = db.fetchone("SELECT COUNT(*) AS n FROM option_snapshots")
    quality = db.fetchone(
        "SELECT COUNT(DISTINCT cycle_id) AS n FROM option_snapshots WHERE bid IS NOT NULL AND ask IS NOT NULL"
    )
    return {
        "cycles": cycles["n"] if cycles else 0,
        "option_snapshots": snaps["n"] if snaps else 0,
        "cycles_with_bid_ask": quality["n"] if quality else 0,
    }


def is_backtest_ready(status=None):
    status = status or historical_data_status()
    return (
        status["cycles"] > 0
        and status["option_snapshots"] > 0
        and status["cycles_with_bid_ask"] > 0
    )


def load_historical_cycles(limit=None):
    query = "SELECT * FROM cycles ORDER BY cycle_id ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    return [dict(r) for r in db.fetchall(query)]


def load_snapshot_for_cycle(cycle_id):
    """Reconstruct the frozen candidate universe stored for one cycle."""
    rows = db.fetchall(
        "SELECT * FROM option_snapshots WHERE cycle_id = ? ORDER BY strike, option_type",
        (cycle_id,),
    )
    return [dict(r) for r in rows]


def load_market_snapshot_for_cycle(cycle_id):
    row = db.fetchone("SELECT * FROM market_snapshots WHERE cycle_id = ? LIMIT 1", (cycle_id,))
    return dict(row) if row else None


def reconstruct_candidates(snapshot_rows):
    """Build production candidate dicts from stored option snapshots.

    Uses the same liquidity rule as live candidate generation; missing
    bid/ask stays missing (frozen real data only).
    """
    from app.selector.candidates import passes_liquidity_filter, spread_percent

    candidates = []
    for r in snapshot_rows:
        opt = {
            "ltp": r.get("ltp"),
            "bid": r.get("bid"),
            "ask": r.get("ask"),
            "volume": r.get("volume"),
            "oi": r.get("oi"),
            "oi_change": r.get("oi_change"),
            "iv": r.get("iv"),
        }
        try:
            ltp = float(r["ltp"]) if r.get("ltp") is not None else None
        except (TypeError, ValueError):
            ltp = None
        if ltp is None or ltp <= 0:
            continue
        if not passes_liquidity_filter(opt):
            continue
        candidates.append({
            "underlying": r.get("underlying"),
            "strike": r.get("strike"),
            "option_type": r.get("option_type"),
            "expiry": r.get("expiry"),
            "ltp": ltp,
            "bid": r.get("bid"),
            "ask": r.get("ask"),
            "volume": r.get("volume"),
            "oi": r.get("oi"),
            "oi_change": r.get("oi_change"),
            "iv": r.get("iv"),
            "source": r.get("source") or "JUGAAD",
        })
    return candidates


def reconstruct_underlying_snapshot(market_snapshot, cycles_before):
    """Frozen underlying context from stored data only."""
    if market_snapshot:
        return {
            "symbol": market_snapshot.get("underlying"),
            "spot": market_snapshot.get("spot"),
            "vwap": market_snapshot.get("vwap"),
            "atr": market_snapshot.get("atr"),
            "trend": market_snapshot.get("trend"),
            "return_1m": None,
            "return_3m": None,
            "return_5m": None,
            "ma_fast": None,
            "ma_slow": None,
            "realized_volatility": None,
        }
    # Fall back to the stored cycle row itself (spot/regime were frozen there).
    return None


def replay_cycle(cycle, candidates, underlying_snapshot, regime):
    """Run the identical production scoring/ranking path on frozen inputs."""
    scoring = ScoringEngine()
    ranking = RankingEngine()
    scored = scoring.score_candidates(candidates, underlying_snapshot, regime)
    best, _top3, gate_reason = ranking.select_best(scored)
    return best, gate_reason, scored


def resolve_outcome(cycle_id, best, stored_cycles_by_id):
    """Resolve the decision outcome from strictly later stored snapshots.

    Execution model (§44): entry at ask + slippage, exit at bid - slippage,
    plus round-trip transaction costs as a percent of premium.
    """
    entry_ask = best.get("ask")
    if entry_ask is None:
        return None  # cannot execute realistically without an ask

    slip = config.BACKTEST_SLIPPAGE_PCT / 100.0
    cost = config.BACKTEST_TXN_COST_PCT / 100.0
    entry_price = entry_ask * (1 + slip)
    stop = best.get("stop_loss")
    target = best.get("target")
    result = None
    exit_price = None
    exit_cycle_id = None

    later = db.fetchall(
        """SELECT o.* FROM option_snapshots o
           JOIN cycles c ON c.cycle_id = o.cycle_id
           WHERE o.underlying = ? AND o.strike = ? AND o.option_type = ? AND o.expiry = ?
             AND o.cycle_id > ?
           ORDER BY o.cycle_id ASC""",
        (best.get("underlying"), best.get("strike"), best.get("option_type"),
         best.get("expiry"), cycle_id),
    )
    for row in later:
        bid = row["bid"]
        ltp = row["ltp"]
        if bid is not None:
            bid = float(bid)
            if target is not None and bid >= float(target):
                result = "TARGET_REACHED"
                exit_price = bid * (1 - slip)
                exit_cycle_id = row["cycle_id"]
                break
            if stop is not None and bid <= float(stop):
                result = "SL_REACHED"
                exit_price = bid * (1 - slip)
                exit_cycle_id = row["cycle_id"]
                break
        _ = ltp
    if result is None:
        result = "TIMEOUT"
        if later:
            last = later[-1]
            if last["bid"] is not None:
                exit_price = float(last["bid"]) * (1 - slip)
                exit_cycle_id = last["cycle_id"]
            else:
                exit_price = None
        else:
            exit_price = None

    if exit_price is None:
        return {
            "result": result, "r_multiple": None, "holding_cycles": None,
            "note": "no exit price available in stored snapshots",
        }

    gross = exit_price - entry_price
    costs = (entry_price + exit_price) * cost
    net = gross - costs
    risk = entry_price - float(stop) if stop is not None else None
    r_multiple = (net / risk) if risk and risk > 0 else None
    return {
        "result": result,
        "r_multiple": round(r_multiple, 3) if r_multiple is not None else None,
        "holding_cycles": (exit_cycle_id - cycle_id) if exit_cycle_id else None,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "net_pnl": round(net, 2),
    }


def analyze_results(results):
    """§42 metrics from real replay outcomes."""
    total = len(results)
    decisions = [r for r in results if r.get("best") is not None]
    outcomes = [r for r in decisions if r.get("outcome") and r["outcome"].get("r_multiple") is not None]
    wins = [o for o in outcomes if o["outcome"]["result"] == "TARGET_REACHED"]
    losses = [o for o in outcomes if o["outcome"]["result"] == "SL_REACHED"]
    r_values = [o["outcome"]["r_multiple"] for o in outcomes]

    gross_profit = sum(o["outcome"]["r_multiple"] for o in wins)
    gross_loss = abs(sum(o["outcome"]["r_multiple"] for o in losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for o in outcomes:
        equity += o["outcome"]["r_multiple"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "total_cycles": total,
        "decisions": len(decisions),
        "no_signal_cycles": total - len(decisions),
        "resolved_outcomes": len(outcomes),
        "target_hit_rate": round(100.0 * len(wins) / len(outcomes), 1) if outcomes else None,
        "sl_hit_rate": round(100.0 * len(losses) / len(outcomes), 1) if outcomes else None,
        "false_signal_rate": round(
            100.0 * sum(1 for o in outcomes if o["outcome"]["result"] in ("SL_REACHED", "TIMEOUT"))
            / len(outcomes), 1) if outcomes else None,
        "no_signal_rate": round(100.0 * (total - len(decisions)) / total, 1) if total else None,
        "win_rate": round(100.0 * len(wins) / len(outcomes), 1) if outcomes else None,
        "loss_rate": round(100.0 * len(losses) / len(outcomes), 1) if outcomes else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "average_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "max_drawdown_r": round(max_dd, 3) if r_values else None,
        "avg_holding_cycles": round(
            sum(o["outcome"]["holding_cycles"] for o in outcomes if o["outcome"]["holding_cycles"])
            / len(outcomes), 1) if outcomes else None,
    }


def run_backtest(limit=None):
    status = historical_data_status()
    if not is_backtest_ready(status):
        return None, status

    cycles = load_historical_cycles(limit)
    results = []
    for cycle in cycles:
        cycle_id = cycle["cycle_id"]
        snapshot_rows = load_snapshot_for_cycle(cycle_id)
        if not snapshot_rows:
            results.append({"cycle_id": cycle_id, "best": None,
                            "note": "no stored option snapshot for this cycle"})
            continue
        candidates = reconstruct_candidates(snapshot_rows)
        if not candidates:
            results.append({"cycle_id": cycle_id, "best": None,
                            "note": "no candidates passed liquidity filter in stored snapshot"})
            continue
        market_snapshot = load_market_snapshot_for_cycle(cycle_id)
        underlying_snapshot = reconstruct_underlying_snapshot(market_snapshot, cycles)
        spot = market_snapshot.get("spot") if market_snapshot else cycle.get("spot")
        if underlying_snapshot:
            underlying_snapshot["spot"] = spot
        if spot is None:
            results.append({"cycle_id": cycle_id, "best": None, "note": "no stored spot"})
            continue
        regime = cycle.get("regime") or "NEUTRAL"
        best, gate_reason, _scored = replay_cycle(cycle, candidates, underlying_snapshot, regime)
        entry = {
            "cycle_id": cycle_id,
            "timestamp": cycle.get("timestamp"),
            "regime": regime,
            "best": best,
            "gate_reason": gate_reason,
        }
        if best:
            entry["outcome"] = resolve_outcome(cycle_id, best, cycles)
        results.append(entry)
    return results, status


def main():
    parser = argparse.ArgumentParser(description="BLOCKORA Backtesting (stored historical data only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of historical cycles to replay")
    args = parser.parse_args()

    print("=" * 60)
    print("BLOCKORA BACKTESTING (STORED HISTORICAL DATA ONLY)")
    print("=" * 60)

    status = historical_data_status()
    print(f"Historical cycles stored          : {status['cycles']}")
    print(f"Stored option snapshots           : {status['option_snapshots']}")
    print(f"Cycles with stored bid/ask quotes : {status['cycles_with_bid_ask']}")

    if not is_backtest_ready(status):
        print()
        print("BACKTEST NOT READY")
        print("- Missing: historical option-chain snapshots for each past cycle.")
        print("  Real per-cycle chain data accumulates in option_snapshots only")
        print("  while the live engine runs during market hours.")
        print("- No live data was fetched and none was fabricated.")
        return 2

    results, _status = run_backtest(args.limit)
    if not results:
        print("No replayable cycles found.")
        return 2

    analysis = analyze_results(results)
    print()
    print(f"Cycles replayed           : {analysis['total_cycles']}")
    print(f"Decisions (BEST selected) : {analysis['decisions']}")
    print(f"No-signal cycles          : {analysis['no_signal_cycles']}")
    print(f"Resolved outcomes         : {analysis['resolved_outcomes']}")
    if analysis["resolved_outcomes"]:
        print(f"Target hit rate           : {analysis['target_hit_rate']}%")
        print(f"SL hit rate               : {analysis['sl_hit_rate']}%")
        print(f"Win rate                  : {analysis['win_rate']}%")
        print(f"Profit factor             : {analysis['profit_factor']}")
        print(f"Average R                 : {analysis['average_r']}")
        print(f"Max drawdown (R)          : {analysis['max_drawdown_r']}")
        print(f"Avg holding cycles        : {analysis['avg_holding_cycles']}")
    print()
    print("Costs applied: txn "
          f"{config.BACKTEST_TXN_COST_PCT}% round trip, slippage "
          f"{config.BACKTEST_SLIPPAGE_PCT}% per side (§44)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
