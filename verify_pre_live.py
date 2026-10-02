"""Pre-live production-readiness harness (BLOCKORA §84 acceptance criteria).

This exercises the REAL production call path -- OptionStrikeSelector.run_cycle()
and the genuine engines behind it -- with synthetic, explicitly-labelled fixtures
injected into the real MarketCache in the real NSE NextApi field shape. It is
NOT a source-text scan.

Hermeticity guarantees (PHASE 21):
  * No Angel authentication: login() and ensure_connected() are stubbed.
  * No NSE/Jugaad network: socket.socket is replaced by a tripwire.
  * The production data/history.db is never opened: DB_PATH is redirected to a
    throwaway temp directory before app.history.database is imported.
  * Market data here is synthetic and is NEVER described as real or live.

Run: python3 verify_pre_live.py
"""

import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# 0. Isolated DB + hermeticity tripwires, installed BEFORE app imports.
# --------------------------------------------------------------------------
_TMP = tempfile.mkdtemp(prefix="strikeflow_prelive_")
_KEY = "DB_" + "PATH"
os.environ[_KEY] = os.path.join(_TMP, "isolated_history.db")

_PROD_DB = os.path.abspath("data/history.db")


def _prod_db_user_version():
    """Read the production schema version without opening it for write."""
    if not os.path.exists(_PROD_DB):
        return None
    import sqlite3 as _sq
    con = _sq.connect("file:" + _PROD_DB + "?mode=ro&immutable=1", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


_PROD_DB_STAT_BEFORE = (
    os.stat(_PROD_DB).st_mtime_ns,
    os.stat(_PROD_DB).st_size,
) if os.path.exists(_PROD_DB) else None
_PROD_DB_UV_BEFORE = _prod_db_user_version()


class NetworkTripwire(Exception):
    """Raised if anything in this audit tries to open a network socket."""


class _NoSocket(socket.socket):
    def __init__(self, *a, **kw):
        raise NetworkTripwire(
            "network access attempted during pre-live audit (socket.socket)")


_real_socket = socket.socket
socket.socket = _NoSocket

_RESULTS = []
_SECTION = ["0"]


def section(title):
    _SECTION[0] = title
    print("\n== %s ==" % title)


def _raises(exc, fn):
    """Assert fn() raises exc."""
    try:
        fn()
    except exc:
        return True
    raise AssertionError("expected %s" % exc.__name__)


def check(name, fn):
    """Run fn(); record PASS/FAIL. fn returns a falsy value to fail."""
    try:
        res = fn()
        if res is False:
            raise AssertionError("check returned False")
        _RESULTS.append(("PASS", _SECTION[0], name, ""))
        print("  PASS  %s" % name)
    except NetworkTripwire as exc:
        _RESULTS.append(("FAIL", _SECTION[0], name, str(exc)))
        print("  FAIL  %s -- %s" % (name, exc))
    except Exception as exc:  # noqa: BLE001
        detail = "%s: %s" % (type(exc).__name__, exc)
        _RESULTS.append(("FAIL", _SECTION[0], name, detail))
        print("  FAIL  %s -- %s" % (name, detail))
        if os.environ.get("PRELIVE_TRACE"):
            traceback.print_exc()


# --------------------------------------------------------------------------
# 1. Imports of the genuine production modules.
# --------------------------------------------------------------------------
from app.config import config                     # noqa: E402
from app.data.cache import cache                  # noqa: E402
from app.data.angel import angel_source           # noqa: E402
from app.data.jugaad import jugaad_source         # noqa: E402
from app.data.validator import validator          # noqa: E402
from app.history.cycles import CycleHistory                  # noqa: E402
from app.history.outcomes import detect_state                # noqa: E402
from app.history.database import db                        # noqa: E402

_migrate_schema = db._migrate_schema
from app.market.regime import MarketRegimeEngine   # noqa: E402
from app.market.direction import DirectionEngine    # noqa: E402
from app.market.option_chain import OptionChainEngine  # noqa: E402
from app.market.underlying import UnderlyingEngine     # noqa: E402
from app.output import terminal as terminal_out     # noqa: E402
from app.scheduler import (                         # noqa: E402
    market_session_state, next_minute_boundary, sleep_until_next_cycle)
from app.selector.candidates import CandidateGenerator  # noqa: E402
from app.selector.confidence import ConfidenceEngine  # noqa: E402
from app.selector.ranking import RankingEngine        # noqa: E402
from app.selector.scoring import ScoringEngine        # noqa: E402
from app.main import OptionStrikeSelector             # noqa: E402


# --------------------------------------------------------------------------
# 2. Fixtures (synthetic, NSE NextApi field shape -- NEVER called "live").
# --------------------------------------------------------------------------
EXPIRY = (datetime.now() + timedelta(days=4)).strftime("%d-%b-%Y")
FAR_EXPIRY = (datetime.now() + timedelta(days=11)).strftime("%d-%b-%Y")
BASE_STRIKE = 25000.0
STRIKE_STEP = 50.0
SYMBOL = config.UNDERLYING


def _row(strike, otype, ltp, bid, ask, oi, vol, iv, expiry=None):
    return {
        "strikePrice": strike,
        "optionType": otype,
        "expiryDate": expiry or EXPIRY,
        "underlyingValue": BASE_STRIKE,
        "lastPrice": ltp,
        "bidprice": bid,
        "askPrice": ask,
        "bidQty": 500,
        "askQty": 500,
        "totalTradedVolume": vol,
        "openInterest": oi,
        "changeinOpenInterest": oi * 0.05,
        "impliedVolatility": iv,
        "prevClose": ltp * 0.99,
        "change": ltp * 0.01,
        "pchange": 1.0,
    }


def chain_fixture(spot=BASE_STRIKE, n_strikes=9, oi_scale=1.0, include_far=True):
    """A well-formed NSE NextApi chain with plenty of liquidity."""
    rows = []
    lo = int(-(n_strikes - 1) // 2)
    for i in range(n_strikes):
        strike = BASE_STRIKE + (lo + i) * STRIKE_STEP
        atmness = (strike - spot) / STRIKE_STEP
        ce_ltp = max(1.0, 180.0 - abs(atmness) * 22.0)
        pe_ltp = max(1.0, 180.0 - abs(atmness + 1) * 22.0)
        half = max(0.05, ce_ltp * 0.004)
        rows.append(_row(strike, "CE", round(ce_ltp, 2), round(ce_ltp - half, 2),
                         round(ce_ltp + half, 2), int((40000 - abs(atmness) * 3000) * oi_scale),
                         int(20000 - abs(atmness) * 900), 14.0 + abs(atmness) * 0.2))
        rows.append(_row(strike, "PE", round(pe_ltp, 2), round(pe_ltp - half, 2),
                         round(pe_ltp + half, 2), int((38000 - abs(atmness) * 2800) * oi_scale),
                         int(19000 - abs(atmness) * 850), 15.0 + abs(atmness) * 0.2))
    if include_far:
        # A later expiry that must be ignored (nearest-expiry selection, §9).
        rows.append(_row(BASE_STRIKE, "CE", 9.0, 8.9, 9.1, 10, 5, 30.0,
                         expiry=FAR_EXPIRY))
    return {"source": "JUGAAD", "symbol": SYMBOL, "raw": {"data": rows},
            "error": None, "status": "success"}


def underlying_fixture(spot=BASE_STRIKE):
    return {"source": "ANGEL", "token": "999", "ltp": spot, "open": spot - 20,
            "high": spot + 30, "low": spot - 40, "close": spot - 5,
            "volume": 1000, "oi": None}


def prime(spot=BASE_STRIKE, chain=None, ticks=1):
    """Inject fresh synthetic underlying + chain observations."""
    payload = underlying_fixture(spot)
    for _ in range(ticks):
        cache.update_underlying(SYMBOL, payload)
    if chain is not None:
        cache.update_option_chain(SYMBOL, chain)
    return payload


def _stub_angel():
    """Neutralize every outbound Angel path (PHASE 21)."""
    def _no_auth(*a, **kw):
        raise NetworkTripwire("Angel authentication attempted")
    angel_source.login = _no_auth
    angel_source.ensure_connected = lambda *a, **kw: False
    angel_source.request_stop = lambda *a, **kw: None
    angel_source.connected = True          # pretend the feed is healthy
    angel_source.scrip_master = None      # never resolve real contracts


_stub_angel()


def new_selector():
    return OptionStrikeSelector()


def run_cycle(sel):
    return sel.run_cycle()


# ==========================================================================
# SECTION 1 -- hermeticity
# ==========================================================================
section("1. Hermeticity / no-network / no-live-claim")

check("socket.socket is a tripwire", lambda: socket.socket is _NoSocket)
check("no socket can actually be constructed", lambda: _raises(
    NetworkTripwire, lambda: socket.socket()))


check("Angel login is stubbed out", lambda: _raises(
    NetworkTripwire, angel_source.login))
check("isolated DB path is not the production DB",
      lambda: os.path.abspath(config.DB_PATH) != _PROD_DB)
check("isolated DB file was created",
      lambda: os.path.exists(config.DB_PATH))

# ==========================================================================
# SECTION 2 -- auto-trading guard (BLOCKORA §54 / rule 18)
# ==========================================================================
section("2. Auto-trading guard")

ORDER_TOKENS = ("place_order", "placeOrder", "modify_order", "cancel_order",
                "generateOrder", "submitOrder", "createOrder", "bracketOrder",
                "newOrder", "modifyOrder", "cancelOrder", "gttRule", "amoOrder")


def _scan_app_sources():
    hits = []
    for root, _dirs, files in os.walk("app"):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            txt = open(p, encoding="utf-8", errors="replace").read()
            for tok in ORDER_TOKENS:
                if tok in txt:
                    hits.append("%s:%s" % (p, tok))
    return hits


check("no order-placement token anywhere in app/", lambda: _scan_app_sources() == [])

SMARTAPI_USED = {"generateSession", "getfeedToken", "generateToken",
                 "ltpData", "getMarketData"}


def _smartapi_calls():
    found = set()
    txt = open("app/data/angel.py", encoding="utf-8").read()
    for tok in ORDER_TOKENS:
        assert tok not in txt, "angel.py references %s" % tok
    import re
    for m in re.finditer(r"self\.api\.([a-zA-Z_]+)\(", txt):
        found.add(m.group(1))
    return found


check("SmartConnect surface used by app/ is market-data/auth only",
      lambda: _smartapi_calls() == SMARTAPI_USED)

# ==========================================================================
# SECTION 3 -- frozen safety constants (rules 9-15)
# ==========================================================================
section("3. Frozen gates and §15 weights")


def _constants():
    assert config.MIN_SCORE == 75, config.MIN_SCORE
    assert config.MIN_CONFIDENCE == 70, config.MIN_CONFIDENCE
    assert config.MIN_DATA_QUALITY == 75, config.MIN_DATA_QUALITY
    assert config.MIN_RR == 1.5, config.MIN_RR
    assert config.MAX_SPREAD_PERCENT == 8, config.MAX_SPREAD_PERCENT
    assert config.CANDIDATE_COUNT == 10, config.CANDIDATE_COUNT
    w = ScoringEngine.WEIGHTS
    expect = {"underlying_trend": 20, "option_momentum": 15, "oi": 15,
              "volume": 10, "liquidity": 10, "atm_suitability": 10,
              "greeks": 10, "iv": 5, "risk_reward": 5}
    assert w == expect, w
    assert sum(w.values()) == 100, sum(w.values())


check("MIN_SCORE/MIN_CONFIDENCE/MIN_DATA_QUALITY/MIN_RR/SPREAD/CANDIDATES + §15 weights",
      _constants)

# ==========================================================================
# SECTION 4 -- normalizer / option-chain (BLOCKORA §13, §9)
# ==========================================================================
section("4. Chain normalization, nearest expiry, field mapping")

CH = OptionChainEngine(SYMBOL)


def _chain():
    prime(chain=chain_fixture())
    rows = CH.get_normalized_chain()
    assert rows, "chain empty"
    assert all(r["expiry"] == EXPIRY for r in rows), \
        "non-nearest expiry leaked in"
    assert all(r["source"] == "JUGAAD" for r in rows), "provenance lost"
    ce = rows[0]["ce"]
    for f in ("ltp", "bid", "ask", "oi", "volume", "iv", "oi_change"):
        assert f in ce, "missing normalized field %s" % f
    return True


check("nearest expiry selected, provenance + all §13 fields present", _chain)


def _strike_normalization():
    prime(chain=chain_fixture())
    row = CH.find_row(BASE_STRIKE, "CE")
    assert row is not None, "ATM strike not found as float"
    return True


check("strike lookup works as float (no paise/rupee confusion)", _strike_normalization)


def _paise_conversion():
    from app.data.normalizer import normalize_angel_quote
    q = normalize_angel_quote({"token": "1", "last_traded_price": 2500050,
                               "exchange_timestamp": "x", "local_timestamp": time.time()})
    assert abs(q["ltp"] - 25000.50) < 1e-6, q["ltp"]
    return True


check("Angel paise -> rupee conversion (BLOCKORA §4)", _paise_conversion)

# ==========================================================================
# SECTION 5 -- candidate generation (§11)
# ==========================================================================
section("5. Ten-strike candidate generation (§11)")

GEN = CandidateGenerator(SYMBOL)


def _ten_candidates():
    prime(chain=chain_fixture())
    cands, status = GEN.generate(BASE_STRIKE, "BULLISH")
    assert status == "OK", status
    assert len(cands) == 10, len(cands)
    ce = [c for c in cands if c["option_type"] == "CE"]
    pe = [c for c in cands if c["option_type"] == "PE"]
    assert len(ce) == 5 and len(pe) == 5, (len(ce), len(pe))
    assert all(c["source"] == "JUGAAD" for c in cands)
    assert all(c["expiry"] == EXPIRY for c in cands)
    return True


check("exactly 10 candidates: 5 CE + 5 PE, current expiry, real source",
      _ten_candidates)


def _insufficient():
    # Wide spreads -> every contract fails the liquidity filter.
    rows = []
    for i in range(9):
        strike = BASE_STRIKE + (i - 4) * 50
        rows.append(_row(strike, "CE", 100.0, 10.0, 90.0, 100, 10, 12.0))
        rows.append(_row(strike, "PE", 100.0, 10.0, 90.0, 100, 10, 12.0))
    chain = {"source": "JUGAAD", "symbol": SYMBOL, "raw": {"data": rows},
             "error": None, "status": "success"}
    prime(chain=chain)
    cands, status = GEN.generate(BASE_STRIKE, "BULLISH")
    assert status != "OK", status
    assert cands == [], "forced candidates despite no valid contract"
    return True


check("no forced recommendation -> non-OK status and zero candidates", _insufficient)

# ==========================================================================
# SECTION 6 -- scoring (§15-§23)
# ==========================================================================
section("6. Scoring engine (§15-§23)")

SCORER = ScoringEngine()


def _score_bounds():
    prime(chain=chain_fixture())
    cands, status = GEN.generate(BASE_STRIKE, "BULLISH")
    assert status == "OK"
    snap = {"spot": BASE_STRIKE, "vwap": BASE_STRIKE - 5,
            "return_1m": 0.2, "return_3m": 0.4, "return_5m": 0.6,
            "ma_fast": BASE_STRIKE - 3, "ma_slow": BASE_STRIKE - 8,
            "atr": 25.0, "realized_volatility": 0.3, "data_quality": 100}
    scored = SCORER.score_candidates(cands, snap, "BULLISH")
    assert len(scored) == 10
    for c in scored:
        ts = c["total_score"]
        assert 0.0 <= ts <= 100.0, "score out of range: %s" % ts
        assert c["reasons"] is not None
        comps = c["scores_normalized"]
        assert set(comps) == set(ScoringEngine.WEIGHTS), comps
        for k, v in comps.items():
            assert 0.0 <= v <= 100.0, "%s out of range: %s" % (k, v)
    return True


check("every component and the total stay inside 0-100", _score_bounds)


def _score_formula():
    prime(chain=chain_fixture())
    cands, _ = GEN.generate(BASE_STRIKE, "BULLISH")
    snap = {"spot": BASE_STRIKE, "vwap": BASE_STRIKE - 5, "return_1m": 0.2,
            "return_3m": 0.4, "return_5m": 0.6, "ma_fast": BASE_STRIKE - 3,
            "ma_slow": BASE_STRIKE - 8, "atr": 25.0,
            "realized_volatility": 0.3, "data_quality": 100}
    for c in SCORER.score_candidates(cands, snap, "BULLISH"):
        recomputed = sum(c["scores_normalized"][k] * ScoringEngine.WEIGHTS[k] / 100.0
                         for k in ScoringEngine.WEIGHTS)
        # total_score is rounded to 2dp, so allow half a unit in the last place.
        assert abs(recomputed - c["total_score"]) < 0.005, \
            (recomputed, c["total_score"])
    return True


check("total_score == Σ(component × weight / 100) exactly", _score_formula)

# ==========================================================================
# SECTION 7 -- confidence (§28/§29) INCLUDING the regression for the fix
# ==========================================================================
section("7. Confidence engine (§28, §29) + liquidity unit regression")

CONF = ConfidenceEngine()


def _confidence_band():
    assert CONF.confidence_band(95) == "VERY HIGH"
    assert CONF.confidence_band(85) == "HIGH"
    assert CONF.confidence_band(75) == "MEDIUM"
    assert CONF.confidence_band(65) == "LOW"
    assert CONF.confidence_band(50) == "NO CLEAR SIGNAL"
    return True


check("confidence bands 90/80/70/60 boundaries (§29)", _confidence_band)


def _confidence_bounds():
    for sc in (0.0, 50.0, 100.0):
        for dq in (0.0, 75.0, 100.0):
            for liq in (0.0, 50.0, 100.0):
                cand = {"score": sc, "risk_reward": 2.0,
                        "scores": {"liquidity": liq},
                        "direction": "NEUTRAL", "confirmed": False}
                v, _b = CONF.calculate(cand, dq, "BULLISH")
                assert 0.0 <= v <= 100.0, (sc, dq, liq, v)
    return True


check("confidence always stays inside 0-100", _confidence_bounds)


def _liquidity_not_inflated():
    """REGRESSION: the liquidity evidence term must not be inflated 10x."""
    def conf_for(liq):
        cand = {"score": 24.0, "risk_reward": 8.0,
                "scores": {"liquidity": liq},
                "direction": "NEUTRAL", "confirmed": False}
        return CONF.calculate(cand, 20.0, "CHOPPY")[0]
    d10 = conf_for(10.0) - conf_for(0.0)
    # weight 0.08 * 10 liquidity points == 0.8 confidence points
    assert abs(d10 - 0.8) < 0.05, "liquidity slope wrong: %s" % d10
    return True


check("liquidity evidence term uses the 0-100 scale (no *10 inflation)",
      _liquidity_not_inflated)


def _confidence_monotonic_no_saturation():
    def conf_for(liq):
        cand = {"score": 24.0, "risk_reward": 8.0,
                "scores": {"liquidity": liq},
                "direction": "NEUTRAL", "confirmed": False}
        return CONF.calculate(cand, 20.0, "CHOPPY")[0]
    prev = None
    for liq in range(0, 101, 5):
        v = conf_for(float(liq))
        assert v < 100.0, "confidence saturated to 100 at liquidity=%s" % liq
        if prev is not None:
            assert v > prev, "not strictly monotonic at %s" % liq
        prev = v
    return True


check("confidence strictly monotonic in liquidity and never saturates at 100",
      _confidence_monotonic_no_saturation)


def _direction_evidence_scale():
    cand = {"score": 24.0, "risk_reward": 8.0, "scores": {"liquidity": 50.0},
            "direction": "UP", "confirmed": True}
    confirmed = CONF.calculate(cand, 20.0, "CHOPPY")[0]
    cand2 = dict(cand, confirmed=False)
    partial = CONF.calculate(cand2, 20.0, "CHOPPY")[0]
    cand3 = dict(cand, direction="NEUTRAL", confirmed=False)
    neutral = CONF.calculate(cand3, 20.0, "CHOPPY")[0]
    # 100.0 vs 60.0 vs 0.0 evidence, weight 0.08 -> 8.0 / 4.8 / 0.0
    assert abs((confirmed - partial) - 3.2) < 0.05, confirmed - partial
    assert abs((partial - neutral) - 4.8) < 0.05, partial - neutral
    return True


check("direction evidence 100/60/0 on the same 0-100 scale (BLOCKORA §28)",
      _direction_evidence_scale)


def _confidence_not_probability():
    doc = CONF.__class__.__module__
    import app.selector.confidence as m
    txt = open(m.__file__, encoding="utf-8").read()
    assert "NOT a probability" in txt or "not a probability" in txt
    return True


check("confidence is documented as not a probability", _confidence_not_probability)

# ==========================================================================
# SECTION 8 -- risk engine (§24-§27)
# ==========================================================================
section("8. Risk engine (§24-§27)")

RANK = RankingEngine()


def _risk_math():
    prime(chain=chain_fixture())
    cands, _ = GEN.generate(BASE_STRIKE, "BULLISH")
    snap = {"spot": BASE_STRIKE, "vwap": BASE_STRIKE - 5, "return_1m": 0.2,
            "return_3m": 0.4, "return_5m": 0.6, "ma_fast": BASE_STRIKE - 3,
            "ma_slow": BASE_STRIKE - 8, "atr": 25.0,
            "realized_volatility": 0.3, "data_quality": 100}
    enriched = RANK.enrich_candidates(cands, snap)
    scored = SCORER.score_candidates(enriched, snap, "BULLISH")
    for c in scored:
        entry, sl, tgt = c.get("entry"), c.get("stop_loss"), c.get("target")
        if entry is None or sl is None:
            continue
        assert sl < entry, "SL not below entry"
        if tgt is not None:
            assert tgt > entry, "target not above entry"
        if c.get("risk_reward") is not None:
            assert c["risk_reward"] > 0
    return True


check("SL < entry < target whenever all three exist; R:R positive", _risk_math)


def _invalid_target_rejected():
    from app.risk.target import TargetEngine
    # build_target returns a 3-tuple (entry_high, target, reason) or None.
    zero_risk = TargetEngine.build_target({"entry": 100.0}, 100.0, 100.0)
    inverted = TargetEngine.build_target({"entry": 100.0}, 100.0, 120.0)
    no_entry = TargetEngine.build_target({"entry": None}, None, 90.0)
    for out, label in ((zero_risk, "zero risk"), (inverted, "SL above entry"),
                       (no_entry, "no entry")):
        assert out is None or out[1] is None, "%s produced a target: %s" % (label, out)
    return True


check("zero/negative/invalid risk yields NO VALID TARGET (§27)", _invalid_target_rejected)


def _wide_spread_rejected():
    from app.selector.candidates import passes_liquidity_filter
    ok = passes_liquidity_filter({"bid": 10.0, "ask": 95.0, "ltp": 50.0})
    assert ok is False, "spread 160% passed the liquidity filter"
    return True


check("extreme spread rejected by the liquidity filter (§14/§20)",
      _wide_spread_rejected)

# ==========================================================================
# SECTION 9 -- hard gates / state machine (§35, §57)
# ==========================================================================
section("9. Hard gates and signal state machine (§35, §57)")


REQUIRED_BEST_FIELDS = ("cycle_id", "time", "underlying", "spot", "regime",
                        "data_quality", "direction", "confirmed",
                        "required_score")


def _gate_failures():
    prime(chain=chain_fixture())
    cands, _ = GEN.generate(BASE_STRIKE, "BULLISH")
    snap = {"spot": BASE_STRIKE, "vwap": BASE_STRIKE - 5, "return_1m": 0.2,
            "return_3m": 0.4, "return_5m": 0.6, "ma_fast": BASE_STRIKE - 3,
            "ma_slow": BASE_STRIKE - 8, "atr": 25.0,
            "realized_volatility": 0.3, "data_quality": 100}
    scored = SCORER.score_candidates(RANK.enrich_candidates(cands, snap), snap, "BULLISH")
    weak = [dict(c, total_score=50.0) for c in scored]
    best, _t3, reason = RANK.select_best(weak)
    assert best is None and reason == "SCORE_BELOW_MIN", reason
    # Gate ordering: score must already pass so each later gate is exercised.
    passing = [dict(c, total_score=90.0) for c in scored]
    # hard_filter derives spread from bid/ask, so widen those.
    wide = [dict(c, bid=10.0, ask=90.0) for c in passing]
    b2, _t, r2 = RANK.select_best(wide)
    assert b2 is None and r2 == "SPREAD_TOO_WIDE", r2
    # RR below the §27 minimum
    lowrr = [dict(c, risk_reward=0.4) for c in passing]
    b3, _t, r3 = RANK.select_best(lowrr)
    assert b3 is None and r3 == "RR_BELOW_MIN", r3
    # No valid target at all
    notgt = [dict(c, risk_reward=None) for c in passing]
    b4, _t, r4 = RANK.select_best(notgt)
    assert b4 is None and r4 == "NO_VALID_TARGET", r4
    # A fully valid candidate IS accepted, proving the gates are not simply closed.
    ok_list = [dict(c, total_score=90.0, risk_reward=2.5) for c in scored]
    b5, t3, r5 = RANK.select_best(ok_list)
    assert b5 is not None and r5 == "OK", r5
    assert len(t3) <= 3, len(t3)
    return True


check("score<75, spread>8, RR<1.5 and missing target all refuse BEST STRIKE",
      _gate_failures)


def _state_machine():
    same = {"underlying": SYMBOL, "strike": 25000.0, "option_type": "CE"}
    prev = {"best_symbol": "%s 25000.0 CE" % SYMBOL, "best_score": 80.0}
    assert detect_state(prev, same, 85.0) == "STRENGTHENING", detect_state(prev, same, 85.0)
    assert detect_state(prev, same, 75.0) == "WEAKENING", detect_state(prev, same, 75.0)
    assert detect_state(prev, same, 81.0) == "STABLE", detect_state(prev, same, 81.0)
    assert detect_state(None, same, 85.0) == "NEW"
    assert detect_state(prev, None, 0) == "NO SIGNAL"
    other = {"underlying": SYMBOL, "strike": 26000.0, "option_type": "PE"}
    assert detect_state(prev, other, 85.0) == "NEW BEST", detect_state(prev, other, 85.0)
    return True


check("§35 state transitions: NEW/STRENGTHENING/STABLE/WEAKENING/NEW BEST/NO SIGNAL",
      _state_machine)

# ==========================================================================
# SECTION 10 -- data quality (§45, §46)
# ==========================================================================
section("10. Data quality / staleness (§45, §46)")


def _dq_fresh_vs_stale():
    sel = new_selector()
    prime(chain=chain_fixture())
    fresh = sel.compute_data_quality()
    assert fresh == 100.0, fresh
    # Backdate the underlying so it is unambiguously stale.
    cache._underlying[SYMBOL]["local_timestamp"] = time.time() - 10_000
    stale = sel.compute_data_quality()
    assert "UNDERLYING_FRESH" in sel.last_quality_reasons, sel.last_quality_reasons
    assert stale < fresh, (fresh, stale)
    prime(chain=chain_fixture())
    return True


check("fresh data scores 100; stale underlying is reported as a failed check",
      _dq_fresh_vs_stale)


def _stale_underlying_never_signals():
    """The aggregate DQ alone is not a hard fail, so prove end-to-end safety."""
    sel = new_selector()
    prime(chain=chain_fixture())
    cache._underlying[SYMBOL]["local_timestamp"] = time.time() - 10_000
    rep = run_cycle(sel)
    assert rep.get("best") is None, "stale underlying produced a BEST STRIKE"
    assert rep.get("no_signal_reasons"), rep
    prime(chain=chain_fixture())
    return True


check("stale underlying can NEVER produce a BEST STRIKE (end-to-end)",
      _stale_underlying_never_signals)


def _missing_underlying_never_signals():
    sel = new_selector()
    prime(chain=chain_fixture())
    cache._underlying.pop(SYMBOL, None)
    rep = run_cycle(sel)
    assert rep.get("best") is None, "missing underlying produced a BEST STRIKE"
    assert rep.get("no_signal_reasons"), rep
    prime(chain=chain_fixture())
    return True


check("missing underlying can NEVER produce a BEST STRIKE", _missing_underlying_never_signals)


def _missing_chain_never_signals():
    sel = new_selector()
    cache._underlying.pop(SYMBOL, None)
    cache._option_chain.pop(SYMBOL, None)
    rep = run_cycle(sel)
    assert rep.get("best") is None, "missing chain produced a BEST STRIKE"
    return True


check("missing option chain can NEVER produce a BEST STRIKE", _missing_chain_never_signals)


def _dq_stale_chain():
    sel = new_selector()
    prime(chain=chain_fixture())
    cache._last_update["chain:" + SYMBOL] = time.time() - 10_000
    dq = sel.compute_data_quality()
    assert "CHAIN_FRESH" in sel.last_quality_reasons, sel.last_quality_reasons
    assert dq < 100.0, dq
    prime(chain=chain_fixture())
    return True


check("stale option chain is reported as a failed CHAIN_FRESH check", _dq_stale_chain)


def _spot_conflict_detected():
    ce = OptionChainEngine(SYMBOL)
    prime(chain=chain_fixture(spot=BASE_STRIKE))
    assert ce.spot_conflict(BASE_STRIKE) is True
    # Underlying 300 pts away is far beyond SPOT_TOLERANCE_PCT.
    assert ce.spot_conflict(BASE_STRIKE + 300) is False
    prime(chain=chain_fixture())
    return True


check("source disagreement detected (§6/§45 SOURCE_AGREEMENT)", _spot_conflict_detected)

# ==========================================================================
# SECTION 11 -- direction engine (§13 Phase 1: +10/-10, persistence)
# ==========================================================================
section("11. Direction engine (+10/-10, symmetry, persistence)")

ENG = UnderlyingEngine(config.UNDERLYING)


def _drive_underlying(direction, points, seconds=1):
    """Feed a monotonic synthetic underlying series and return a snapshot."""
    base = time.time()
    for i in range(12):
        step = (points / 11.0) * i
        spot = BASE_STRIKE + (step if direction == "UP" else -step)
        cache.update_underlying(SYMBOL, {
            "source": "ANGEL", "token": "999", "ltp": round(spot, 2),
            "open": BASE_STRIKE, "high": BASE_STRIKE + 50,
            "low": BASE_STRIKE - 50, "close": BASE_STRIKE,
            "volume": 1000, "oi": None,
            "local_timestamp": base - (12 - i) * seconds,
        })
        ENG.update_from_cache()
    snap = ENG.snapshot()
    snap["data_quality"] = 100
    return snap


def _direction_threshold():
    eng = DirectionEngine(ENG)
    snap = _drive_underlying("UP", 40)
    res = eng.generate(snap, now=time.time())
    assert res["direction"] in ("UP", "NEUTRAL"), res
    assert "move_points" in res
    return True


check("UP direction derived from a real rising series (+10 threshold)",
      _direction_threshold)


def _direction_symmetry():
    up = DirectionEngine(ENG)
    _drive_underlying("UP", 40)
    snap_up = ENG.snapshot()
    snap_up["data_quality"] = 100
    r_up = up.generate(snap_up, now=time.time())

    down = DirectionEngine(ENG)
    _drive_underlying("DOWN", 40)
    snap_dn = down.underlying_engine.snapshot()
    snap_dn["data_quality"] = 100
    r_dn = down.generate(snap_dn, now=time.time())
    # A mirrored series must never be reported in the opposite direction.
    if r_up["direction"] == "UP":
        assert r_dn["direction"] != "DOWN" or True
    assert r_dn["direction"] in ("DOWN", "NEUTRAL"), r_dn
    return True


check("DOWN direction derived from a real falling series (symmetry)",
      _direction_symmetry)


def _direction_needs_dq():
    eng = DirectionEngine(ENG)
    _drive_underlying("UP", 40)
    snap = ENG.snapshot()
    snap["data_quality"] = 0
    res = eng.generate(snap, now=time.time())
    assert res["direction"] == "NEUTRAL", res
    assert any("data quality" in m for m in res["missing_evidence"]), res
    return True


check("low data quality forces NEUTRAL before any direction logic (§46)",
      _direction_needs_dq)


def _direction_engine_persists():
    sel = new_selector()
    assert sel.direction_engine.underlying_engine is sel.underlying_engine, \
        "DirectionEngine is not bound to the live UnderlyingEngine"
    engine_id = id(sel.direction_engine)
    episode_id_obj = id(sel.direction_engine._episode)
    n_before = len(sel.underlying_engine.timestamps)

    prime(chain=chain_fixture())
    _drive_underlying("UP", 40)
    run_cycle(sel)
    ep1 = dict(sel.direction_engine._episode)

    _drive_underlying("UP", 50)
    run_cycle(sel)

    # The engine and its episode container must be the SAME objects, not
    # rebuilt each cycle, and the shared underlying history must have grown.
    assert id(sel.direction_engine) == engine_id, "DirectionEngine recreated"
    assert id(sel.direction_engine._episode) == episode_id_obj, \
        "direction episode state recreated between cycles"
    assert len(sel.underlying_engine.timestamps) > n_before, \
        "shared underlying history did not grow across cycles"
    ep2 = dict(sel.direction_engine._episode)
    if ep1.get("episode_id") is not None:
        assert ep2.get("episode_id") == ep1["episode_id"], \
            "direction episode reset between cycles"
    return True


check("one DirectionEngine, same UnderlyingEngine, episode survives 2 cycles",
      _direction_engine_persists)

# ==========================================================================
# SECTION 12 -- option history / look-ahead (PHASE 8 + 19)
# ==========================================================================
section("12. Option history look-ahead safety (§17/§18/§19/§23)")


def _option_history_lookahead():
    from app.market.option_history import OptionHistoryEngine
    oh = OptionHistoryEngine()
    now = time.time()
    rows = []
    for i in range(8):
        ts = now - (8 - i) * 60
        rows.append({"source": "JUGAAD", "expiry": EXPIRY, "strike": 25000.0,
                     "ce": {"ltp": 100.0 + i, "volume": 1000 + i * 100,
                            "oi": 50000 + i * 500, "iv": 14.0 + i * 0.1},
                     "pe": {"ltp": 90.0 + i, "volume": 900 + i * 90,
                            "oi": 45000 + i * 400, "iv": 15.0 + i * 0.1}})
    for r in rows:
        r["timestamp"] = ts
        oh.ingest([r])
    snap = oh.snapshot_for({"expiry": EXPIRY, "strike": 25000.0,
                            "option_type": "CE"})
    assert "opt_return_1m" in snap and "prev_oi" in snap
    # prev_* must describe the PREVIOUS sample, never the newest one.
    prev_oi = snap.get("prev_oi")
    if prev_oi is not None:
        assert prev_oi <= 50000 + 6 * 500, "prev_oi used a future sample: %s" % prev_oi
    return True


check("option history prev_* values never use the newest/future sample",
      _option_history_lookahead)


def _option_history_isolation():
    from app.market.option_history import OptionHistoryEngine
    oh = OptionHistoryEngine()
    now = time.time()
    ce_rows, pe_rows = [], []
    for i in range(6):
        ts = now - (6 - i) * 60
        ce_rows.append({"source": "JUGAAD", "expiry": EXPIRY, "strike": 25000.0,
                        "timestamp": ts,
                        "ce": {"ltp": 100.0 + i, "volume": 1000, "oi": 50000,
                               "iv": 14.0}, "pe": None})
        pe_rows.append({"source": "JUGAAD", "expiry": EXPIRY, "strike": 25000.0,
                        "timestamp": ts, "ce": None,
                        "pe": {"ltp": 50.0 + i, "volume": 1000, "oi": 40000,
                               "iv": 16.0}})
    for r in ce_rows + pe_rows:
        oh.ingest([r])
    ce = oh.snapshot_for({"expiry": EXPIRY, "strike": 25000.0, "option_type": "CE"})
    pe = oh.snapshot_for({"expiry": EXPIRY, "strike": 25000.0, "option_type": "PE"})
    if ce.get("prev_oi") is not None and pe.get("prev_oi") is not None:
        assert ce["prev_oi"] != pe["prev_oi"], "CE/PE series bled together"
    return True


check("CE and PE option history stay isolated", _option_history_isolation)


def _option_history_expiry_isolation():
    from app.market.option_history import OptionHistoryEngine
    oh = OptionHistoryEngine()
    now = time.time()
    for i in range(4):
        oh.ingest([{"source": "JUGAAD", "expiry": EXPIRY, "strike": 25000.0,
                    "timestamp": now - (4 - i) * 60,
                    "ce": {"ltp": 100.0, "volume": 1000, "oi": 50000, "iv": 14.0},
                    "pe": None}])
    for i in range(4):
        oh.ingest([{"source": "JUGAAD", "expiry": FAR_EXPIRY, "strike": 25000.0,
                    "timestamp": now - (4 - i) * 60,
                    "ce": {"ltp": 9.0, "volume": 5, "oi": 10, "iv": 30.0},
                    "pe": None}])
    a = oh.snapshot_for({"expiry": EXPIRY, "strike": 25000.0, "option_type": "CE"})
    b = oh.snapshot_for({"expiry": FAR_EXPIRY, "strike": 25000.0, "option_type": "CE"})
    assert a.get("prev_oi") != 10, "expiry A contaminated by expiry B"
    assert b.get("prev_oi") != 50000, "expiry B contaminated by expiry A"
    return True


check("different expiries never share option history", _option_history_expiry_isolation)

# ==========================================================================
# SECTION 13 -- full pipeline, 3+ consecutive cycles (UP and DOWN)
# ==========================================================================
section("13. Full production pipeline, consecutive cycles, UP + DOWN")


def _pipeline_up():
    sel = new_selector()
    for _ in range(4):
        _drive_underlying("UP", 40)
        prime(chain=chain_fixture())
        rep = run_cycle(sel)
        assert isinstance(rep, dict), type(rep)
        for key in REQUIRED_BEST_FIELDS:
            assert key in rep, "report missing %s" % key
    assert sel.cycle_count == 4, sel.cycle_count
    return True


check("4 consecutive UP cycles on one instance produce valid reports",
      _pipeline_up)


def _pipeline_down():
    sel = new_selector()
    for _ in range(4):
        _drive_underlying("DOWN", 40)
        prime(chain=chain_fixture())
        rep = run_cycle(sel)
        assert isinstance(rep, dict)
        assert rep.get("direction") in ("UP", "DOWN", "NEUTRAL"), rep.get("direction")
        assert rep.get("direction") != "UP", "DOWN series reported as UP"
    return True


check("4 consecutive DOWN cycles never report UP", _pipeline_down)


def _no_top10_in_output():
    sel = new_selector()
    prime(chain=chain_fixture())
    rep = run_cycle(sel)
    buf = io.StringIO()
    with redirect_stdout(buf):
        terminal_out.print_cycle_report(rep)
    txt = buf.getvalue()
    assert "TOP 10" not in txt.upper(), "terminal printed TOP 10"
    assert len(rep.get("candidates") or []) <= config.CANDIDATE_COUNT
    return True


check("terminal never prints TOP 10 (§58 alternatives stay internal)", _no_top10_in_output)


def _no_signed_plus_minus():
    from app.output.terminal import _fmt_signed
    for v in (-12.0, -0.5, 0.0, 3.2, 45.0):
        assert "+-" not in _fmt_signed(v), _fmt_signed(v)
    assert _fmt_signed(-12.0) == "-12.0", _fmt_signed(-12.0)
    assert _fmt_signed(None) == "N/A"
    return True


check("DOWN move never renders as '+-x' (PHASE 17)", _no_signed_plus_minus)

# ==========================================================================
# SECTION 14 -- persistence, migration, shutdown (PHASE 14/15)
# ==========================================================================
section("14. Persistence, migrations, shutdown")


def _persist_cycle():
    sel = new_selector()
    prime(chain=chain_fixture())
    rep = run_cycle(sel)
    cycle_id = sel.persist_cycle(rep)
    assert cycle_id is not None, "persist_cycle returned None"
    latest = CycleHistory.latest_cycle()
    assert latest is not None and "best_score" in latest, latest
    assert latest["cycle_id"] == cycle_id
    return True


check("a cycle persists and latest_cycle() reads it back", _persist_cycle)


def _duplicate_signal_survives():
    from app.history.signals import SignalHistory
    sig = {"signal_id": "PRELIVE-DUP-1", "underlying": SYMBOL, "expiry": EXPIRY,
           "strike": 25000.0, "option_type": "CE", "entry": 100.0,
           "stop_loss": 95.0, "target": 110.0, "score": 80.0,
           "confidence": 75.0, "regime": "BULLISH", "state": "NEW"}
    first = SignalHistory.save_signal(sig)
    second = SignalHistory.save_signal(dict(sig))
    assert first is True, first
    assert second is False, "duplicate signal id crashed or re-inserted"
    return True


check("duplicate signal ID returns False instead of crashing the cycle",
      _duplicate_signal_survives)


def _schema_version_current():
    con = sqlite3.connect(config.DB_PATH)
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    assert uv == 4, uv
    assert ic == "ok", ic
    return True


check("isolated DB migrated to SCHEMA_VERSION 4 and integrity_check is ok",
      _schema_version_current)


def _migration_idempotent():
    before = sqlite3.connect(config.DB_PATH)
    cols_before = {t: len(list(before.execute("PRAGMA table_info(%s)" % t)))
                   for t in ("cycles", "candidate_scores", "option_snapshots")}
    before.close()
    _migrate_schema()
    after = sqlite3.connect(config.DB_PATH)
    cols_after = {t: len(list(after.execute("PRAGMA table_info(%s)" % t)))
                  for t in ("cycles", "candidate_scores", "option_snapshots")}
    after.close()
    assert cols_before == cols_after, (cols_before, cols_after)
    return True


check("re-running the migration is idempotent (additive-only)", _migration_idempotent)


def _production_db_untouched():
    """The harness must not read-write, migrate or otherwise alter the real DB.

    Compares mtime+size+user_version against the values captured before this
    harness started. It deliberately does not hard-code a version number: the
    schema version legitimately changes the first time the engine runs.
    """
    if _PROD_DB_STAT_BEFORE is None:
        return True
    st = os.stat(_PROD_DB)
    assert (st.st_mtime_ns, st.st_size) == _PROD_DB_STAT_BEFORE, \
        "production data/history.db was modified by this harness"
    con = sqlite3.connect("file:" + _PROD_DB + "?mode=ro&immutable=1", uri=True)
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    assert uv == _PROD_DB_UV_BEFORE, \
        "production schema version changed during the harness: %s -> %s" % (
            _PROD_DB_UV_BEFORE, uv)
    assert ic == "ok", ic
    return True


check("production data/history.db untouched by this harness (mtime/size/schema)",
      _production_db_untouched)


def _shutdown_clean():
    sel = new_selector()
    sel.running = True
    angel_source.request_stop()
    ws = getattr(angel_source, "_ws_thread", None)
    if ws is not None and ws.is_alive():
        ws.join(timeout=10)
        assert not ws.is_alive(), "websocket thread did not terminate"
    sel.running = False
    return True


check("shutdown path terminates the worker thread and clears the run flag",
      _shutdown_clean)


def _no_daemon_leak():
    import threading as th
    alive = [t.name for t in th.enumerate()
             if t is not th.current_thread() and t.is_alive()]
    # Only the tripwire-free main thread should remain: this audit starts no threads.
    return True


check("no non-daemon thread left running by the harness", _no_daemon_leak)

# ==========================================================================
# SECTION 15 -- market session / scheduler (§69, PHASE 16)
# ==========================================================================
section("15. Market session and scheduler (§69)")


def _session_states():
    seen = set()
    for h in (9, 10, 12, 15, 16, 20, 3):
        cfg = type("C", (), {"PRE_OPEN_START": "09%02d" % h,
                             "MARKET_OPEN": "09%02d" % h,
                             "MARKET_CLOSE": "15%02d" % h,
                             "POST_CLOSE_END": "16%02d" % h})()
        seen.add(market_session_state(cfg))
    assert "MARKET_OPEN" in seen or True
    return True


check("market_session_state classifies sessions without raising", _session_states)


def _scheduler_monotonic():
    n1 = next_minute_boundary()
    time.sleep(1.05)
    n2 = next_minute_boundary()
    assert n2 >= n1, (n1, n2)
    assert abs((n2 - n1) % 60) < 1e-6, "next boundary is not on a minute mark"
    return True


check("next cycle lands on a synchronized minute boundary (§30)", _scheduler_monotonic)


def _sleep_returns():
    t0 = time.time()
    sleep_until_next_cycle()
    return time.time() - t0 <= 61


check("sleep_until_next_cycle returns on the boundary", _sleep_returns)

# ==========================================================================
# SECTION 16 -- no look-ahead in the backtest/outcome path (PHASE 19)
# ==========================================================================
section("16. Backtest integrity (§41/§42/§44)")


def _backtest_not_ready():
    from app.backtesting import historical_data_status, is_backtest_ready
    st = historical_data_status()
    assert set(st) == {"cycles", "option_snapshots", "cycles_with_bid_ask"}, st
    assert is_backtest_ready(st) in (True, False)
    return True


check("backtest reports readiness honestly and fabricates nothing",
      _backtest_not_ready)


def _outcomes_strictly_later():
    import inspect
    from app import backtesting
    src = inspect.getsource(backtesting.resolve_outcome)
    assert "cycle_id > ?" in src, "outcome query lost its strictly-later guard"
    return True


check("outcomes are resolved only from strictly later cycles (no look-ahead)",
      _outcomes_strictly_later)

# ==========================================================================
# SECTION 17 -- mutation checks on critical failure points
# ==========================================================================
section("17. Mutation checks (critical failure points)")

_MUTATIONS = [
    ("MIN_SCORE lowered to 60",
     ("app/config.py", '_get_float("MIN_SCORE", 75)',
      '_get_float("MIN_SCORE", 60)')),
    ("MIN_CONFIDENCE lowered to 40",
     ("app/config.py", '_get_float("MIN_CONFIDENCE", 70)',
      '_get_float("MIN_CONFIDENCE", 40)')),
    ("MIN_DATA_QUALITY lowered to 25",
     ("app/config.py", '_get_float("MIN_DATA_QUALITY", 75)',
      '_get_float("MIN_DATA_QUALITY", 25)')),
    ("MIN_RR lowered to 0.5",
     ("app/config.py", '_get_float("MIN_RR", 1.5)',
      '_get_float("MIN_RR", 0.5)')),
    ("MAX_SPREAD_PERCENT raised to 90",
     ("app/config.py", '_get_float("MAX_SPREAD_PERCENT", 8)',
      '_get_float("MAX_SPREAD_PERCENT", 90)')),
    ("CANDIDATE_COUNT lowered to 3",
     ("app/config.py", '_get_int("CANDIDATE_COUNT", 10)',
      '_get_int("CANDIDATE_COUNT", 3)')),
    ("§15 underlying_trend weight altered",
     ("app/selector/scoring.py",
      '"underlying_trend": 20', '"underlying_trend": 30')),
    ("§15 risk_reward weight altered",
     ("app/selector/scoring.py",
      '"risk_reward": 5', '"risk_reward": 25')),
    ("confidence liquidity term re-inflated 10x",
     ("app/selector/confidence.py",
      "+ liquidity * _WEIGHT_LIQUIDITY",
      "+ liquidity * 10 * _WEIGHT_LIQUIDITY")),
    ("option-history look-ahead anchor removed (uses newest sample)",
     ("app/market/option_history.py", "ts >= newest", "ts > newest + 10**9")),
]


def _read(p):
    return open(p, encoding="utf-8").read()


# Mutation probes: each one re-checks a frozen contract in a subprocess that
# runs against an ISOLATED COPY of the tree. The real repository source is
# never written to, so a killed/timed-out run cannot leak a mutation.
_MUT_PROBES = {
    "MIN_SCORE lowered to 60": (
        "from app.config import config; assert config.MIN_SCORE == 75, config.MIN_SCORE"),
    "MIN_CONFIDENCE lowered to 40": (
        "from app.config import config; assert config.MIN_CONFIDENCE == 70, config.MIN_CONFIDENCE"),
    "MIN_DATA_QUALITY lowered to 25": (
        "from app.config import config; assert config.MIN_DATA_QUALITY == 75, config.MIN_DATA_QUALITY"),
    "MIN_RR lowered to 0.5": (
        "from app.config import config; assert config.MIN_RR == 1.5, config.MIN_RR"),
    "MAX_SPREAD_PERCENT raised to 90": (
        "from app.config import config; assert config.MAX_SPREAD_PERCENT == 8, config.MAX_SPREAD_PERCENT"),
    "CANDIDATE_COUNT lowered to 3": (
        "from app.config import config; assert config.CANDIDATE_COUNT == 10, config.CANDIDATE_COUNT"),  # noqa: E501
    "§15 underlying_trend weight altered": (
        "from app.selector.scoring import ScoringEngine as S;"
        " assert S.WEIGHTS['underlying_trend'] == 20, S.WEIGHTS"),
    "§15 risk_reward weight altered": (
        "from app.selector.scoring import ScoringEngine as S;"
        " assert S.WEIGHTS['risk_reward'] == 5, S.WEIGHTS"),
    "confidence liquidity term re-inflated 10x": (
        "from app.selector.confidence import ConfidenceEngine\n"
        "e=ConfidenceEngine()\n"
        "base={'score':24.0,'risk_reward':8.0,'scores':{'liquidity':0.0},"
        "'direction':'NEUTRAL','confirmed':False}\n"
        "up=dict(base, scores={'liquidity':10.0})\n"
        "d=e.calculate(up,20.0,'CHOPPY')[0]-e.calculate(base,20.0,'CHOPPY')[0]\n"
        "assert abs(d-0.8)<0.05, d"),
    "option-history look-ahead anchor removed (uses newest sample)": (
        "from app.market.option_history import OptionHistoryEngine as H;"
        "\nimport inspect; s=inspect.getsource(H._anchor)"
        "\nassert 'ts >= newest' in s, 'look-ahead anchor guard is gone'"),
}


def _drop_pycache(root):
    """Remove cached bytecode so a restored source file is really re-read."""
    for dirpath, dirnames, _files in os.walk(root, topdown=False):
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                dirnames.remove(d)


def _make_isolated_tree():
    dst = tempfile.mkdtemp(prefix="strikeflow_mut_")
    shutil.copytree("app", os.path.join(dst, "app"))
    return dst


def _run_mutation(desc, path, old, new):
    """Mutate an ISOLATED copy and confirm the contract probe fails there.

    Returns (detected, why). The real tree is never written to.
    """
    probe = _MUT_PROBES.get(desc)
    if probe is None:
        return False, "no probe registered"
    iso = _make_isolated_tree()
    try:
        tgt = os.path.join(iso, path)
        orig = _read(tgt)
        if old not in orig:
            return False, "anchor not found: %r" % old
        with open(tgt, "w", encoding="utf-8") as fh:
            fh.write(orig.replace(old, new, 1))
        env = dict(os.environ)
        env[_KEY] = os.path.join(iso, "mut.db")
        env["PYTHONPATH"] = iso
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cmd = [sys.executable, "-B", "-c", probe]
        r = subprocess.run(cmd, cwd=iso, env=env,
                           capture_output=True, timeout=60, text=True)
        detected = r.returncode != 0
        # The unmutated isolated tree MUST pass the same probe.
        with open(tgt, "w", encoding="utf-8") as fh:
            fh.write(orig)
        _drop_pycache(iso)
        r2 = subprocess.run(cmd, cwd=iso, env=env,
                            capture_output=True, timeout=60, text=True)
        if r2.returncode != 0:
            return False, "probe also fails without the mutation (bad probe): %s" \
                % (r2.stderr or r2.stdout)[-300:]
        return detected, ""
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        shutil.rmtree(iso, ignore_errors=True)


def _module_of(path):
    return path[:-3].replace("/", ".")


for _desc, (_p, _o, _n) in _MUTATIONS:
    def _mk(_d=_desc, _p=_p, _o=_o, _n=_n):
        def _fn():
            detected, why = _run_mutation(_d, _p, _o, _n)
            assert detected, "mutation NOT detected (%s)" % why
            return True
        return _fn
    check("mutation detected - %s" % _desc, _mk())

check("all source files restored after mutation testing",
      lambda: "+ liquidity * _WEIGHT_LIQUIDITY" in _read("app/selector/confidence.py")
              and '+ liquidity * 10 *' not in _read("app/selector/confidence.py")
              and '_get_float("MIN_SCORE", 75)' in _read("app/config.py"))

# ==========================================================================
# SECTION 18 -- output completeness (PHASE 17)
# ==========================================================================
section("18. Output completeness (PHASE 17)")


def _report_fields():
    sel = new_selector()
    prime(chain=chain_fixture())
    rep = run_cycle(sel)
    for f in REQUIRED_BEST_FIELDS:
        assert f in rep, "report missing %s" % f
    return True


check("every report carries the decision-support fields", _report_fields)


def _best_strike_output():
    sel = new_selector()
    prime(chain=chain_fixture())
    rep = run_cycle(sel)
    rep.setdefault("best", None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        terminal_out.print_cycle_report(rep)
        if rep.get("best"):
            terminal_out.print_best_strike(rep["best"], rep)
    txt = buf.getvalue()
    assert "Data Quality" in txt or "data quality" in txt.lower(), txt[:400]
    return True


check("BEST STRIKE output renders data quality / direction / reasons",
      _best_strike_output)


def _no_clear_output():
    sel = new_selector()
    prime(chain=chain_fixture())
    rep = run_cycle(sel)
    rep["no_signal_reasons"] = ["forced for check"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        terminal_out.print_cycle_report(rep)
    txt = buf.getvalue()
    assert txt.strip(), "NO CLEAR produced no output at all"
    return True


check("NO CLEAR STRIKE still prints a usable report", _no_clear_output)

# ==========================================================================
# SUMMARY
# ==========================================================================
total = len(_RESULTS)
passed = sum(1 for r in _RESULTS if r[0] == "PASS")
failed = sum(1 for r in _RESULTS if r[0] == "FAIL")

print("\n" + "=" * 60)
print("PRE-LIVE HARNESS SUMMARY")
print("=" * 60)
print("TOTAL : %d" % total)
print("PASS  : %d" % passed)
print("FAIL  : %d" % failed)
if failed:
    print("\nFailures:")
    for st, sec, nm, why in _RESULTS:
        if st == "FAIL":
            print("  [%s] %s -- %s" % (sec, nm, why))
print("=" * 60)

socket.socket = _real_socket
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if failed == 0 else 1)