"""Walk-forward validation over stored historical data (BLOCKORA §41, §43).

Integrity rules:

- Chronological split: the training period strictly precedes the
  out-of-sample period. Multiple non-overlapping windows advance forward.
- Every replayed decision uses ONLY data stored for that cycle (frozen
  inputs, no look-ahead).
- No parameter optimization is performed here: weights are the BLOCKORA §15
  initial architecture, and this module measures out-of-sample behaviour.
  §68 anti-overfitting rules apply: no weight changes based on one day.
- If insufficient historical data exists, this prints BACKTEST NOT READY (or
  reports too-few-cycles) rather than producing a misleading split. It never
  fetches live data and never fabricates history.

This is a CLI: python -m app.walk_forward [--train-ratio 0.6] [--windows 3]
"""

import argparse
import sys

from app.config import config
from app.history.database import db


def historical_data_status():
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


def is_walk_forward_ready(status=None):
    status = status or historical_data_status()
    return status["cycles"] > 0 and status["cycles_with_bid_ask"] > 0


def split_windows(cycle_ids, windows, train_ratio):
    """Advance chronological windows: train prefix, then out-of-sample slice."""
    results = []
    ids = sorted(cycle_ids)
    if not ids:
        return results
    train_end = max(1, int(len(ids) * train_ratio))
    if train_end >= len(ids):
        return results
    oos = ids[train_end:]
    chunk = max(1, len(oos) // max(1, windows))
    for w in range(windows):
        start = w * chunk
        end = start + chunk if w < windows - 1 else len(oos)
        if start >= len(oos):
            break
        results.append({
            "window": w + 1,
            "train_cycle_ids": ids[:train_end],
            "oos_cycle_ids": oos[start:end],
        })
    return results


def run_walk_forward(windows=3, train_ratio=0.6, limit=None):
    """Run the walk-forward replay using the same engine as backtesting."""
    from app.backtesting import (
        load_historical_cycles, load_snapshot_for_cycle, load_market_snapshot_for_cycle,
        reconstruct_candidates, reconstruct_underlying_snapshot, replay_cycle, resolve_outcome,
    )

    status = historical_data_status()
    if not is_walk_forward_ready(status):
        return None, status

    cycles = load_historical_cycles(limit)
    if not cycles:
        return None, status

    split = split_windows([c["cycle_id"] for c in cycles], windows, train_ratio)
    if not split:
        return {"windows": [], "note": "insufficient cycles for a chronological split"}, status

    by_id = {c["cycle_id"]: c for c in cycles}
    window_results = []
    for w in split:
        w_res = {"window": w["window"], "cycles": [], "outcomes": []}
        for cycle_id in w["oos_cycle_ids"]:
            cycle = by_id[cycle_id]
            rows = load_snapshot_for_cycle(cycle_id)
            if not rows:
                w_res["cycles"].append({"cycle_id": cycle_id, "decision": "NO SNAPSHOT"})
                continue
            candidates = reconstruct_candidates(rows)
            if not candidates:
                w_res["cycles"].append({"cycle_id": cycle_id, "decision": "NO CANDIDATES"})
                continue
            market_snapshot = load_market_snapshot_for_cycle(cycle_id)
            underlying_snapshot = reconstruct_underlying_snapshot(market_snapshot, cycles)
            spot = market_snapshot.get("spot") if market_snapshot else cycle.get("spot")
            if underlying_snapshot:
                underlying_snapshot["spot"] = spot
            if spot is None:
                w_res["cycles"].append({"cycle_id": cycle_id, "decision": "NO SPOT"})
                continue
            best, gate_reason, _ = replay_cycle(cycle, candidates, underlying_snapshot, cycle.get("regime") or "NEUTRAL")
            if best is None:
                w_res["cycles"].append({
                    "cycle_id": cycle_id, "decision": "NO CLEAR STRIKE", "gate_reason": gate_reason,
                })
                continue
            outcome = resolve_outcome(cycle_id, best, cycles)
            w_res["cycles"].append({
                "cycle_id": cycle_id,
                "decision": "BEST",
                "strike": f"{best.get('strike')} {best.get('option_type')}",
                "score": best.get("total_score"),
                "outcome": outcome,
            })
            if outcome and outcome.get("r_multiple") is not None:
                w_res["outcomes"].append(outcome)
        window_results.append(w_res)
    return window_results, status


def main():
    parser = argparse.ArgumentParser(description="BLOCKORA Walk-Forward Validation (stored data only)")
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--train-ratio", type=float, default=0.6)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    print("=" * 60)
    print("BLOCKORA WALK-FORWARD VALIDATION (STORED DATA ONLY)")
    print("=" * 60)

    status = historical_data_status()
    print(f"Historical cycles stored          : {status['cycles']}")
    print(f"Stored option snapshots           : {status['option_snapshots']}")
    print(f"Cycles with stored bid/ask quotes : {status['cycles_with_bid_ask']}")

    if not is_walk_forward_ready(status):
        print()
        print("BACKTEST NOT READY")
        print("- Missing: historical per-cycle option-chain snapshots with bid/ask.")
        print("  Walk-forward validation requires real accumulated history; none")
        print("  was fetched live or fabricated.")
        return 2

    window_results, _status = run_walk_forward(
        windows=args.windows, train_ratio=args.train_ratio, limit=args.limit
    )
    if not window_results:
        print("No walk-forward windows could be constructed from stored data.")
        return 2

    if isinstance(window_results, dict):
        print(window_results.get("note"))
        return 2

    print()
    for w in window_results:
        outcomes = w["outcomes"]
        r_vals = [o["r_multiple"] for o in outcomes if o.get("r_multiple") is not None]
        print(f"Window {w['window']}: {len(w['cycles'])} out-of-sample cycles, "
              f"{len(outcomes)} resolved outcomes")
        if r_vals:
            avg_r = sum(r_vals) / len(r_vals)
            print(f"  Average R: {avg_r:.3f} over {len(r_vals)} outcomes")
        else:
            print("  No executable outcomes in this window (no stored bid/ask path)")
        for c in w["cycles"]:
            line = f"  cycle {c['cycle_id']}: {c['decision']}"
            if c.get("strike"):
                line += f" {c['strike']} score={c.get('score')}"
            if c.get("outcome") and c["outcome"].get("result"):
                line += f" -> {c['outcome']['result']} (R={c['outcome'].get('r_multiple')})"
            print(line)
        print()

    print(f"Costs applied: txn {config.BACKTEST_TXN_COST_PCT}% round trip, "
          f"slippage {config.BACKTEST_SLIPPAGE_PCT}% per side (§44)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
