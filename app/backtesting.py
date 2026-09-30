import sys
import argparse
import time
import math

import pytz
import sqlite3

from app.config import config
from app.data.jugaad import jugaad_source
from app.data.cache import cache
from app.selector.candidates import CandidateGenerator
from app.market.option_chain import OptionChainEngine
from app.market.regime import MarketRegimeEngine
from app.market.underlying import UnderlyingEngine
from app.selector.scoring import ScoringEngine
from app.selector.ranking import RankingEngine
from app.risk.entry import EntryEngine
from app.risk.stoploss import StopLossEngine
from app.risk.target import TargetEngine
from app.output.terminal import print_cycle_report
from app.scheduler import market_session_state
from app.history.database import db
from app.history.outcomes import OutcomeEngine
from app.history.signals import SignalHistory

IST = pytz.timezone("Asia/Kolkata")


def _safe_float(value):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _replay_single_cycle(cycle_data):
    """Re-run one complete production cycle using data available at that
    cycle's timestamp. Returns dict with scores and decisions."""
    
    underlying_sym = cycle_data.get("underlying", "NIFTY")
    spot = _safe_float(cycle_data.get("spot"))
    regime = cycle_data.get("regime", "NEUTRAL")
    data_quality = _safe_float(cycle_data.get("data_quality"))
    
    # Skip if spot is None (data feed failed that cycle)
    if spot is None:
        return None
    
    # Initialize engines
    underlying_engine = UnderlyingEngine(underlying_sym)
    chain_engine = OptionChainEngine(underlying_sym)
    candidate_generator = CandidateGenerator(underlying_sym)
    scoring_engine = ScoringEngine()
    ranking_engine = RankingEngine()
    entry_engine = EntryEngine()
    sl_engine = StopLossEngine()
    target_engine = TargetEngine()
    
    # Update underlying with just the spot price
    if underlying_engine.price_history:
        underlying_engine.price_history = [spot]
    if underlying_engine.high_history:
        underlying_engine.high_history = [spot]
    if underlying_engine.low_history:
        underlying_engine.low_history = [spot]
    
    underlying_snapshot = underlying_engine.snapshot()
    regime = MarketRegimeEngine.classify(underlying_snapshot)
    
    # Generate candidates
    candidates = candidate_generator.generate(spot, regime)
    
    if not candidates:
        return {
            "cycle_id": cycle_data["cycle_id"],
            "timestamp": cycle_data["timestamp"],
            "spot": spot,
            "regime": regime,
            "data_quality": data_quality,
            "candidates_generated": 0,
            "best": None,
        }
    
    # Score candidates
    scored = scoring_engine.score_candidates(candidates, underlying_snapshot, regime)
    
    # Rank candidates
    best, top3 = ranking_engine.select_best(scored)
    
    best_str = f" BEST: {best.get('strike')} {best.get('option_type')} score={best.get('total_score')}" if best else ""
    
    return {
        "cycle_id": cycle_data["cycle_id"],
        "timestamp": cycle_data["timestamp"],
        "spot": spot,
        "regime": regime,
        "data_quality": data_quality,
        "candidates_generated": len(candidates),
        "best": best,
        "best_score_str": best_str,
    }


def run_full_backtest(limit=10, min_data_quality=0):
    """Full backtest: fetch option chain -> update cache -> replay historical cycles."""
    # Step 1: Fetch option chain via Jugaad (secondary data source)
    chain = jugaad_source.option_chain_index('NIFTY')
    
    if chain.get('status') != 'success' or not chain.get('raw'):
        print(f"ERROR: Failed to fetch option chain: {chain}")
        return None
    
    # Step 2: Update the in-memory market cache
    result = cache.update_option_chain('NIFTY', chain)
    # print(f"Option chain cached: {result is not None}")
    
    # Brief pause to ensure cache is set
    time.sleep(0.5)
    
    # Step 3: Load historical cycles from SQLite
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        SELECT * FROM cycles
        WHERE data_quality >= ?
        ORDER BY cycle_id ASC
    """, (min_data_quality,))
    rows = cur.fetchall()
    conn.close()
    
    # Filter cycles where spot is not None
    cycles = [dict(r) for r in rows if _safe_float(r["spot"]) is not None]
    
    if not cycles:
        print("No historical cycles with available spot data found.")
        return []
    
    # Limit the number of cycles
    if limit:
        cycles = cycles[:limit]
    
    print(f"Running backtest on {len(cycles)} historical cycles")
    
    # Step 4: Replay each cycle
    results = []
    for i, cycle_data in enumerate(cycles):
        result = _replay_single_cycle(cycle_data)
        if result is not None:
            results.append(result)
        
        # Progress indicator
        if (i + 1) % 5 == 0 or i + 1 == len(cycles):
            print(f"  Progress: {i+1}/{len(cycles)} cycles replayed")
    
    return results


def run_replay_only(limit=10, min_data_quality=0):
    """Run backtest replay only - assumes option chain cache is already populated."""
    # Load historical cycles from SQLite
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        SELECT * FROM cycles
        WHERE data_quality >= ?
        ORDER BY cycle_id ASC
    """, (min_data_quality,))
    rows = cur.fetchall()
    conn.close()
    
    # Filter cycles where spot is not None
    cycles = [dict(r) for r in rows if _safe_float(r["spot"]) is not None]
    
    if limit:
        cycles = cycles[:limit]
    
    print(f"Replaying {len(cycles)} historical cycles (cache assumed populated)")
    
    results = []
    for i, cycle_data in enumerate(cycles):
        result = _replay_single_cycle(cycle_data)
        if result is not None:
            results.append(result)
        
        if (i + 1) % 5 == 0 or i + 1 == len(cycles):
            print(f"  Progress: {i+1}/{len(cycles)} cycles replayed")
    
    return results


def analyze_results(results):
    """Analyze backtest results and produce performance metrics."""
    if not results:
        return {}
    
    total_cycles = len(results)
    cycles_with_best = sum(1 for r in results if r.get("best") is not None)
    cycles_with_candidates = sum(1 for r in results if r.get("candidates_generated", 0) > 0)
    
    # Score analysis
    scores = [r.get("best", {}).get("total_score") for r in results if r.get("best") is not None]
    
    analysis = {
        "total_cycles_analyzed": total_cycles,
        "cycles_with_best_strike": cycles_with_best,
        "cycles_with_candidates": cycles_with_candidates,
        "pct_cycles_with_best": round(100.0 * cycles_with_best / total_cycles, 1) if total_cycles > 0 else 0,
        "pct_cycles_with_candidates": round(100.0 * cycles_with_candidates / total_cycles, 1) if total_cycles > 0 else 0,
    }
    
    if scores:
        analysis["score_stats"] = {
            "mean": round(sum(scores) / len(scores), 2),
            "min": round(min(scores), 2),
            "max": round(max(scores), 2),
            "above_min_score": round(100.0 * sum(1 for s in scores if s >= config.MIN_SCORE) / len(scores), 1),
        }
    
    # Regime analysis
    regime_counts = {}
    for r in results:
        reg = r.get("regime", "UNKNOWN")
        regime_counts[reg] = regime_counts.get(reg, 0) + 1
    analysis["regime_distribution"] = regime_counts
    
    # Candidate generation analysis
    candidates_per_cycle = [r.get("candidates_generated", 0) for r in results]
    analysis["candidates_stats"] = {
        "mean": round(sum(candidates_per_cycle) / len(candidates_per_cycle), 1),
        "min": min(candidates_per_cycle),
        "max": max(candidates_per_cycle),
    }
    
    return analysis


def generate_report(results):
    """Generate a complete backtesting report."""
    analysis = analyze_results(results)
    
    print("\n" + "=" * 60)
    print("BLOCKORA BACKTESTING REPORT")
    print("=" * 60)
    
    print(f"\nCycles Analyzed: {analysis['total_cycles_analyzed']}")
    print(f"Cycles with Best Strike: {analysis['cycles_with_best_strike']}")
    print(f"Cycles with Candidates: {analysis['cycles_with_candidates']}")
    print(f"% Cycles with Best Strike: {analysis['pct_cycles_with_best']}%")
    print(f"% Cycles with Candidates: {analysis['pct_cycles_with_candidates']}%")
    
    if "score_stats" in analysis:
        ss = analysis["score_stats"]
        print(f"\nScore Statistics (0-90 scale):")
        print(f"  Mean: {ss['mean']}")
        print(f"  Min: {ss['min']}")
        print(f"  Max: {ss['max']}")
        print(f"  % Above MIN_SCORE ({config.MIN_SCORE}): {ss['above_min_score']}%")
    
    if "regime_distribution" in analysis:
        print(f"\nRegime Distribution:")
        for reg, count in sorted(analysis["regime_distribution"].items()):
            print(f"  {reg}: {count} cycles ({round(100*count/analysis['total_cycles_analyzed'],1)}%)")
    
    if "candidates_stats" in analysis:
        cs = analysis["candidates_stats"]
        print(f"\nCandidate Generation:")
        print(f"  Mean candidates per cycle: {cs['mean']}")
        print(f"  Min: {cs['min']}, Max: {cs['max']}")
    
    print("\n" + "=" * 60)
    print("END BACKTESTING REPORT")
    print("=" * 60)


def main():
    """Main entry point for backtesting."""
    parser = argparse.ArgumentParser(description="BLOCKORA Backtesting Tool")
    parser.add_argument("--limit", type=int, default=10,
                        help="Maximum number of historical cycles to analyze")
    parser.add_argument("--min-quality", type=float, default=0,
                        help="Minimum data quality score to include cycle")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip Jugaad fetch (assume cache already populated)")
    parser.add_argument("--report", action="store_true",
                        help="Generate full report after backtest")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("BLOCKORA BACKTESTING - Phase 12")
    print("=" * 60)
    print()
    
    if not args.no_fetch:
        # Full flow: fetch + cache + replay
        print("Step 1: Fetching option chain via Jugaad-Data...")
        results = run_full_backtest(limit=args.limit, min_data_quality=args.min_quality)
        
        if results is None:
            print("\nBacktest aborted due to fetch failure.")
            print("Try: --no-fetch if cache is already populated from a previous run.")
            sys.exit(1)
    else:
        # Replay only: assume cache is populated
        print("Step 1: Skipping fetch - using existing cache...")
        results = run_replay_only(limit=args.limit, min_data_quality=args.min_quality)
    
    if not results:
        print("\nNo backtest results produced. This may be because:")
        print("  - Historical cycles have spot=None (data feed failed)")
        print("  - Option chain cache is empty or invalid")
        print("  - Candidate generation found no valid strikes")
        sys.exit(0)
    
    # Step 2: Generate report
    if args.report or True:
        print()
        print("Step 2: Generating analysis report...")
        generate_report(results)
    
    print()
    print("BLOCKORA BACKTESTING PHASE COMPLETE")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
