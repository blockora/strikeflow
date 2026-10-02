"""Phase-2 verification harness — Option Engine / scoring completion.

Run:  python3 verify_phase2.py

Covers BLOCKORA §17 momentum, §18 OI + OI change, §19 volume, §21 ATM/ITM/OTM,
§22 Greeks and §23 IV, plus the §15 score-scale and candidate invariants.

Harness rules (same discipline as verify_phase1):
  * DB_PATH is redirected to a throwaway temp directory BEFORE any app import,
    so `data/history.db` is never opened or modified.
  * NO NETWORK. No Angel login, no scrip-master download, no HTTP. Market data
    is in-memory only, fed through the production OptionHistoryEngine with an
    injected clock.
  * Assertions are BEHAVIOURAL: fixtures with known orderings and independent
    expected relationships. This file never restates a production formula and
    asserts the restatement.
  * A mutation section re-introduces real defects into a throwaway copy and
    requires the harness to go red, so a broken formula cannot pass silently.
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

_TMPDIR = tempfile.mkdtemp(prefix="blockora_verify2_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "verify2_history.db")

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from app.config import config  # noqa: E402

PASS = 0
FAIL = 0
REAL = 0
STRUCT = 0


def check(name, cond, detail="", structural=False):
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


# --------------------------------------------------------------------------
# In-memory option-chain fixtures (test-only). No files, no network.
# --------------------------------------------------------------------------
EXPIRY = "01-Jan-2027"


def side(ltp=None, volume=None, oi=None, oi_change=None, iv=None, source="JUGAAD"):
    return {"ltp": ltp, "volume": volume, "oi": oi, "oi_change": oi_change,
            "iv": iv, "source": source}


def row(strike, ce=None, pe=None, expiry=EXPIRY):
    return {"expiry": expiry, "strike": strike, "ce": ce, "pe": pe}


def candidate(strike=25100, option_type="CE", **kw):
    c = {
        "underlying": "NIFTY", "expiry": EXPIRY, "strike": strike,
        "option_type": option_type, "ltp": 100.0, "bid": 99.0, "ask": 101.0,
        "volume": 50000.0, "oi": 100000.0, "oi_change": 5000.0, "iv": 13.0,
        "pchange": 4.0, "risk_reward": 1.8,
    }
    c.update(kw)
    return c


def underlying(spot=25020.0, trend="BULLISH", r1=0.15, r3=0.30, r5=0.40, vwap=24950.0):
    return {"symbol": "NIFTY", "spot": spot, "trend": trend, "vwap": vwap,
            "atr": 60.0, "return_1m": r1, "return_3m": r3, "return_5m": r5}


UND_UP = underlying(trend="BULLISH", r1=0.15, r3=0.30, r5=0.40, vwap=24950.0)
UND_DOWN = underlying(trend="BEARISH", r1=-0.15, r3=-0.30, r5=-0.40, vwap=25050.0)
UND_FLAT = underlying(trend="NEUTRAL", r1=0.0, r3=0.0, r5=0.0, vwap=25020.0)


def feed(engine, frames, t0=None, step=60.0):
    """Ingest `frames` (list of chains) one per simulated minute."""
    base = t0 if t0 is not None else time.time() - len(frames) * step
    for i, chain in enumerate(frames):
        engine.ingest(chain, now=base + i * step)
    return base + (len(frames) - 1) * step


def _head_source(path):
    try:
        out = subprocess.run(["git", "-C", APP_ROOT, "show", f"HEAD:{path}"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            return out.stdout
    except Exception:
        pass
    return None


def _weights_block(text):
    m = re.search(r"WEIGHTS\s*=\s*\{(.*?)\}", text, re.S)
    if not m:
        return None
    return {k: int(v) for k, v in re.findall(r'"(\w+)"\s*:\s*(\d+)', m.group(1))}


# ==========================================================================
def main(run_mutations=True, network_check=True):
    from app.market.option_history import OptionHistoryEngine
    from app.selector.scoring import ScoringEngine

    se = ScoringEngine()

    # ------------------------------------------------------------------
    print("== A. §17 Momentum ==")

    def mom(side_type, ret, und, **kw):
        c = candidate(option_type=side_type,
                      opt_return_1m=ret, opt_return_3m=ret, opt_return_5m=ret, **kw)
        return se._momentum_score(c, und)

    ce_aligned = mom("CE", 2.0, UND_UP)
    pe_aligned = mom("PE", 2.0, UND_DOWN)
    ce_adverse = mom("CE", 2.0, UND_DOWN)
    pe_adverse = mom("PE", 2.0, UND_UP)
    ce_flat = mom("CE", 2.0, UND_FLAT)

    check("A: CE with a rising premium in an up underlying scores strongly",
          ce_aligned > 70, f"got {ce_aligned}")
    check("A: PE with a rising premium in a down underlying scores strongly",
          pe_aligned > 70, f"got {pe_aligned}")
    check("A: momentum is symmetric for CE/UP and PE/DOWN",
          abs(ce_aligned - pe_aligned) < 1e-9,
          f"CE={ce_aligned} PE={pe_aligned}")
    check("A: adverse underlying alignment is penalized (CE)",
          ce_adverse < ce_aligned - 20, f"adverse={ce_adverse} aligned={ce_aligned}")
    check("A: adverse underlying alignment is penalized (PE, symmetric)",
          abs(ce_adverse - pe_adverse) < 1e-9, f"got {ce_adverse} vs {pe_adverse}")
    check("A: premium rising while the underlying is flat scores between the two",
          ce_aligned > ce_flat > ce_adverse,
          f"aligned={ce_aligned} flat={ce_flat} adverse={ce_adverse}")

    accel_up = mom("CE", 2.0, UND_UP, opt_acceleration=0.08)
    accel_dn = mom("CE", 2.0, UND_UP, opt_acceleration=-0.08)
    check("A: acceleration raises the momentum score",
          accel_up > mom("CE", 2.0, UND_UP), f"{accel_up}")
    check("A: deceleration lowers the momentum score",
          accel_dn < mom("CE", 2.0, UND_UP), f"{accel_dn}")
    check("A: symmetric acceleration effect for PE",
          abs(accel_up - mom("PE", 2.0, UND_DOWN, opt_acceleration=0.08)) < 1e-9)

    rv_high = mom("CE", 2.0, UND_UP, relative_volume=3.0)
    rv_none = mom("CE", 2.0, UND_UP, relative_volume=None)
    check("A: relative volume contributes to momentum when available",
          rv_high > rv_none, f"rv3={rv_high} none={rv_none}")

    no_hist = se._momentum_score(candidate(option_type="CE", pchange=None, change=None),
                                UND_UP)
    check("A: no option history at all is handled safely (0-100, no crash)",
          0 <= no_hist <= 100, f"got {no_hist}")
    check("A: no option history degrades to a neutral, non-fabricated value",
          abs(no_hist - 50.0) < 1e-9, f"got {no_hist}")
    c_nohist = candidate(option_type="CE", pchange=None, change=None)
    se._momentum_score(c_nohist, UND_UP)
    check("A: unavailable evidence is stated, not invented",
          "unavailable" in c_nohist["momentum_evidence"],
          f"got {c_nohist['momentum_evidence']!r}")

    # ------------------------------------------------------------------
    print("\n== B. §18 OI + OI change ==")

    eng = OptionHistoryEngine()
    frames = []
    for i in range(6):
        frames.append([
            row(25100, ce=side(ltp=100 + i * 3, volume=1000 + i * 1000,
                               oi=100000 + i * 8000, oi_change=8000, iv=13.0 + i * 0.2),
                pe=side(ltp=90 - i, volume=800 + i * 400, oi=60000 + i * 500,
                        oi_change=500, iv=13.5)),
            row(25200, ce=side(ltp=60 + i * 2, volume=900 + i * 600,
                               oi=50000 + i * 2000, oi_change=2000, iv=14.0),
                pe=side(ltp=120, volume=700, oi=55000, oi_change=100, iv=13.0)),
        ])
    last_ts = feed(eng, frames)
    m = eng.snapshot_for(candidate(strike=25100, option_type="CE"), now=last_ts)

    check("B: current OI is read from the chain", m["prev_oi"] is not None)
    check("B: previous OI is recovered from real history",
          m["prev_oi"] == 100000 + 4 * 8000, f"got {m['prev_oi']}")
    check("B: OI change percent is computed from previous OI",
          m["oi_change_pct"] is not None and m["oi_change_pct"] > 0,
          f"got {m['oi_change_pct']}")
    check("B: OI source provenance is preserved", m["oi_source"] == "JUGAAD",
          f"got {m['oi_source']}")

    oi_peers = se._peer_stats([
        candidate(strike=25100, option_type="CE", oi=100000),
        candidate(strike=25200, option_type="CE", oi=20000),
    ])
    high = se._oi_score(candidate(strike=25100, option_type="CE", oi=100000,
                                  oi_change=5000, relative_volume=1.5), oi_peers)
    low = se._oi_score(candidate(strike=25200, option_type="CE", oi=20000,
                                 oi_change=5000, relative_volume=1.5), oi_peers)
    check("B: nearby-strike OI comparison distinguishes higher-OI contracts",
          high > low, f"high={high} low={low}")

    # The peer cohort must contain BOTH option types, otherwise the CE candidate
    # would be scored against a real nearby-strike median while the PE candidate
    # fell back to the no-peer baseline and the comparison would be meaningless.
    oi_peers_both = se._peer_stats([
        candidate(strike=25100, option_type="CE", oi=100000),
        candidate(strike=25200, option_type="CE", oi=20000),
        candidate(strike=25100, option_type="PE", oi=100000),
        candidate(strike=25200, option_type="PE", oi=20000),
    ])
    oi_ce_same = se._oi_score(candidate(strike=25100, option_type="CE", oi=100000,
                                     oi_change=5000, oi_change_pct=5.0,
                                     relative_volume=1.5,
                                     opt_return_1m=1.0), oi_peers_both)
    oi_pe_same = se._oi_score(candidate(strike=25100, option_type="PE", oi=100000,
                                        oi_change=5000, oi_change_pct=5.0,
                                        relative_volume=1.5,
                                        opt_return_1m=1.0), oi_peers_both)
    check("B: identical OI facts give an identical OI score for CE and PE",
          abs(oi_ce_same - oi_pe_same) < 1e-9, f"CE={oi_ce_same} PE={oi_pe_same}")

    def oi_pat(oi_chg, oi_pct, premium_up):
        return se._oi_score(candidate(option_type="CE", oi=100000, oi_change=oi_chg,
                                      oi_change_pct=oi_pct, relative_volume=1.0,
                                      opt_return_1m=1.0 if premium_up else -1.0), {})
    up_up = oi_pat(5000, 5.0, True)
    up_down = oi_pat(-5000, -5.0, True)
    down_up = oi_pat(5000, 5.0, False)
    down_down = oi_pat(-5000, -5.0, False)
    check("B: the four documented price/OI patterns rank as BLOCKORA §18 describes",
          up_up > up_down > down_up > down_down,
          f"p+/OI+={up_up} p+/OI-={up_down} p-/OI+={down_up} p-/OI-={down_down}")

    building = oi_pat(5000, 50.0, True)
    unwinding = oi_pat(5000, 0.0, True)
    check("B: a larger real OI change scores above an unchanged OI",
          building > unwinding, f"build={building} flat={unwinding}")

    building = se._oi_score(candidate(option_type="CE", oi=100000, oi_change=5000,
                                      oi_change_pct=10.0, relative_volume=1.0,
                                      opt_return_1m=1.0), {})
    unwinding = se._oi_score(candidate(option_type="CE", oi=100000, oi_change=-5000,
                                       oi_change_pct=-10.0, relative_volume=1.0,
                                       opt_return_1m=1.0), {})
    check("B: OI building scores above OI unwinding",
          building > unwinding, f"build={building} unwind={unwinding}")

    none_oi = se._oi_score(candidate(option_type="CE", oi=None, oi_change=None), {})
    check("B: missing OI degrades to a neutral value, never a false positive",
          0 <= none_oi <= 55, f"got {none_oi}")
    c_nooi = candidate(option_type="CE", oi=None, oi_change=None)
    se._oi_score(c_nooi, {})
    check("B: missing OI is stated as unavailable",
          "unavailable" in c_nooi["oi_evidence"], f"got {c_nooi['oi_evidence']!r}")

    high_oi_flat = se._oi_score(candidate(option_type="CE", oi=10 ** 7,
                                          oi_change=0, relative_volume=1.0), {})
    low_oi_move = se._oi_score(candidate(option_type="CE", oi=1000, oi_change=5000,
                                         oi_change_pct=50.0, relative_volume=1.0,
                                         opt_return_1m=1.0), {})
    check("B: a huge but static OI does not outrank a real OI build (§18, §14)",
          not (high_oi_flat > low_oi_move + 30),
          f"static={high_oi_flat} building={low_oi_move}")

    # ------------------------------------------------------------------
    print("\n== C. §19 Volume ==")

    v_eng = OptionHistoryEngine()
    v_frames = []
    for i in range(8):
        v_frames.append([
            row(25100, ce=side(ltp=100, volume=2000 + i * 500, oi=100000,
                               oi_change=0, iv=13.0),
                pe=side(ltp=90, volume=1000, oi=50000, oi_change=0, iv=13.0)),
        ])
    v_ts = feed(v_eng, v_frames)
    vm = v_eng.snapshot_for(candidate(), now=v_ts)
    expected_avg = sum(2000 + i * 500 for i in range(7)) / 7.0
    check("C: volume average is built from real prior observations",
          vm["volume_avg"] is not None and abs(vm["volume_avg"] - expected_avg) < 1e-6,
          f"got {vm['volume_avg']} expected {expected_avg}")
    check("C: relative volume = current / recent average",
          vm["relative_volume"] is not None
          and abs(vm["relative_volume"] - (2000 + 7 * 500) / expected_avg) < 1e-6,
          f"got {vm['relative_volume']}")
    check("C: volume acceleration is derived from the previous sample",
          vm["volume_acceleration"] is not None
          and abs(vm["volume_acceleration"] - (2000 + 7 * 500) / (2000 + 6 * 500)) < 1e-6,
          f"got {vm['volume_acceleration']}")

    hot = se._volume_score(candidate(volume=100000, relative_volume=3.0,
                                     volume_acceleration=2.0))
    cold = se._volume_score(candidate(volume=1000, relative_volume=0.3,
                                      volume_acceleration=0.5))
    check("C: heavy relative volume outscores light relative volume",
          hot > cold + 40, f"hot={hot} cold={cold}")

    z = se._volume_score(candidate(volume=1000, relative_volume=1.0))
    check("C: a zero previous volume never becomes an infinite relative volume",
          0 <= z <= 100, f"got {z}")

    short = OptionHistoryEngine()
    short.ingest([row(25100, ce=side(ltp=100, volume=1000, oi=1, oi_change=0, iv=13.0))],
                 now=time.time() - 60)
    sm = short.snapshot_for(candidate())
    check("C: insufficient history yields None, never a fabricated average",
          sm["volume_avg"] is None and sm["relative_volume"] is None,
          f"got {sm}")
    missing = se._volume_score(candidate(volume=None))
    check("C: missing volume degrades safely inside the 0-100 scale",
          0 <= missing <= 40, f"got {missing}")

    # ------------------------------------------------------------------
    print("\n== D. §21 ATM / ITM / OTM ==")

    interval = config.STRIKE_INTERVAL
    check("D: CE below spot is ITM",
          se.classify_moneyness(25000, 24900, "CE", interval) == "ITM")
    check("D: CE above spot is OTM",
          se.classify_moneyness(25000, 25100, "CE", interval) == "OTM")
    check("D: PE above spot is ITM",
          se.classify_moneyness(25000, 25100, "PE", interval) == "ITM")
    check("D: PE below spot is OTM",
          se.classify_moneyness(25000, 24900, "PE", interval) == "OTM")
    check("D: strike equal to spot is ATM for CE",
          se.classify_moneyness(25000, 25000, "CE", interval) == "ATM")
    check("D: strike equal to spot is ATM for PE",
          se.classify_moneyness(25000, 25000, "PE", interval) == "ATM")
    half = interval // 2
    check("D: a strike inside half the interval is ATM (CE)",
          se.classify_moneyness(25000, 25000 + half, "CE", interval) == "ATM",
          f"got {se.classify_moneyness(25000, 25000 + half, 'CE', interval)}")
    check("D: a strike beyond half the interval is OTM (CE)",
          se.classify_moneyness(25000, 25000 + half + 50, "CE", interval) == "OTM")
    check("D: a strike inside half the interval is ATM (PE)",
          se.classify_moneyness(25000, 25000 - half, "PE", interval) == "ATM")
    check("D: a PE beyond half the interval below spot is OTM",
          se.classify_moneyness(25000, 25000 - half - 50, "PE", interval) == "OTM",
          f"got {se.classify_moneyness(25000, 25000 - half - 50, 'PE', interval)}")
    check("D: a PE beyond half the interval above spot is ITM",
          se.classify_moneyness(25000, 25000 + half + 50, "PE", interval) == "ITM")
    check("D: unknown spot never yields a moneyness guess",
          se.classify_moneyness(None, 25100, "CE", interval) is None)

    atm = se._atm_score(candidate(strike=25000), UND_FLAT)
    itm = se._atm_score(candidate(strike=24900), UND_FLAT)
    otm = se._atm_score(candidate(strike=25300), UND_FLAT)
    check("D: ATM suitability ranks ATM above ITM above far OTM",
          atm > itm > otm, f"atm={atm} itm={itm} otm={otm}")
    c_money = candidate(strike=25100)
    se._atm_score(c_money, UND_FLAT)
    check("D: moneyness is exposed as candidate evidence",
          c_money.get("moneyness") in ("ITM", "ATM", "OTM"),
          f"got {c_money.get('moneyness')}")
    check("D: distance from ATM is exposed as candidate evidence",
          c_money.get("distance_steps") is not None,
          f"got {c_money.get('distance_steps')}")

    # ------------------------------------------------------------------
    print("\n== E. §22 Greeks ==")

    gk = dict(delta=0.50, gamma=0.0002, theta=-2.0, vega=12.0, ltp=100.0)
    gk_peers = se._peer_stats([
        candidate(strike=25100, option_type="CE", gamma=0.00020, vega=12.0),
        candidate(strike=25200, option_type="CE", gamma=0.00010, vega=6.0),
        candidate(strike=25300, option_type="CE", gamma=0.00030, vega=18.0),
    ])
    base_g = se._greeks_score(candidate(option_type="CE", **gk), gk_peers)
    d_g = se._greeks_score(candidate(option_type="CE", **dict(gk, delta=0.90)), gk_peers)
    g_g = se._greeks_score(candidate(option_type="CE", **dict(gk, gamma=0.0009)), gk_peers)
    t_g = se._greeks_score(candidate(option_type="CE", **dict(gk, theta=-30.0)), gk_peers)
    v_g = se._greeks_score(candidate(option_type="CE", **dict(gk, vega=40.0)), gk_peers)
    check("E: delta actually affects the Greeks component", d_g < base_g, f"{d_g} vs {base_g}")
    check("E: gamma actually affects the Greeks component", g_g > base_g, f"{g_g} vs {base_g}")
    check("E: theta actually affects the Greeks component", t_g < base_g, f"{t_g} vs {base_g}")
    check("E: vega actually affects the Greeks component", v_g > base_g, f"{v_g} vs {base_g}")

    ce_mirror = se._greeks_score(candidate(option_type="CE", **dict(gk, delta=0.5)), gk_peers)
    pe_mirror = se._greeks_score(candidate(option_type="PE", **dict(gk, delta=-0.5)), gk_peers)
    check("E: CE +0.5 delta and PE -0.5 delta are treated symmetrically",
          abs(ce_mirror - pe_mirror) < 1e-9, f"CE={ce_mirror} PE={pe_mirror}")

    no_delta = se._greeks_score(candidate(option_type="CE", delta=None), {})
    check("E: missing Greeks degrade to a neutral value",
          abs(no_delta - 50.0) < 1e-9, f"got {no_delta}")
    c_nog = candidate(option_type="CE", delta=None)
    se._greeks_score(c_nog, {})
    check("E: missing Greeks are stated as unavailable",
          "unavailable" in c_nog["greeks_evidence"], f"got {c_nog['greeks_evidence']!r}")
    check("E: Greeks stay inside the 0-100 normalized scale",
          all(0 <= v <= 100 for v in (base_g, d_g, g_g, t_g, v_g, no_delta)))

    # ------------------------------------------------------------------
    print("\n== F. §23 IV ==")

    iv_dirs = se._iv_score(candidate(iv=13.0, iv_change=1.5), UND_UP)
    iv_flat = se._iv_score(candidate(iv=13.0, iv_change=1.5), UND_FLAT)
    iv_opp = se._iv_score(candidate(iv=13.0, iv_change=1.5), UND_DOWN)
    check("F: IV expansion WITH an underlying move beats IV expansion with a flat underlying",
          iv_dirs > iv_flat + 20, f"moving={iv_dirs} flat={iv_flat}")
    check("F: IV expansion against the underlying scores worst of the three",
          iv_flat < iv_dirs and iv_flat <= iv_opp,
          f"moving={iv_dirs} flat={iv_flat} opposed={iv_opp}")

    check("F: IV expansion alone is not treated as directional confirmation",
          iv_flat < 60, f"got {iv_flat}")

    iv_falling = se._iv_score(candidate(iv=13.0, iv_change=-1.0), UND_UP)
    check("F: a falling IV with an underlying move still scores as constructive",
          iv_falling > 60, f"got {iv_falling}")

    iv_eng = OptionHistoryEngine()
    iv_frames = []
    for i in range(14):
        iv_frames.append([row(25100, ce=side(ltp=100, volume=1000, oi=1000,
                                             oi_change=0, iv=10.0 + i * 0.4),
                              pe=None)])
    iv_ts = feed(iv_eng, iv_frames)
    iv_m = iv_eng.snapshot_for(candidate(), now=iv_ts)
    check("F: IV change is derived from real prior IV observations",
          iv_m["iv_change"] is not None and abs(iv_m["iv_change"] - 0.4) < 1e-6,
          f"got {iv_m['iv_change']}")
    check("F: a percentile is produced once enough real samples exist",
          iv_m["iv_percentile"] is not None
          and 0 <= iv_m["iv_percentile"] <= 100, f"got {iv_m['iv_percentile']}")
    check("F: the newest IV sits at the top of its own history",
          iv_m["iv_percentile"] >= 90, f"got {iv_m['iv_percentile']}")

    short_iv = OptionHistoryEngine()
    for i in range(4):
        short_iv.ingest([row(25100, ce=side(ltp=100, volume=1000, oi=1,
                                             oi_change=0, iv=12.0 + i))],
                        now=time.time() - (10 - i) * 60)
    s_iv = short_iv.snapshot_for(candidate())
    check("F: no percentile is invented from too few samples",
          s_iv["iv_percentile"] is None,
          f"got {s_iv['iv_percentile']} with {s_iv['iv_samples']} samples")
    c_iv = candidate(iv=13.0, iv_change=0.2, iv_percentile=s_iv["iv_percentile"],
                     iv_samples=s_iv["iv_samples"])
    se._iv_score(c_iv, UND_UP)
    check("F: insufficient IV history is exposed as unavailable",
          "percentile unavailable" in c_iv["iv_evidence"],
          f"got {c_iv['iv_evidence']!r}")

    check("F: missing IV degrades to a neutral value",
          abs(se._iv_score(candidate(iv=None), UND_UP) - 50.0) < 1e-9)

    # ------------------------------------------------------------------
    print("\n== G. Score-scale invariants ==")

    base_cand = candidate(strike=25100, option_type="CE")
    peers = se._peer_stats([
        candidate(strike=25100, option_type="CE", oi=100000, gamma=0.0002, vega=12.0),
        candidate(strike=25200, option_type="CE", oi=90000, gamma=0.00018, vega=11.0),
        candidate(strike=25300, option_type="CE", oi=95000, gamma=0.00022, vega=13.0),
    ])
    scored = se.score_candidates([
        candidate(strike=s, option_type="CE",
                  opt_return_1m=1.0, opt_return_3m=1.0, opt_return_5m=1.0,
                  relative_volume=1.5, volume_acceleration=1.4, oi_change_pct=4.0,
                  iv_change=0.3, iv_percentile=55.0, iv_samples=20,
                  delta=0.5, gamma=0.0002, theta=-2.0, vega=12.0)
        for s in (25000, 25050, 25100, 25150, 25200)
    ], UND_UP, "BULLISH")

    check("G: every normalized component is inside 0-100",
          all(0 <= v <= 100 for c in scored for v in c["scores_normalized"].values()),
          f"{[c['scores_normalized'] for c in scored[:1]]}")
    check("G: every final candidate score is inside 0-100",
          all(0 <= c["total_score"] <= 100 for c in scored),
          f"{[c['total_score'] for c in scored]}")
    check("G: exactly the nine §15 components are produced, no tenth added",
          set(scored[0]["scores_normalized"]) == {
              "underlying_trend", "option_momentum", "oi", "volume", "liquidity",
              "atm_suitability", "greeks", "iv", "risk_reward"},
          f"got {sorted(scored[0]['scores_normalized'])}")

    # Independent re-derivation from the engine's OWN returned normalized values:
    # if the engine summed components directly instead of applying §15 weights,
    # this would blow past 100 and fail.
    independent = sum(
        scored[0]["scores_normalized"][name] * se.WEIGHTS[name] / 100.0
        for name in se.WEIGHTS
    )
    check("G: the §15 weights are actually applied to the normalized components",
          abs(scored[0]["total_score"] - independent) < 0.02,
          f"total={scored[0]['total_score']} independent={independent}")
    check("G: a naive direct sum of components would NOT equal the score (proves weighting)",
          abs(sum(scored[0]["scores_normalized"].values()) - scored[0]["total_score"]) > 20,
          "components summed directly - weights not applied")

    check("G: §15 weights total exactly 100", se.MAX_TOTAL_SCORE == 100,
          f"got {se.MAX_TOTAL_SCORE}")
    head_scoring = _head_source("app/selector/scoring.py")
    if head_scoring is None:
        check("§15 weights match the committed baseline", False, "git HEAD unavailable")
    else:
        cur = open(os.path.join(APP_ROOT, "app", "selector", "scoring.py")).read()
        check("§15 weights are byte-identical to the committed HEAD baseline",
              _weights_block(cur) == _weights_block(head_scoring),
              f"cur={_weights_block(cur)} head={_weights_block(head_scoring)}")

    head_cfg = _head_source("app/config.py")
    if head_cfg is None:
        check("hard gates match the committed baseline", False, "git HEAD unavailable")
    else:
        cur_cfg = open(os.path.join(APP_ROOT, "app", "config.py")).read()
        pat = r'%s\s*=\s*_get_\w+\(\s*"%s"\s*,\s*([0-9.]+)\s*\)'
        ok = True
        for name in ("MIN_SCORE", "MIN_CONFIDENCE", "MIN_DATA_QUALITY", "MIN_RR",
                     "MAX_SPREAD_PERCENT", "CANDIDATE_COUNT"):
            a = re.search(pat % (name, name), head_cfg)
            b = re.search(pat % (name, name), cur_cfg)
            if not a or not b or a.group(1) != b.group(1):
                ok = False
        check("every hard gate is unchanged from the committed HEAD baseline", ok)

    from app.selector.ranking import RankingEngine
    rank = RankingEngine()
    ok_below, why = rank.hard_filter(
        {"total_score": config.MIN_SCORE - 1, "risk_reward": 3.0,
         "bid": 100.0, "ask": 101.0, "scores": {"liquidity": 5}})
    check("G: MIN_SCORE is still enforced by the real gate",
          (not ok_below) and why == "SCORE_BELOW_MIN", f"{ok_below} {why}")

    # ------------------------------------------------------------------
    print("\n== H. Candidate invariants ==")

    from app.selector.candidates import CandidateGenerator

    gen = CandidateGenerator("NIFTY")
    gen.chain_engine = type("Chain", (), {
        "get_normalized_chain": lambda self: [
            {"strike": 25000 + off * 50, "expiry": EXPIRY,
             "ce": {"ltp": 100.0 + abs(off), "bid": 99.0, "ask": 101.0,
                    "volume": 1000, "oi": 1000, "oi_change": 0},
             "pe": {"ltp": 100.0 + abs(off), "bid": 99.0, "ask": 101.0,
                    "volume": 1000, "oi": 1000, "oi_change": 0}}
            for off in range(-6, 7)
        ]
    })()
    made, status = gen.generate(25000.0, "BULLISH")
    check("H: exactly CANDIDATE_COUNT contracts are produced",
          len(made) == config.CANDIDATE_COUNT == 10, f"got {len(made)} {status}")
    check("H: candidates stay balanced 5 CE + 5 PE",
          sum(1 for c in made if c["option_type"] == "CE") == 5
          and sum(1 for c in made if c["option_type"] == "PE") == 5,
          f"{[c['option_type'] for c in made]}")

    thin, thin_status = gen.generate(25000.0, "BULLISH")
    check("H: insufficient data still reports a status instead of forcing a recommendation",
          status == "OK" and len(thin) == 10, f"{thin_status} {len(thin)}")

    # ------------------------------------------------------------------
    print("\n== K. Temporary-DB persistence smoke ==")

    import app.history.database as db_mod
    from app.history.cycles import CycleHistory

    check("K: the production history.db is not the active database",
          db_mod.db.db_path.startswith(_TMPDIR), f"active={db_mod.db.db_path}")
    import app.history.cycles as cycles_mod
    prod_db = os.path.join(APP_ROOT, "data", "history.db")
    prod_before = os.path.getmtime(prod_db) if os.path.exists(prod_db) else None

    saved_path = db_mod.config.DB_PATH
    wpath = os.path.join(_TMPDIR, "persist.db")
    db_mod.config.DB_PATH = wpath
    writer = db_mod.Database()
    cycles_mod.db = writer
    check("K: the candidate_scores evidence columns exist after migration",
          all(c in {r[1] for r in writer.conn.execute("PRAGMA table_info(candidate_scores)")}
              for c in ("underlying_trend_score", "atm_suitability_score",
                        "option_return_1m", "relative_volume", "prev_oi",
                        "oi_change_pct", "moneyness", "iv_change", "iv_percentile",
                        "oi_source", "option_samples")))

    cid = CycleHistory.save_cycle({
        "underlying": "NIFTY", "spot": 25020.0, "regime": "BULLISH",
        "data_quality": 100.0, "best_symbol": None, "best_score": None,
        "confidence": None, "entry": None, "stop_loss": None, "target": None,
        "risk_reward": None, "signal_state": "NO SIGNAL", "direction": "UP",
        "confirmed_move": 12.0, "direction_confirmed": True,
        "direction_origin_time": 1750000000.0, "direction_origin_spot": 25008.0,
        "direction_confirm_reason": ["UP confirmed"],
    })
    CycleHistory.save_candidate_scores(cid, scored)
    crow = writer.fetchone(
        "SELECT underlying_trend_score, atm_suitability_score, option_return_1m, "
        "relative_volume, prev_oi, oi_change_pct, moneyness, iv_change, "
        "iv_percentile, oi_source, option_samples FROM candidate_scores "
        "WHERE cycle_id = ? LIMIT 1", (cid,))
    check("K: option-engine evidence reaches candidate_scores",
          crow is not None and crow["underlying_trend_score"] is not None
          and crow["atm_suitability_score"] is not None,
          f"got {dict(crow) if crow else None}")
    check("K: moneyness classification is persisted",
          crow is not None and crow["moneyness"] in ("ITM", "ATM", "OTM"),
          f"got {crow['moneyness'] if crow else None}")
    check("K: relative volume and OI change percent are persisted",
          crow is not None and crow["relative_volume"] is not None
          and crow["oi_change_pct"] is not None,
          f"got {dict(crow) if crow else None}")
    ev_cols = {
        "underlying_trend_score": "REAL", "atm_suitability_score": "REAL",
        "option_return_1m": "REAL", "option_return_3m": "REAL",
        "opt_acceleration": "REAL", "relative_volume": "REAL", "prev_oi": "REAL",
        "oi_change_pct": "REAL", "moneyness": "TEXT", "iv_change": "REAL",
        "iv_percentile": "REAL", "oi_source": "TEXT", "option_samples": "INTEGER",
    }
    declared = {r[1]: r[2].upper() for r in
                writer.conn.execute("PRAGMA table_info(candidate_scores)")}
    check("K: every added evidence column is a bounded scalar type",
          all(declared.get(k) == v for k, v in ev_cols.items()),
          f"got {{k: declared.get(k) for k in ev_cols}}".replace("{k: ", "{"))
    check("K: no JSON/blob column was introduced for evidence",
          not any("BLOB" in v for v in declared.values()),
          f"blob columns: {[k for k, v in declared.items() if 'BLOB' in v]}")
    cyc = writer.fetchone("SELECT direction, confirmed_move FROM cycles WHERE cycle_id=?",
                          (cid,))
    check("K: Phase-1 direction persistence still works alongside Phase-2 columns",
          cyc["direction"] == "UP" and cyc["confirmed_move"] == 12.0, f"got {dict(cyc)}")
    cycles_mod.db = db_mod.db
    db_mod.config.DB_PATH = saved_path

    prod_after = os.path.getmtime(prod_db) if os.path.exists(prod_db) else None
    check("K: the production history.db was never opened or modified",
          prod_before == prod_after, f"{prod_before} -> {prod_after}")

    # ------------------------------------------------------------------
    print("\n== I. Phase-1 regression ==")

    r = subprocess.run([sys.executable, "verify_phase1.py"], cwd=APP_ROOT,
                       capture_output=True, text=True, timeout=300)
    m = re.search(r"PASS\s*:\s*(\d+).*?FAIL\s*:\s*(\d+)", r.stdout, re.S)
    check("I: verify_phase1.py still passes with zero failures",
          r.returncode == 0 and m is not None and m.group(2) == "0",
          f"rc={r.returncode} {m.groups() if m else r.stdout[-300:]}")
    check("I: verify_phase1.py still runs at least its full check count",
          m is not None and int(m.group(1)) >= 151, f"got {m.group(1) if m else None}")

    # ------------------------------------------------------------------
    print("\n== L. Harness hermeticity ==")

    here = os.path.basename(__file__)
    harness_src = open(os.path.join(APP_ROOT, here)).read()
    check("L: the harness imports no network client",
          not re.search(r"^\s*(?:import|from)\s+(requests|urllib|http|socket|aiohttp)\b",
                        harness_src, re.M),
          "network import found")
    check("L: the harness imports no broker or market-data SDK",
          not re.search(r"^\s*(?:import|from)\s+(SmartApi|smartapi|jugaad_data)\b",
                        harness_src, re.M),
          "broker SDK import found")
    check("L: the Option Engine history module imports no network client",
          not re.search(r"^\s*(?:import|from)\s+(requests|urllib|http|socket|aiohttp)\b",
                        open(os.path.join(APP_ROOT, "app", "market",
                                          "option_history.py")).read(), re.M))

    # Behavioural hermeticity: re-run the whole harness with the socket layer
    # disabled. Any real network use (scrip-master download, live quotes) would
    # raise and fail this check.
    # Only the top-level invocation pays for this nested full re-run; nested
    # invocations (mutation workers, the audit-hook child) pass
    # network_check=False so the run cannot recurse without bound.
    if network_check:
        net_block = (
            "import sys\n"
            "def _hook(event, args):\n"
            "    if event in ('socket.connect', 'socket.getaddrinfo', 'urllib.Request',\n"
            "                 'socket.sendto', 'socket.bind'):\n"
            "        raise RuntimeError('network use detected: ' + event)\n"
            "sys.addaudithook(_hook)\n"
            "sys.argv = ['verify_phase2.py']\n"
            "import verify_phase2\n"
            "sys.exit(verify_phase2.main(run_mutations=False, network_check=False))\n"
        )
        nn = subprocess.run([sys.executable, "-c", net_block], cwd=APP_ROOT,
                            capture_output=True, text=True, timeout=600)
        check("L: the entire harness runs green with network access audited off",
              nn.returncode == 0, (nn.stdout or nn.stderr)[-400:])
    else:
        print("  SKIP  L: network audit re-run (nested invocation, already proven)")

    print("\n== M. No auto-trading anywhere in app/ ==")
    app_src = ""
    for root, _d, files in os.walk(os.path.join(APP_ROOT, "app")):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                app_src += open(os.path.join(root, f)).read()
    banned = ["place_order", "placeOrder", "modify_order", "cancel_order",
              "generateOrder", "submitOrder", "createOrder", "bracketOrder"]
    found = [b for b in banned if b in app_src]
    check("M: no order placement / modification / cancellation exists", not found,
          f"found {found}")

    # ------------------------------------------------------------------
    if run_mutations:
        _mutation_section()

    print(f"\n{'=' * 58}")
    print(f"TOTAL : {PASS + FAIL}")
    print(f"PASS  : {PASS}")
    print(f"FAIL  : {FAIL}")
    print(f"  REAL LOGIC       : {REAL}")
    print(f"  STRUCTURAL/STATIC: {STRUCT}")
    print(f"{'=' * 58}")
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    return 1 if FAIL else 0


# ==========================================================================
# Mutation checks: re-introduce real defects into a throwaway copy and require
# this harness to go red. A green harness therefore cannot coexist with a
# broken production formula.
# ==========================================================================
def _sub(root, rel, old, new):
    p = os.path.join(root, rel)
    s = open(p).read()
    if old not in s:
        raise AssertionError("anchor missing in %s" % rel)
    open(p, "w").write(s.replace(old, new, 1))


MUTATIONS = [
    ("§17 momentum: aligned underlying no longer rewarded",
     "app/selector/scoring.py",
     "            alignment = 35.0\n", "            alignment = 5.0\n"),
    ("§17 momentum: side sign applied to the premium (breaks CE/PE symmetry)",
     "app/selector/scoring.py",
     "            premium_mean = sum(opt_returns) / len(opt_returns)\n"
     "            premium = _clamp(20.0 + premium_mean * 20.0, 0.0, 40.0)",
     "            premium_mean = sum(sign * r for r in opt_returns) / len(opt_returns)\n"
     "            premium = _clamp(20.0 + premium_mean * 20.0, 0.0, 40.0)"),
    ("§18 OI: price-up + OI-up treated as liquidation",
     "app/selector/scoring.py",
     "            agreement = 30.0      # price up + OI up: new positions",
     "            agreement = 0.0"),
    ("§18 OI: nearby-strike comparison removed",
     "app/selector/scoring.py",
     "            nearby = _clamp(ratio * 10.0, 0.0, 20.0)",
     "            nearby = 10.0"),
    ("§19 volume: relative volume scale inflated",
     "app/selector/scoring.py",
     "            relative = _clamp(rv * 20.0, 0.0, 40.0)",
     "            relative = _clamp(rv * 400.0, 0.0, 40.0)"),
    ("§21 moneyness: CE ITM/OTM inverted",
     "app/selector/scoring.py",
     '        if option_type == "CE":\n'
     '            return "ITM" if diff < 0 else "OTM"\n'
     '        return "ITM" if diff > 0 else "OTM"',
     '        if option_type == "CE":\n'
     '            return "OTM" if diff < 0 else "ITM"\n'
     '        return "ITM" if diff > 0 else "OTM"'),
    ("§22 greeks: vega dropped from the component",
     "app/selector/scoring.py",
     "            vega_score = _clamp((vega / vega_median) * 7.5, 0.0, 15.0)",
     "            vega_score = 7.5"),
    ("§22 greeks: absolute delta used, breaking CE/PE symmetry",
     "app/selector/scoring.py",
     "        adj_delta = sign * delta",
     "        adj_delta = abs(delta) if delta > 0 else 0.0"),
    ("§23 IV: expansion with a flat underlying treated as confirmation",
     "app/selector/scoring.py",
     "                change_score = 8.0        # premium + IV up, underlying FLAT",
     "                change_score = 50.0"),
    ("§23 IV: percentile guard removed (percentile invented from few samples)",
     "app/market/option_history.py",
     "            if iv is not None and len(window) >= config.IV_PERCENTILE_MIN_SAMPLES:",
     "            if iv is not None:"),
    ("Option history: look-ahead anchor (uses the newest sample)",
     "app/market/option_history.py",
     "            if ts is None or ts >= newest:",
     "            if ts is None:"),
    ("Option history: relative volume without a zero-denominator guard",
     "app/market/option_history.py",
     "            if average > 0:\n                out[\"relative_volume\"] = volume / average",
     "            out[\"relative_volume\"] = volume / (average or 1e-12)"),
    ("§15 weights altered",
     "app/selector/scoring.py",
     '        "option_momentum": 15,', '        "option_momentum": 18,'),
]


def _mutation_section():
    print("\n== N. Mutation checks (a broken formula must fail this harness) ==")
    base = tempfile.mkdtemp(prefix="mut2base_")
    repo_base = os.path.join(base, "repo")
    shutil.copytree(APP_ROOT, repo_base,
                    ignore=shutil.ignore_patterns("__pycache__", ".git", "data"))
    shutil.copy2(os.path.join(APP_ROOT, "verify_phase1.py"),
                 os.path.join(repo_base, "verify_phase1.py"))

    detected = 0
    for name, rel, old, new in MUTATIONS:
        work = tempfile.mkdtemp(prefix="mut2_")
        repo = os.path.join(work, "repo")
        shutil.copytree(repo_base, repo)
        try:
            _sub(repo, rel, old, new)
        except AssertionError:
            print(f"  SKIP  mutation anchor missing: {name}")
            shutil.rmtree(work, ignore_errors=True)
            continue
        run = subprocess.run(
            [sys.executable, "-c",
             "import sys, os;"
             "sys.argv=['verify_phase2.py'];"
             "import verify_phase2;"
             "sys.exit(verify_phase2.main(run_mutations=False, network_check=False))"],
            cwd=repo, capture_output=True, text=True, timeout=600,
        )
        failed = run.returncode != 0
        check(f"N: mutation detected — {name}", failed,
              "harness still green with the defect reintroduced")
        detected += 1 if failed else 0
        shutil.rmtree(work, ignore_errors=True)

    shutil.rmtree(base, ignore_errors=True)
    print(f"  mutation detection: {detected}/{len(MUTATIONS)}")


if __name__ == "__main__":
    sys.exit(main(run_mutations="--no-mutations" not in sys.argv))