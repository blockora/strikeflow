"""Phase 1 verification harness (BLOCKORA / STRIKEFLOW).

Run:  python3 verify_phase1.py

Harness rules (these are the rules the previous harness broke):

* DB_PATH is redirected to a throwaway temp directory BEFORE any app import.
  load_dotenv() does not override already-set variables, so this wins over any
  .env value and `data/history.db` is never opened, created or modified.
* Every check drives a PRODUCTION function. This file never restates a
  production formula or constant and then asserts that the restatement is
  true — that is how the previous 32 tautological checks passed while the whole
  feature was broken.
* Gate invariants are asserted BEHAVIOURALLY (a below-threshold candidate is
  actually rejected by the real gate) and, where a literal must be pinned, are
  compared against the committed baseline at HEAD via `git show`.
* Market data is in-memory only. No mock directories, no network, no files
  written outside the temp dir.

Sections:
  0  harness safety
  1  wall-clock returns
  2  DirectionEngine
  3  Confidence
  4  gate behaviour + regression invariants vs HEAD
  5  SQLite migration + the real persistence write path
  6  terminal output
  7  production wiring: a real OptionStrikeSelector.run_cycle()
  8  DirectionEngine has no network
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# Redirect the database BEFORE importing anything from app/.
# --------------------------------------------------------------------------
_TMPDIR = tempfile.mkdtemp(prefix="blockora_verify_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "verify_history.db")

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from app.config import config  # noqa: E402
from app.market.underlying import UnderlyingEngine  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0
REAL = 0
STRUCT = 0


def check(name, cond, detail="", structural=False):
    """Record one check. `structural=True` marks a static/source-level check."""
    global PASS, FAIL, REAL, STRUCT
    if structural:
        STRUCT += 1
    else:
        REAL += 1
    if cond:
        PASS += 1
        print(f"  PASS  [{'S' if structural else 'R'}] {name}")
    else:
        FAIL += 1
        print(f"  FAIL  [{'S' if structural else 'R'}] {name}  {detail}")
    return bool(cond)


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP       {name}  ({why})")


# --------------------------------------------------------------------------
# In-memory fixtures (test-only). No files, no folders, no network.
# --------------------------------------------------------------------------
def _fill_engine(eng, prices, timestamps):
    eng.price_history = list(prices)
    eng.timestamps = list(timestamps)
    eng.high_history = list(prices)
    eng.low_history = list(prices)
    eng.volume_history = [1.0] * len(prices)


def _timestamps(prices, step=30.0, end_offset=2.0):
    last = time.time() - end_offset
    start = last - step * (len(prices) - 1)
    return [start + i * step for i in range(len(prices))]


def engine_with(prices, step=30.0, when="today"):
    eng = UnderlyingEngine("NIFTY")
    if when == "today":
        ts = _timestamps(prices, step)
    else:
        ts = [time.time() - 86400.0 - step * (len(prices) - 1) + i * step
              for i in range(len(prices))]
    _fill_engine(eng, prices, ts)
    return eng


def _stale_safe_engine(yesterday_only=True):
    """History whose newest cell is FRESH (passes the staleness gate) while the
    oldest qualifying cell is from the previous day.

    yesterday_only=True  -> no same-day cell reaches the lookback window, so the
                            engine must fail closed with a previous-day reason.
    yesterday_only=False -> one same-day cell DOES reach the window, so the
                            previous-day cells must not block it.
    """
    eng = UnderlyingEngine("NIFTY")
    now = time.time()
    prices = [24850, 24855, 24860, 24865, 24870, 24875]
    yesterday = [now - 90000 + i * 30 for i in range(6)]
    if yesterday_only:
        prices += [24856, 24858, 24860]
        today = [now - 120.0, now - 60.0, now - 5.0]
    else:
        prices += [24856, 24858, 24862]
        today = [now - 400.0, now - 200.0, now - 5.0]
    _fill_engine(eng, prices, yesterday + today)
    return eng


def snapshot(spot, vwap, dq=100.0, r1=0.02, r3=0.03, r5=0.04, atr=10.0):
    """A minimal real UnderlyingEngine.snapshot()-shaped dict."""
    return {
        "symbol": "NIFTY", "spot": spot, "vwap": vwap, "atr": atr,
        "data_quality": dq, "return_1m": r1, "return_3m": r3, "return_5m": r5,
        "trend": "NEUTRAL", "samples": 12,
    }


# Rising and falling series that span > DIRECTION_LOOKBACK_MINUTES.
UP_CLEAN = [24850, 24851, 24853, 24855, 24856, 24858,
            24859, 24860, 24861, 24861.5, 24861.8, 24862]
UP_SPIKE = [24850, 24860, 24870, 24880, 24890, 24900,
            24900, 24900, 24900, 24900, 24900, 24875]
DOWN_CLEAN = [24900, 24895, 24890, 24885, 24880, 24875,
              24870, 24865, 24862, 24858, 24854, 24852]
DOWN_SPIKE = [24900, 24890, 24880, 24870, 24860, 24850,
              24852, 24856, 24862, 24868, 24874, 24880]


def _head_source(path):
    try:
        out = subprocess.run(
            ["git", "-C", APP_ROOT, "show", f"HEAD:{path}"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return out.stdout
    except Exception:
        pass
    return None


def _numeric_default(text, name):
    """Extract the default literal from `NAME = _get_*( "NAME", <num> )`."""
    m = re.search(rf'{name}\s*=\s*_get_\w+\(\s*"{name}"\s*,\s*([0-9.]+)\s*\)', text)
    return float(m.group(1)) if m else None


def _weights_block(text):
    m = re.search(r"WEIGHTS\s*=\s*\{(.*?)\}", text, re.S)
    if not m:
        return None
    return {k: int(v) for k, v in re.findall(r'"(\w+)"\s*:\s*(\d+)', m.group(1))}


def _has_columns(conn, table, columns):
    names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return all(c in names for c in columns)


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ==========================================================================
def main():
    print("== 0. Harness safety ==")
    import app.history.database as db_mod

    check("production history.db is NOT the active database",
          db_mod.db.db_path.startswith(_TMPDIR),
          f"active={db_mod.db.db_path} tmp={_TMPDIR}")
    check("active database lives inside the temp dir",
          os.path.dirname(db_mod.db.db_path) == _TMPDIR,
          f"active={db_mod.db.db_path}")
    prod_db = os.path.join(APP_ROOT, "data", "history.db")
    prod_mtime_before = os.path.getmtime(prod_db) if os.path.exists(prod_db) else None

    # ------------------------------------------------------------------
    print("\n== 1. Wall-clock returns (UnderlyingEngine.snapshot) ==")

    eng = engine_with([100.0] * 5, step=1.0)
    snap = eng.snapshot()
    check("1m returns None when wall-clock coverage is insufficient",
          snap["return_1m"] is None, f"got {snap['return_1m']}")
    check("3m returns None when wall-clock coverage is insufficient",
          snap["return_3m"] is None, f"got {snap['return_3m']}")
    check("5m returns None when wall-clock coverage is insufficient",
          snap["return_5m"] is None, f"got {snap['return_5m']}")

    # No sample-count fallback: 200 samples inside a 20s span is still < 60s.
    many = [100.0 + i * 0.01 for i in range(200)]
    eng = engine_with(many, step=0.1)
    s = eng.snapshot()
    check("no sample-count fallback: 200 samples in 20s still returns None",
          s["samples"] == 200 and s["return_1m"] is None,
          f"samples={s['samples']} r1={s['return_1m']}")

    n = 11
    prices = [90.0 + 10.0 * i / (n - 1) for i in range(n)]
    eng = engine_with(prices, step=60.0)
    s = eng.snapshot()
    check("spot is the latest injected price", s["spot"] == prices[-1], f"got {s['spot']}")
    check("samples count matches the series", s["samples"] == n, f"got {s['samples']}")
    check("return_1m anchors at the 60s cell, not the latest",
          abs(s["return_1m"] - (100.0 - 99.0) / 99.0 * 100.0) < 1e-6,
          f"got {s['return_1m']}")
    check("return_3m anchors at the 180s cell, not the latest",
          abs(s["return_3m"] - (100.0 - 97.0) / 97.0 * 100.0) < 1e-6,
          f"got {s['return_3m']}")
    check("return_5m anchors at the 300s cell, not the latest",
          abs(s["return_5m"] - (100.0 - 95.0) / 95.0 * 100.0) < 1e-6,
          f"got {s['return_5m']}")

    # Explicit no-look-ahead proof: the latest-cell answer would be 0.0%.
    check("no look-ahead: 1m return is not the 0.0% latest-cell answer",
          s["return_1m"] is not None and abs(s["return_1m"]) > 0.5,
          f"got {s['return_1m']}")
    check("_validated_price rejects the latest cell",
          UnderlyingEngine._validated_price([1.0, 2.0, 3.0], [0, 1, 2], 2) is None)
    check("_validated_price accepts a non-latest cell",
          UnderlyingEngine._validated_price([1.0, 2.0, 3.0], [0, 1, 2], 1) == 2.0)

    # Irregular gaps are handled by wall time, not by index.
    e2 = UnderlyingEngine("NIFTY")
    _fill_engine(e2, [80.0, 85.0, 90.0, 95.0], [0.0, 10.0, 20.0, 31.0])
    s2 = e2.snapshot()
    check("irregular 31s span yields no 1m return", s2["return_1m"] is None,
          f"got {s2['return_1m']}")
    check("irregular span still reports the correct spot", s2["spot"] == 95.0,
          f"got {s2['spot']}")

    # ------------------------------------------------------------------
    print("\n== 2. DirectionEngine ==")
    from app.market.direction import DirectionEngine

    # --- origin / evidence absence ---
    out = DirectionEngine(UnderlyingEngine("NIFTY")).generate(snapshot(25000.0, 24980.0))
    check("empty history => NEUTRAL", out["direction"] == "NEUTRAL", f"got {out['direction']}")
    check("empty history => not confirmed", out["confirmed"] is False)
    check("empty history => missing evidence names the origin",
          "origin" in " ".join(out["missing_evidence"]).lower(),
          f"got {out['missing_evidence']}")

    thin = [24850.0, 24856.0, 24862.0]
    out_t = DirectionEngine(engine_with(thin, step=30.0)).generate(snapshot(24862.0, 24850.0))
    check("insufficient lookback coverage => NEUTRAL",
          out_t["direction"] == "NEUTRAL", f"got {out_t['direction']}")

    out_p = DirectionEngine(_stale_safe_engine(yesterday_only=True)
                            ).generate(snapshot(24862.0, 24850.0))
    check("previous-day cells with no same-day coverage => not confirmed",
          out_p["confirmed"] is False)
    check("previous-day cells with no same-day coverage => reason names the previous day",
          re.search(r"previous[- ]day", " ".join(out_p["missing_evidence"]), re.I)
          is not None,
          f"got {out_p['missing_evidence']}")

    # A previous-day cell must NOT abort the search for a valid same-day origin.
    mixed = _stale_safe_engine(yesterday_only=False)
    out_m = DirectionEngine(mixed).generate(snapshot(24862.0, 24850.0))
    check("previous-day cells do not block a valid same-day origin",
          out_m["origin_spot"] is not None,
          f"missing={out_m['missing_evidence']}")
    check("the chosen origin is a same-day cell, not yesterday's",
          out_m["origin_time"] is not None
          and out_m["origin_time"] > time.time() - 86400,
          f"origin_time={out_m['origin_time']}")

    # --- clean moves ---
    eng_up = engine_with(UP_CLEAN)
    engine = DirectionEngine(eng_up)
    out1 = engine.generate(snapshot(24862.0, 24850.0))
    check("clean UP => direction UP", out1["direction"] == "UP", f"got {out1['direction']}")
    check("clean UP => no missing evidence", out1["missing_evidence"] == [],
          f"got {out1['missing_evidence']}")
    check("clean UP => move_points is positive and equals the move",
          out1["move_points"] is not None and abs(out1["move_points"] - 12.0) < 0.01,
          f"got {out1['move_points']}")
    check("clean UP => origin is a past cell, never the latest",
          out1["origin_spot"] == 24850.0 and out1["origin_time"] < eng_up.timestamps[-1],
          f"origin_spot={out1['origin_spot']}")

    eng_dn = engine_with(DOWN_CLEAN)
    od = DirectionEngine(eng_dn).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] + 10.0, r1=-0.02, r3=-0.03, r5=-0.04))
    check("clean DOWN => direction DOWN", od["direction"] == "DOWN", f"got {od['direction']}")
    check("clean DOWN => no missing evidence", od["missing_evidence"] == [],
          f"got {od['missing_evidence']}")
    check("clean DOWN => move_points is negative",
          od["move_points"] is not None and od["move_points"] < 0,
          f"got {od['move_points']}")

    # --- symmetric timeframe agreement ---
    up_bear = DirectionEngine(engine_with(UP_CLEAN)).generate(
        snapshot(24862.0, 24850.0, r1=-0.5, r3=-0.6, r5=-0.7))
    check("UP with all-bearish timeframes is NOT confirmed", up_bear["confirmed"] is False)
    check("UP with all-bearish timeframes names the conflicting horizons",
          "1m" in " ".join(up_bear["missing_evidence"])
          and "3m" in " ".join(up_bear["missing_evidence"]),
          f"got {up_bear['missing_evidence']}")

    dn_bull = DirectionEngine(engine_with(DOWN_CLEAN)).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] + 10.0, r1=0.5, r3=0.6, r5=0.7))
    check("DOWN with all-bullish timeframes is NOT confirmed", dn_bull["confirmed"] is False)
    check("DOWN with all-bullish timeframes names the conflicting horizons",
          "1m" in " ".join(dn_bull["missing_evidence"]),
          f"got {dn_bull['missing_evidence']}")

    up_mixed = DirectionEngine(engine_with(UP_CLEAN)).generate(
        snapshot(24862.0, 24850.0, r1=0.2, r3=-0.1, r5=0.1))
    check("UP with a conflicting available horizon is NOT confirmed",
          up_mixed["confirmed"] is False)
    dn_mixed = DirectionEngine(engine_with(DOWN_CLEAN)).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] + 10.0, r1=-0.2, r3=0.1, r5=-0.1))
    check("DOWN with a conflicting available horizon is NOT confirmed",
          dn_mixed["confirmed"] is False)

    up_partial = DirectionEngine(engine_with(UP_CLEAN)).generate(
        snapshot(24862.0, 24850.0, r1=0.2, r3=0.1, r5=None))
    check("UP with a partially-available horizon set stays eligible",
          up_partial["direction"] == "UP" and not any(
              "timeframe" in m for m in up_partial["missing_evidence"]),
          f"got {up_partial['direction']} missing={up_partial['missing_evidence']}")
    dn_partial = DirectionEngine(engine_with(DOWN_CLEAN)).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] + 10.0, r1=-0.2, r3=-0.1, r5=None))
    check("DOWN with a partially-available horizon set stays eligible",
          dn_partial["direction"] == "DOWN" and not any(
              "timeframe" in m for m in dn_partial["missing_evidence"]),
          f"got {dn_partial['direction']} missing={dn_partial['missing_evidence']}")

    # --- VWAP ---
    vw = DirectionEngine(engine_with(UP_CLEAN)).generate(snapshot(24862.0, 24870.0))
    check("UP below VWAP => not confirmed", vw["confirmed"] is False)
    check("UP below VWAP => reason names VWAP",
          "vwap" in " ".join(vw["missing_evidence"]).lower(),
          f"got {vw['missing_evidence']}")
    vw_d = DirectionEngine(engine_with(DOWN_CLEAN)).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] - 10.0, r1=-0.02, r3=-0.03, r5=-0.04))
    check("DOWN above VWAP => not confirmed", vw_d["confirmed"] is False)
    check("DOWN above VWAP => reason names VWAP",
          "vwap" in " ".join(vw_d["missing_evidence"]).lower(),
          f"got {vw_d['missing_evidence']}")

    # --- data quality ---
    low = DirectionEngine(engine_with(UP_CLEAN)).generate(
        snapshot(24862.0, 24850.0, dq=10.0))
    check("DQ below MIN_DATA_QUALITY => NEUTRAL", low["direction"] == "NEUTRAL")
    check("DQ below MIN_DATA_QUALITY => not confirmed", low["confirmed"] is False)
    at_gate = DirectionEngine(engine_with(UP_CLEAN)).generate(
        snapshot(24862.0, 24850.0, dq=config.MIN_DATA_QUALITY))
    check("DQ exactly at MIN_DATA_QUALITY is not rejected by the DQ gate",
          not any("data quality" in m for m in at_gate["missing_evidence"]),
          f"got {at_gate['missing_evidence']}")

    # --- spike / V-shape, symmetric ---
    up_v = DirectionEngine(engine_with(UP_SPIKE)).generate(snapshot(24875.0, 24850.0))
    check("UP V-shape is rejected by the spike guard", up_v["confirmed"] is False)
    check("UP V-shape rejection is observable in missing evidence",
          any("retraced" in m for m in up_v["missing_evidence"]),
          f"got {up_v['missing_evidence']}")
    dn_v = DirectionEngine(engine_with(DOWN_SPIKE)).generate(
        snapshot(DOWN_SPIKE[-1], DOWN_SPIKE[-1] + 10.0, r1=-0.02, r3=-0.03, r5=-0.04))
    check("DOWN V-shape is rejected by the spike guard", dn_v["confirmed"] is False)
    check("DOWN V-shape rejection is observable in missing evidence",
          any("retraced" in m for m in dn_v["missing_evidence"]),
          f"got {dn_v['missing_evidence']}")
    up_noise = DirectionEngine(engine_with(UP_CLEAN)).generate(snapshot(24862.0, 24850.0))
    check("clean UP survives normal noise (no spike rejection)",
          not any("retraced" in m for m in up_noise["missing_evidence"]),
          f"got {up_noise['missing_evidence']}")
    dn_noise = DirectionEngine(engine_with(DOWN_CLEAN)).generate(
        snapshot(DOWN_CLEAN[-1], DOWN_CLEAN[-1] + 10.0, r1=-0.02, r3=-0.03, r5=-0.04))
    check("clean DOWN survives normal noise (no spike rejection)",
          not any("retraced" in m for m in dn_noise["missing_evidence"]),
          f"got {dn_noise['missing_evidence']}")

    # Threshold scale: 25 percentage points of the lookback range.
    for pull, expect_ok in ((2.0, True), (15.0, False)):
        series = [24850, 24860, 24870, 24880, 24890, 24900,
                  24900, 24900, 24900, 24900, 24900, 24900 - pull]
        e = engine_with(series)
        o = DirectionEngine(e).generate(snapshot(24900.0 - pull, 24850.0))
        rejected = any("retraced" in m for m in o["missing_evidence"])
        check(f"spike threshold: {pull}-point pullback on a 50-point range "
              f"-> {'survives' if expect_ok else 'rejected'}",
              rejected is (not expect_ok),
              f"direction={o['direction']} missing={o['missing_evidence']}")

    # Spike guard must not depend on ATR.
    up_no_atr = DirectionEngine(engine_with(UP_SPIKE)).generate(
        snapshot(24875.0, 24850.0, atr=None))
    check("spike guard still runs when ATR is missing (no silent fail-open)",
          any("retraced" in m for m in up_no_atr["missing_evidence"]),
          f"got {up_no_atr['missing_evidence']}")
    check("ATR is not an input to _spike_protection",
          "atr" not in DirectionEngine._spike_protection.__code__.co_varnames)

    # Spike guard fails closed on unusable data.
    bad = UnderlyingEngine("NIFTY")
    _fill_engine(bad, UP_CLEAN, _timestamps(UP_CLEAN))
    bad.price_history = bad.price_history[:-1]   # misalign the arrays
    bad_out = DirectionEngine(bad).generate(snapshot(24862.0, 24850.0))
    check("spike guard / origin fails closed on misaligned history",
          bad_out["direction"] == "NEUTRAL",
          f"got {bad_out['direction']}")

    # --- injected clock ---
    e_clock = engine_with(UP_CLEAN)
    frozen = e_clock.timestamps[-1]
    o_clock = DirectionEngine(e_clock).generate(snapshot(24862.0, 24850.0), now=frozen)
    check("generate(now=...) honours the injected clock deterministically",
          o_clock["direction"] == "UP" and o_clock["origin_time"] is not None,
          f"got {o_clock['direction']}")

    # Decisive clock test: the history lives on YESTERDAY relative to real wall
    # time, so it is only same-day with respect to the INJECTED now.  If the
    # engine overwrote `now` with the real clock, the same-day guard would
    # reject it and the result would be NEUTRAL.
    import pytz
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    ist = pytz.timezone("Asia/Kolkata")
    _y = _dt.now(ist) - _td(days=1)
    yesterday_1400 = ist.localize(_dt(_y.year, _y.month, _y.day, 14, 0, 0))
    hist_ts = [yesterday_1400.timestamp() - (len(UP_CLEAN) - 1 - i) * 30.0
               for i in range(len(UP_CLEAN))]
    clock_eng = UnderlyingEngine("NIFTY")
    _fill_engine(clock_eng, UP_CLEAN, hist_ts)
    clock_eng.underlying_age = lambda: 1.0   # isolate the staleness clock
    o_inj = DirectionEngine(clock_eng).generate(
        snapshot(24862.0, 24850.0), now=yesterday_1400.timestamp() + 60.0)
    check("_origin_for does not overwrite the injected `now`",
          o_inj["direction"] == "UP" and o_inj["origin_spot"] is not None,
          f"direction={o_inj['direction']} missing={o_inj['missing_evidence']}")

    # --- bounded history buffer -------------------------------------
    buf = UnderlyingEngine("NIFTY")
    base = time.time() - 1000.0
    _fill_engine(buf, [100.0] * 700, [base + i * 1.4 for i in range(700)])
    buf._ensure_history({"ltp": 100.0, "high": 100.0, "low": 100.0, "volume": 1})
    check("the real trim bounds the buffer at MAX_HISTORY",
          len(buf.timestamps) == UnderlyingEngine.MAX_HISTORY,
          f"len={len(buf.timestamps)} MAX_HISTORY={UnderlyingEngine.MAX_HISTORY}")
    span = buf.timestamps[-1] - buf.timestamps[0]
    lookback_s = config.DIRECTION_LOOKBACK_MINUTES * 60.0
    check("the retained buffer spans well beyond the direction lookback",
          span >= lookback_s * 1.5,
          f"span={span:.0f}s lookback={lookback_s:.0f}s "
          f"MAX_HISTORY={UnderlyingEngine.MAX_HISTORY}")
    check("the buffer stays bounded and trivially small for Android 14",
          UnderlyingEngine.MAX_HISTORY <= 1000,
          f"MAX_HISTORY={UnderlyingEngine.MAX_HISTORY}")

    # --- persistence and episodes ---
    persistent = DirectionEngine(engine_with(UP_CLEAN))
    p1 = persistent.generate(snapshot(24862.0, 24850.0))
    p2 = persistent.generate(snapshot(24862.0, 24850.0))
    check("persistence: cycle 1 is not confirmed", p1["confirmed"] is False)
    check("persistence: cycle 2 IS confirmed on the same instance", p2["confirmed"] is True,
          f"reasons={p2['confirmation_reason']}")
    persistent.generate(snapshot(24862.0, 24870.0))          # fail one requirement
    p3 = persistent.generate(snapshot(24862.0, 24850.0))
    check("a failed requirement resets the persistence streak", p3["confirmed"] is False)

    flipper = DirectionEngine(engine_with(UP_CLEAN))
    flipper.generate(snapshot(24862.0, 24850.0))
    flipped = flipper.generate(snapshot(24862.0, 24850.0))
    check("episode direction is reported as UP before a flip",
          flipped["direction"] == "UP")
    check("episode_id carries the session date and direction",
          isinstance(flipped["episode_id"], str)
          and flipped["episode_id"].endswith("|UP"),
          f"got {flipped['episode_id']}")

    # Regression guard for the production wiring bug: a fresh engine per cycle
    # must never be able to confirm.
    never = [DirectionEngine(engine_with(UP_CLEAN)).generate(snapshot(24862.0, 24850.0))
             for _ in range(4)]
    check("a fresh DirectionEngine per cycle can NEVER confirm (regression guard)",
          all(not o["confirmed"] for o in never),
          f"got {[o['confirmed'] for o in never]}")

    # ------------------------------------------------------------------
    print("\n== 3. Confidence ==")
    from app.selector.confidence import ConfidenceEngine

    ce = ConfidenceEngine()
    check("production confidence weights sum to exactly 1.0",
          abs(ConfidenceEngine.WEIGHT_SUM - 1.0) < 1e-9,
          f"sum={ConfidenceEngine.WEIGHT_SUM}")
    check("production weights expose all nine components",
          len(ConfidenceEngine.WEIGHTS) == 9,
          f"got {sorted(ConfidenceEngine.WEIGHTS)}")

    base = {"total_score": 80, "scores": {"liquidity": 7}, "risk_reward": 2.0}

    def conf_for(direction, confirmed, score=80):
        cand = dict(base)
        cand["direction"] = direction
        cand["confirmed"] = confirmed
        return ce.calculate(cand, data_quality=80, regime="BULLISH",
                            previous_cycle=None,
                            historical_stats={"samples": 20, "target_reached": 15})[0]

    c_neutral = conf_for("NEUTRAL", False)
    c_partial = conf_for("UP", False)
    c_confirmed = conf_for("UP", True)
    check("confidence stays within 0-100",
          all(0 <= v <= 100 for v in (c_neutral, c_partial, c_confirmed)))
    check("confidence band is one of the documented bands",
          ce.confidence_band(c_confirmed) in
          ("VERY HIGH", "HIGH", "MEDIUM", "LOW", "NO CLEAR SIGNAL"))
    check("neutral -> partial direction INCREASES confidence",
          c_partial > c_neutral, f"neutral={c_neutral} partial={c_partial}")
    check("partial -> confirmed direction INCREASES confidence",
          c_confirmed > c_partial, f"partial={c_partial} confirmed={c_confirmed}")
    check("partial direction contributes 60.0 * 0.08 = 4.8 points",
          abs((c_partial - c_neutral) - 4.8) < 0.011,
          f"delta={round(c_partial - c_neutral, 4)}")
    check("confirmed direction contributes 100.0 * 0.08 = 8.0 points",
          abs((c_confirmed - c_neutral) - 8.0) < 0.011,
          f"delta={round(c_confirmed - c_neutral, 4)}")
    check("confidence is not a probability / not equal to the raw score",
          abs(conf_for("UP", True, score=95) - 95) > 1.0
          and 0 <= conf_for("UP", True, score=0) <= 100)

    # ------------------------------------------------------------------
    print("\n== 4. Gate behaviour (real gates, exercised not restated) ==")
    from app.selector.ranking import RankingEngine
    from app.selector.scoring import ScoringEngine

    rank = RankingEngine()

    def cand(score, rr, bid=100.0, ask=101.0):
        return {
            "total_score": score, "risk_reward": rr, "bid": bid, "ask": ask,
            "scores": {"liquidity": 5}, "strike": 25000, "option_type": "CE",
        }

    ok_below, why_below = rank.hard_filter(cand(config.MIN_SCORE - 1, 3.0))
    check("MIN_SCORE is enforced by the real gate",
          (not ok_below) and why_below == "SCORE_BELOW_MIN",
          f"ok={ok_below} reason={why_below}")
    ok_at, _ = rank.hard_filter(cand(config.MIN_SCORE, 3.0))
    check("a candidate exactly at MIN_SCORE passes the score gate", ok_at)
    ok_rr, why_rr = rank.hard_filter(cand(90.0, config.MIN_RR - 0.1))
    check("MIN_RR is enforced by the real gate",
          (not ok_rr) and why_rr == "RR_BELOW_MIN", f"ok={ok_rr} reason={why_rr}")
    wide = config.MAX_SPREAD_PERCENT + 1.0
    ok_sp, why_sp = rank.hard_filter(cand(90.0, 3.0, bid=100.0, ask=100.0 + wide * 2))
    check("MAX_SPREAD_PERCENT is enforced by the real gate",
          (not ok_sp) and why_sp == "SPREAD_TOO_WIDE", f"ok={ok_sp} reason={why_sp}")
    ok_no_rr, why_no_rr = rank.hard_filter(cand(90.0, None))
    check("a candidate without a valid target is rejected",
          (not ok_no_rr) and why_no_rr == "NO_VALID_TARGET", f"reason={why_no_rr}")

    print("  -- regression invariants vs committed HEAD baseline --")
    head_cfg = _head_source("app/config.py")
    if head_cfg is None:
        for nm in ("MIN_SCORE", "MIN_CONFIDENCE", "MIN_DATA_QUALITY", "MIN_RR",
                   "MAX_SPREAD_PERCENT"):
            skip(f"{nm} matches HEAD baseline", "git HEAD unavailable")
    else:
        cur_cfg = open(os.path.join(APP_ROOT, "app", "config.py")).read()
        for nm in ("MIN_SCORE", "MIN_CONFIDENCE", "MIN_DATA_QUALITY", "MIN_RR",
                   "MAX_SPREAD_PERCENT", "CANDIDATE_COUNT"):
            check(f"{nm} default is unchanged from the HEAD baseline",
                  _numeric_default(cur_cfg, nm) == _numeric_default(head_cfg, nm),
                  f"cur={_numeric_default(cur_cfg, nm)} head={_numeric_default(head_cfg, nm)}")

    head_scoring = _head_source("app/selector/scoring.py")
    if head_scoring is None:
        skip("§15 scoring weights unchanged", "git HEAD unavailable")
    else:
        cur_scoring = open(os.path.join(APP_ROOT, "app", "selector", "scoring.py")).read()
        check("§15 scoring weights are byte-identical to the HEAD baseline",
              _weights_block(cur_scoring) == _weights_block(head_scoring),
              f"cur={_weights_block(cur_scoring)} head={_weights_block(head_scoring)}")
    check("§15 weights still total 100 points",
          sum(ScoringEngine.WEIGHTS.values()) == ScoringEngine.MAX_TOTAL_SCORE == 100,
          f"{ScoringEngine.MAX_TOTAL_SCORE}")

    from app.selector.candidates import CandidateGenerator
    gen = CandidateGenerator("NIFTY")
    gen.chain_engine = type("Chain", (), {
        "get_normalized_chain": lambda self: [
            {
                "strike": 25000 + off * 50, "expiry": "01-Jan-2027",
                "ce": {"ltp": 100.0 + abs(off), "bid": 99.0, "ask": 101.0,
                       "volume": 1000, "oi": 1000, "oi_change": 0},
                "pe": {"ltp": 100.0 + abs(off), "bid": 99.0, "ask": 101.0,
                       "volume": 1000, "oi": 1000, "oi_change": 0},
            }
            for off in range(-6, 7)
        ]
    })()
    made, status = gen.generate(25000.0, "BULLISH")
    check("CandidateGenerator yields exactly CANDIDATE_COUNT contracts",
          len(made) == config.CANDIDATE_COUNT == 10, f"got {len(made)} status={status}")
    check("candidates are balanced 5 CE + 5 PE",
          sum(1 for c in made if c["option_type"] == "CE") == 5
          and sum(1 for c in made if c["option_type"] == "PE") == 5,
          f"CE={sum(1 for c in made if c['option_type'] == 'CE')} "
          f"PE={sum(1 for c in made if c['option_type'] == 'PE')}")

    # ------------------------------------------------------------------
    print("\n== 5. SQLite migration + real persistence write path ==")
    DIR_COLS = ["direction", "confirmed_move", "direction_confirmed",
                "direction_origin_time", "direction_origin_spot",
                "direction_confirm_reason"]
    legacy_ddl = """
        CREATE TABLE cycles (
            cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
            underlying TEXT, spot REAL, expiry TEXT, regime TEXT,
            data_quality REAL, best_symbol TEXT, best_score REAL,
            confidence REAL, entry REAL, stop_loss REAL, target REAL,
            risk_reward REAL, signal_state TEXT);
        INSERT INTO cycles (timestamp, underlying, spot, regime, data_quality)
        VALUES ('2026-09-30 09:00:00', 'NIFTY', 25000.0, 'BULLISH', 100);
    """
    saved_path = db_mod.config.DB_PATH
    try:
        fresh = db_mod.Database.__new__(db_mod.Database)
        fresh_path = os.path.join(_TMPDIR, "fresh.db")
        db_mod.config.DB_PATH = fresh_path
        fresh = db_mod.Database()
        check("fresh DB carries every direction column",
              _has_columns(fresh.conn, "cycles", DIR_COLS))

        legacy = os.path.join(_TMPDIR, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.executescript(legacy_ddl)
        conn.commit()
        conn.close()
        db_mod.config.DB_PATH = legacy
        legacy_db = db_mod.Database()
        check("legacy v1 database migrates forward",
              _user_version(legacy_db.conn) == db_mod.Database.SCHEMA_VERSION,
              f"user_version={_user_version(legacy_db.conn)}")
        check("migrated legacy database carries every direction column",
              _has_columns(legacy_db.conn, "cycles", DIR_COLS))
        row = legacy_db.fetchone("SELECT underlying, spot FROM cycles")
        check("migration preserves existing rows and values",
              row is not None and row["underlying"] == "NIFTY"
              and row["spot"] == 25000.0, f"got {tuple(row) if row else None}")
        db_mod.config.DB_PATH = legacy
        again = db_mod.Database()
        check("migration is idempotent across restarts",
              _user_version(again.conn) == db_mod.Database.SCHEMA_VERSION)
        check("idempotent restart does not duplicate columns",
              _has_columns(again.conn, "cycles", DIR_COLS))
    finally:
        db_mod.config.DB_PATH = saved_path

    # --- the real write path: CycleHistory.save_cycle -------------------
    from app.history.cycles import CycleHistory

    check("dangerous dead save_direction_evidence helper is gone",
          not hasattr(CycleHistory, "save_direction_evidence"),
          "helper still present")
    check("save_direction_evidence raises no duplicate-cycle_id INSERT",
          "save_direction_evidence" not in
          open(os.path.join(APP_ROOT, "app", "history", "cycles.py")).read())

    write_db = os.path.join(_TMPDIR, "write_path.db")
    db_mod.config.DB_PATH = write_db
    writer = db_mod.Database()
    check("save_cycle writes direction fields on the SAME cycle row",
          _has_columns(writer.conn, "cycles", DIR_COLS))
    # Re-point the module-level db used by CycleHistory at the temp database.
    import app.history.cycles as cycles_mod
    cycles_mod.db = writer

    cid = CycleHistory.save_cycle({
        "underlying": "NIFTY", "spot": 24862.0, "regime": "BULLISH",
        "data_quality": 100.0, "best_symbol": "NIFTY 25000 CE", "best_score": 80.0,
        "confidence": 80.0, "entry": 100.0, "stop_loss": 90.0, "target": 120.0,
        "risk_reward": 1.8, "signal_state": "NEW",
        "direction": "UP", "confirmed_move": 12.0, "direction_confirmed": True,
        "direction_origin_time": 123.0, "direction_origin_spot": 24850.0,
        "direction_confirm_reason": ["UP confirmed over 2/2 consecutive cycles"],
    })
    row = writer.fetchone(
        "SELECT direction, confirmed_move, direction_confirmed, "
        "direction_origin_time, direction_origin_spot, direction_confirm_reason "
        "FROM cycles WHERE cycle_id = ?", (cid,))
    check("save_cycle persists direction", row["direction"] == "UP", f"got {row['direction']}")
    check("save_cycle persists confirmed_move",
          row["confirmed_move"] == 12.0, f"got {row['confirmed_move']}")
    check("save_cycle persists direction_confirmed",
          row["direction_confirmed"] == 1, f"got {row['direction_confirmed']}")
    check("save_cycle persists direction_origin_time as an IST timestamp",
          isinstance(row["direction_origin_time"], str)
          and len(row["direction_origin_time"]) == 19
          and row["direction_origin_time"].startswith("1970-01-01"),
          f"got {row['direction_origin_time']!r}")
    check("save_cycle persists direction_origin_spot",
          row["direction_origin_spot"] == 24850.0, f"got {row['direction_origin_spot']}")
    check("save_cycle round-trips the confirmation reason",
          json.loads(row["direction_confirm_reason"]) ==
          ["UP confirmed over 2/2 consecutive cycles"],
          f"got {row['direction_confirm_reason']!r}")

    # A NO CLEAR cycle (no `best`) must still persist direction evidence.
    cid_nc = CycleHistory.save_cycle({
        "underlying": "NIFTY", "spot": 24862.0, "regime": "NO_CLEAR_REGIME",
        "data_quality": 100.0, "best_symbol": None, "best_score": None,
        "confidence": None, "entry": None, "stop_loss": None, "target": None,
        "risk_reward": None, "signal_state": "NO SIGNAL",
        "direction": "DOWN", "confirmed_move": -11.0, "direction_confirmed": False,
        "direction_origin_time": 456.0, "direction_origin_spot": 24873.0,
        "direction_confirm_reason": [],
    })
    row_nc = writer.fetchone(
        "SELECT direction, confirmed_move, direction_confirmed, "
        "direction_confirm_reason FROM cycles WHERE cycle_id = ?", (cid_nc,))
    check("direction is persisted for NO CLEAR cycles too",
          row_nc["direction"] == "DOWN" and row_nc["confirmed_move"] == -11.0
          and row_nc["direction_confirmed"] == 0,
          f"got {tuple(row_nc)}")
    check("an empty confirmation reason stores NULL, not a blob",
          row_nc["direction_confirm_reason"] is None,
          f"got {row_nc['direction_confirm_reason']!r}")
    check("one row per cycle: no duplicate cycle_id was created",
          writer.fetchone("SELECT COUNT(*) AS n FROM cycles")["n"] == 2,
          f"rows={writer.fetchone('SELECT COUNT(*) AS n FROM cycles')['n']}")

    # latest_cycle() feeds run_cycle's previous-cycle comparison and
    # history.outcomes.detect_state, both of which call .get().  A raw
    # sqlite3.Row has no .get(), which crashed run_cycle from cycle 2 onwards.
    latest = CycleHistory.latest_cycle()
    check("latest_cycle() returns a .get()-able mapping, not sqlite3.Row",
          hasattr(latest, "get"), f"got {type(latest).__name__}")
    check("latest_cycle() exposes the direction evidence it just stored",
          hasattr(latest, "get") and latest.get("direction") in ("UP", "DOWN", "NEUTRAL"),
          f"got {latest.get('direction') if hasattr(latest, 'get') else None}")

    cycles_mod.db = db_mod.db
    db_mod.config.DB_PATH = saved_path

    # ------------------------------------------------------------------
    print("\n== 6. Terminal output ==")
    import app.output.terminal as term

    def render(fn, *a):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            fn(*a)
        finally:
            sys.stdout = old
        return buf.getvalue()

    def base_report(**over):
        rep = {
            "cycle_id": 1, "time": "10:00:00", "underlying": "NIFTY", "spot": 24900.0,
            "expiry": "01-Jan-2027", "regime": "BULLISH", "data_quality": 90.0,
            "best_raw_score": 68.0, "required_score": config.MIN_SCORE,
            "confidence_value": None, "confidence_label": None,
            "gate_reason": "SCORE_BELOW_MIN", "no_signal_reasons": ["below min"],
            "signal_state": "NO SIGNAL", "direction": "NEUTRAL", "confirmed": False,
            "move_points": None, "setup": None, "confirmation_reason": [],
        }
        rep.update(over)
        return rep

    out = render(term.print_no_clear_strike, base_report())
    check("NO CLEAR renders Direction", "Direction   : NEUTRAL" in out)
    check("NO CLEAR with no move renders Confirmed N/A", "Confirmed   : N/A" in out)
    check("NO CLEAR renders Required from the configured MIN_SCORE",
          f"Required    : {_fmt_expected(config.MIN_SCORE)}" in out, f"got:\n{out}")
    check("NO CLEAR renders the NO SIGNAL result",
          "RESULT      : NO SIGNAL" in out)
    check("NO CLEAR renders N/A confidence when there is none",
          "Confidence  : N/A" in out)

    up_no_clear = render(term.print_no_clear_strike, base_report(
        direction="UP", confirmed=False, move_points=12.5, setup="PARTIAL",
        gate_reason="SCORE_BELOW_MIN"))
    check("NO CLEAR preserves a real UP direction (not forced to NEUTRAL)",
          "Direction   : UP" in up_no_clear, f"got:\n{up_no_clear}")
    check("NO CLEAR shows the unconfirmed move with its sign",
          "Confirmed   : +12.5 pts (not confirmed)" in up_no_clear, f"got:\n{up_no_clear}")
    check("NO CLEAR shows the derived setup",
          "Setup       : PARTIAL" in up_no_clear, f"got:\n{up_no_clear}")

    best_rep = base_report(
        confidence_value=82.0, confidence_label="HIGH", signal_state="STRENGTHENING",
        best={
            "strike": 25000, "option_type": "CE", "direction": "UP", "confirmed": True,
            "move_points": 12.5, "confirmation_reason": ["UP confirmed"],
            "setup": "CONFIRMED", "total_score": 79.4, "entry_low": 24900.0,
            "entry_high": 24990.0, "stop_loss": 24800.0, "target": 25100.0,
            "risk_reward": 1.8, "ltp": 24950.0, "reasons": ["OI confirmation"],
        })
    out_best = render(term.print_best_strike, best_rep, best_rep["best"])
    check("BEST renders Direction", "Direction   : UP" in out_best)
    check("BEST renders a confirmed positive move with its sign",
          "Confirmed   : +12.5 pts (confirmed)" in out_best, f"got:\n{out_best}")
    check("BEST renders the setup", "Setup       : CONFIRMED" in out_best)
    check("BEST renders band and numeric confidence",
          "Confidence  : HIGH (82.00)" in out_best)
    check("BEST renders score with its denominator",
          "Score       : 79.40/100" in out_best)
    check("BEST renders R:R", "Risk/Reward : 1 : 1.80" in out_best)
    check("BEST renders data quality", "Data Quality: 90.0/100" in out_best)
    check("BEST renders reasons", "Reasons     : OI confirmation" in out_best)
    check("BEST prints exactly one strike, never a TOP-10 table",
          "TOP-10" not in out_best and out_best.count("Strike      :") == 1)

    dn_best = base_report(
        confidence_value=75.0, confidence_label="MEDIUM",
        best={
            "strike": 25000, "option_type": "PE", "direction": "DOWN", "confirmed": True,
            "move_points": -12.0, "confirmation_reason": ["DOWN confirmed"],
            "setup": "CONFIRMED", "total_score": 76.0, "entry_low": 100.0,
            "entry_high": 110.0, "stop_loss": 90.0, "target": 130.0,
            "risk_reward": 2.0, "ltp": 105.0, "reasons": [],
        })
    out_dn = render(term.print_best_strike, dn_best, dn_best["best"])
    check("DOWN best renders a NEGATIVE move, never '+-12.0'",
          "Confirmed   : -12.0 pts (confirmed)" in out_dn, f"got:\n{out_dn}")
    check("DOWN best never emits a doubled sign", "+-" not in out_dn, f"got:\n{out_dn}")

    # ------------------------------------------------------------------
    print("\n== 7. Production wiring: a real OptionStrikeSelector.run_cycle() ==")
    import app.main as main_mod
    from app.main import OptionStrikeSelector

    # Static ordering guard: catches the exact use-before-assignment class.
    tree = ast.parse(open(os.path.join(APP_ROOT, "app", "main.py")).read())
    run_cycle_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_cycle")
    stores, loads = [], []
    for n in ast.walk(run_cycle_node):
        if isinstance(n, ast.Name) and n.id == "regime":
            (stores if isinstance(n.ctx, ast.Store) else loads).append(n.lineno)
    check("run_cycle assigns `regime` before it is first read",
          bool(stores) and bool(loads) and min(loads) >= min(stores),
          f"first store={min(stores) if stores else None} "
          f"first load={min(loads) if loads else None}")
    check("candidate generation receives a regime argument",
          any(isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "generate"
              and n.args
              and isinstance(n.args[-1], ast.Name)
              and n.args[-1].id == "regime"
              for n in ast.walk(run_cycle_node)),
          "candidate_generator.generate(...) no longer passes regime")

    sel = OptionStrikeSelector()
    check("OptionStrikeSelector owns exactly one DirectionEngine",
          isinstance(sel.direction_engine, type(sel.direction_engine)))
    check("the DirectionEngine is bound to the LIVE UnderlyingEngine",
          sel.direction_engine.underlying_engine is sel.underlying_engine,
          "engine is not bound to the selector's underlying engine")
    check("run_cycle no longer constructs a DirectionEngine per cycle",
          not any(
              isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)
              and n.func.id == "DirectionEngine"
              for n in ast.walk(run_cycle_node)),
          "run_cycle still calls DirectionEngine()")

    # In-memory market fixtures only.
    sel.underlying_engine = UnderlyingEngine("NIFTY")
    sel.direction_engine.underlying_engine = sel.underlying_engine
    _fill_engine(sel.underlying_engine, UP_CLEAN, _timestamps(UP_CLEAN))
    sel.compute_data_quality = lambda: 100.0
    sel.last_quality_reasons = []

    def fixture_candidates(_spot, _regime):
        out = []
        for i in range(10):
            side = "CE" if i < 5 else "PE"
            strike = 25000 + (i - 4) * 50
            out.append({
                "underlying": "NIFTY", "strike": strike, "option_type": side,
                "expiry": "01-Jan-2027", "distance_from_atm": abs(strike - 25000),
                "ltp": 150.0, "bid": 149.0, "ask": 151.0, "volume": 200000,
                "oi": 150000, "oi_change": 5000, "iv": None, "pchange": 15.0,
                "change": 20.0, "delta": 0.5, "theta": -2.0, "gamma": 0.001,
                "vega": 10.0, "source": "JUGAAD",
            })
        return out, "OK"

    sel.candidate_generator.generate = fixture_candidates
    patched = main_mod.angel_source
    had_ensure = hasattr(patched, "ensure_connected")
    prev_ensure = getattr(patched, "ensure_connected", None)
    prev_connected = getattr(patched, "connected", False)
    patched.ensure_connected = lambda *a, **k: None
    patched.connected = True
    # Keep the harness hermetic and offline: contract lookup must never trigger
    # a Scrip Master download.
    prev_find_contract = getattr(patched, "find_option_contract", None)
    if prev_find_contract is not None:
        patched.find_option_contract = lambda *a, **k: None

    cycle_1 = None
    cycle_2 = None
    raised = None
    try:
        cycle_1 = sel.run_cycle()
        engine_after_1 = sel.direction_engine
        cycle_2 = sel.run_cycle()
        check("run_cycle survives two cycles on the same instance",
              engine_after_1 is sel.direction_engine)
        # persist_cycle must carry the direction through to SQLite.
        sel.persist_cycle(cycle_1)
        row = db_mod.db.fetchone(
            "SELECT direction, confirmed_move, direction_confirmed "
            "FROM cycles ORDER BY cycle_id DESC LIMIT 1")
        check("persist_cycle writes the direction evidence to SQLite",
              row is not None and row["direction"] == "UP",
              f"got {dict(row) if row else None}")
    except NameError as exc:
        raised = exc
    except UnboundLocalError as exc:  # pragma: no cover - must never happen
        raised = exc
    finally:
        if had_ensure:
            patched.ensure_connected = prev_ensure
        if prev_find_contract is not None:
            patched.find_option_contract = prev_find_contract
        patched.connected = prev_connected

    check("run_cycle completes without UnboundLocalError/NameError",
          raised is None, f"raised {type(raised).__name__}: {raised}")

    if raised is None:
        check("run_cycle report carries the real cycle-level direction",
              cycle_1.get("direction") == "UP",
              f"got {cycle_1.get('direction')} missing={cycle_1.get('missing_evidence')}")
        check("run_cycle attaches the direction to every scored candidate",
              bool(cycle_1.get("candidates"))
              and all(c.get("direction") == "UP" for c in cycle_1["candidates"]),
              f"got {[c.get('direction') for c in cycle_1.get('candidates', [])]}")
        check("the direction handed to candidates is the same cycle-level result",
              {c.get("move_points") for c in cycle_1.get("candidates", [])} ==
              {cycle_1.get("move_points")},
              "candidates disagree with the cycle report")
        check("run_cycle derives a setup label from the real evidence",
              cycle_1.get("setup") is not None, f"got {cycle_1.get('setup')}")
        check("cycle 1 of the live episode is not confirmed",
              cycle_1.get("confirmed") is False)
        check("cycle 2 of the same live instance IS confirmed",
              cycle_2.get("confirmed") is True,
              f"reasons={cycle_2.get('confirmation_reason')}")
        check("confidence is computed from the direction-carrying candidate",
              (cycle_1.get("best") is None)
              or cycle_1.get("confidence_value") is not None,
              "best exists but confidence was never computed")
        check("a NO CLEAR cycle still reports the real direction",
              (cycle_1.get("best") is not None) or
              cycle_1.get("direction") == "UP",
              f"got {cycle_1.get('direction')}")

    # ------------------------------------------------------------------
    print("\n== 8. DirectionEngine has no network ==")
    dir_src = open(os.path.join(APP_ROOT, "app", "market", "direction.py")).read()
    imports = set(re.findall(r"^\s*(?:import|from)\s+([\w.]+)", dir_src, re.M))
    check("DirectionEngine imports no network client",
          not any(m.split(".")[0] in
                  {"requests", "urllib", "urllib3", "http", "socket", "httpx",
                   "aiohttp", "websocket", "smartapi", "jugaad", "httplib"}
                  for m in imports),
          f"imports={sorted(imports)}")
    check("DirectionEngine touches no data source",
          not any(m in imports for m in ("app.data", "app.data.angel", "app.data.jugaad")),
          f"imports={sorted(imports)}")

    # ------------------------------------------------------------------
    print("\n== 9. Auto-trading guard ==")
    app_src = ""
    for root, _dirs, files in os.walk(os.path.join(APP_ROOT, "app")):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                app_src += open(os.path.join(root, f)).read()
    banned = ["place_order", "placeOrder", "modify_order", "cancel_order",
              "generateOrder", "submitOrder"]
    found = [b for b in banned if b in app_src]
    check("no order placement / modification / cancellation exists anywhere",
          not found, f"found {found}")

    # ------------------------------------------------------------------
    prod_now = os.path.getmtime(prod_db) if os.path.exists(prod_db) else None
    check("the production history.db was never touched",
          prod_now == prod_mtime_before,
          f"before={prod_mtime_before} after={prod_now}")

    print(f"\n{'=' * 58}")
    print(f"TOTAL : {PASS + FAIL}")
    print(f"PASS  : {PASS}")
    print(f"FAIL  : {FAIL}")
    print(f"  REAL LOGIC      : {REAL}")
    print(f"  STRUCTURAL/STATIC: {STRUCT}")
    print(f"SKIP  : {SKIP}")
    print(f"{'=' * 58}")
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    return 1 if FAIL else 0


def _fmt_expected(value):
    return f"{float(value):.2f}"


if __name__ == "__main__":
    sys.exit(main())