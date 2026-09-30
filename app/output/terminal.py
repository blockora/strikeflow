def _safe(val, default=None):
    if val is None:
        return default
    return val


def print_cycle_report(report):
    print("=" * 60)
    print("OPTION STRIKE SELECTOR")
    print("=" * 60)
    print(f"Cycle       : {report.get('cycle_id')}")
    print(f"Time        : {report.get('time')}")
    print(f"Underlying  : {report.get('underlying')}")
    print(f"Spot        : {report.get('spot')}")
    print(f"Expiry      : {report.get('expiry')}")
    print(f"Regime      : {report.get('regime')}")
    print(f"Data Quality: {report.get('data_quality')}")
    print("-" * 60)
    print("TOP 10 CANDIDATES")
    print("-" * 60)
    print("Rank  Strike       Type   Score")
    for c in report.get("candidates", []):
        print(f"{c.get('rank'):<5} {c.get('strike'):<12} {c.get('option_type'):<6} {c.get('total_score')}")

    best = _safe(report.get("best"))
    if not best:
        print("-" * 60)
        print("NO CLEAR STRIKE")
        print("-" * 60)
        print(f"Best Raw Score : {report.get('best_raw_score')}")
        print(f"Required Score : {report.get('required_score')}")
        print("Reason:")
        for r in report.get("no_signal_reasons", []):
            print(f"- {r}")
        print("=" * 60)
        return

    print("-" * 60)
    print("BEST STRIKE")
    print("-" * 60)
    underlying = _safe(best.get("underlying"))
    expiry = _safe(best.get("expiry"))
    strike = _safe(best.get("strike"))
    opt_type = _safe(best.get("option_type"))
    if underlying and expiry and strike and opt_type:
        print(f"Symbol      : {underlying} {expiry} {strike} {opt_type}")
    else:
        print("Symbol      : data unavailable")

    ltp = _safe(best.get("ltp"))
    print(f"LTP         : {ltp}" if ltp is not None else "LTP         : N/A")

    entry_low = _safe(best.get("entry_low"))
    entry_high = _safe(best.get("entry_high"))
    if entry_low is not None and entry_high is not None:
        print(f"Entry Zone  : {entry_low} - {entry_high}")
    else:
        print("Entry Zone  : N/A")

    print(f"Stop Loss   : {_safe(best.get('stop_loss'), 'N/A')} ({_safe(best.get('sl_reason'), 'N/A')})")
    print(f"Target      : {_safe(best.get('target'), 'N/A')}")
    rr = _safe(best.get("risk_reward"))
    print(f"R:R         : 1 : {rr}" if rr is not None else "R:R         : N/A")
    print(f"Score       : {_safe(best.get('total_score'), 'N/A')}/100")
    conf_label = _safe(report.get("confidence_label"), "N/A")
    conf_val = _safe(report.get("confidence_value"), "N/A")
    print(f"Confidence  : {conf_label} ({conf_val})")
    print(f"Data Quality: {_safe(report.get('data_quality'), 'N/A')}")
    print("-" * 60)
    print("PREVIOUS CYCLE")
    print("-" * 60)
    prev_symbol = _safe(report.get("previous_symbol"))
    prev_score = _safe(report.get("previous_score"))
    if prev_symbol:
        print(f"Previous Strike : {prev_symbol}")
    else:
        print("Previous Strike : N/A")
    if prev_score is not None:
        print(f"Previous Score  : {prev_score}")
    else:
        print("Previous Score  : N/A")
    curr_score = _safe(best.get("total_score"))
    if curr_score is not None:
        print(f"Current Score   : {curr_score}")
    else:
        print("Current Score   : N/A")
    print(f"Change          : {_safe(report.get('score_change'), 'N/A')}")
    print(f"State           : {_safe(report.get('signal_state'), 'N/A')}")
    print("-" * 60)
    print("REASONS")
    print("-" * 60)
    reasons = _safe(best.get("reasons"), [])
    if isinstance(reasons, list):
        for reason in reasons:
            print(f"[+] {reason}")
    else:
        print("N/A")
    print("=" * 60)
