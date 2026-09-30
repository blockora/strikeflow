"""Terminal report (BLOCKORA §55, §56, §70).

Structured per-cycle output including the NO CLEAR STRIKE block with gate
failure reasons, and a connection-status line. All values print as N/A when
missing — nothing is fabricated for display.
"""


def _fmt(val, na="N/A"):
    return na if val is None else val


def _fmt_num(val, digits=2, na="N/A"):
    if val is None:
        return na
    try:
        return f"{float(val):.{digits}f}"
    except (TypeError, ValueError):
        return na


def print_status_line(angel_status, jugaad_status, db_status, last_data, next_cycle):
    print(
        f"Angel: {angel_status} | Jugaad: {jugaad_status} | "
        f"Database: {db_status} | Last Data: {_fmt(last_data)} | "
        f"Next Cycle: {_fmt(next_cycle)}"
    )


def print_cycle_report(report):
    print("=" * 60)
    print("OPTION STRIKE SELECTOR")
    print("=" * 60)
    print(f"Cycle       : {_fmt(report.get('cycle_id'))}")
    print(f"Time        : {_fmt(report.get('time'))}")
    print(f"Underlying  : {_fmt(report.get('underlying'))}")
    print(f"Spot        : {_fmt_num(report.get('spot'))}")
    print(f"Expiry      : {_fmt(report.get('expiry'))}")
    print(f"Regime      : {_fmt(report.get('regime'))}")
    print(f"Data Quality: {_fmt_num(report.get('data_quality'), 1)}/100")
    if report.get("data_note"):
        print(f"Data Note   : {report['data_note']}")
    print("-" * 60)
    print("TOP 10 CANDIDATES")
    print("-" * 60)
    candidates = report.get("candidates") or []
    if not candidates:
        print("INSUFFICIENT VALID CANDIDATES")
        print(f"Reason: {report.get('candidate_status', 'NO_CHAIN_DATA')}")
    else:
        print("Rank  Strike       Type   Score")
        for c in candidates:
            print(
                f"{c.get('rank', '?'):<5} "
                f"{_fmt(c.get('strike')):<12} "
                f"{_fmt(c.get('option_type')):<6} "
                f"{_fmt_num(c.get('total_score'))}"
            )

    best = report.get("best")
    if not best:
        print("-" * 60)
        print("NO CLEAR STRIKE")
        print("-" * 60)
        print(f"Best Raw Score : {_fmt_num(report.get('best_raw_score'))}")
        print(f"Required Score : {_fmt_num(report.get('required_score'))}")
        gate = report.get("gate_reason")
        if gate:
            print(f"Gate Failure   : {gate}")
        print("Reason:")
        for r in report.get("no_signal_reasons", []):
            print(f"- {r}")
        print("RESULT:")
        print("NO SIGNAL")
        print("=" * 60)
        return

    print("-" * 60)
    print("BEST STRIKE")
    print("-" * 60)
    print(
        f"Symbol      : {_fmt(best.get('underlying'))} {_fmt(best.get('expiry'))} "
        f"{_fmt(best.get('strike'))} {_fmt(best.get('option_type'))}"
    )
    print(f"LTP         : {_fmt_num(best.get('ltp'))}")
    entry_low = best.get("entry_low")
    entry_high = best.get("entry_high")
    if entry_low is not None and entry_high is not None:
        print(f"Entry Zone  : {_fmt_num(entry_low)} - {_fmt_num(entry_high)}")
    else:
        print("Entry Zone  : N/A")
    print(f"Stop Loss   : {_fmt_num(best.get('stop_loss'))} ({_fmt(best.get('sl_reason'))})")
    print(f"Target      : {_fmt_num(best.get('target'))}")
    print(f"Risk        : {_fmt_num(best.get('risk'))}")
    print(f"Reward      : {_fmt_num(best.get('reward'))}")
    rr = best.get("risk_reward")
    print(f"R:R         : 1 : {_fmt_num(rr)}" if rr is not None else "R:R         : N/A")
    print(f"Score       : {_fmt_num(best.get('total_score'))}/100")
    print(f"Confidence  : {_fmt(report.get('confidence_label'))} ({_fmt_num(report.get('confidence_value'))})")
    print(f"Data Quality: {_fmt_num(report.get('data_quality'), 1)}/100")
    print("-" * 60)
    print("PREVIOUS CYCLE")
    print("-" * 60)
    print(f"Previous Strike : {_fmt(report.get('previous_symbol'), 'None')}")
    print(f"Previous Score  : {_fmt_num(report.get('previous_score'))}")
    print(f"Current Score   : {_fmt_num(best.get('total_score'))}")
    print(f"Change          : {_fmt_num(report.get('score_change'))}")
    print(f"State           : {_fmt(report.get('signal_state'))}")
    print("-" * 60)
    print("REASONS")
    print("-" * 60)
    reasons = best.get("reasons") or []
    if reasons:
        for reason in reasons:
            print(f"[+] {reason}")
    else:
        print("N/A")
    print("=" * 60)
