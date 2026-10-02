"""Terminal report (BLOCKORA §55, §56, §58, §70).

Single-result live output: one BEST STRIKE block when every gate passes,
otherwise a NO CLEAR STRIKE block with the exact gate failures. The internal
ten-candidate analysis, ranking and Top-3 backup (§10, §11, §58) are computed
and persisted exactly as before — they are simply not printed here. Values
print as N/A when missing; nothing is fabricated for display (§7).
"""

_WIDTH = 60
_RULE = "=" * _WIDTH


def _fmt(val, na="N/A"):
    return na if val is None else val


def _fmt_num(val, digits=2, na="N/A"):
    if val is None:
        return na
    try:
        return f"{float(val):.{digits}f}"
    except (TypeError, ValueError):
        return na


def _fmt_strike(val, na="N/A"):
    """Strikes arrive as floats from the chain; show 22250, not 22250.0."""
    if val is None:
        return na
    try:
        f = float(val)
    except (TypeError, ValueError):
        return val
    return str(int(f)) if f.is_integer() else str(f)


def print_status_line(angel_status, jugaad_status, db_status, last_data, next_cycle):
    print(
        f"Angel: {angel_status} | Jugaad: {jugaad_status} | "
        f"Database: {db_status} | Last Data: {_fmt(last_data)} | "
        f"Next Cycle: {_fmt(next_cycle)}"
    )


def _fmt_signed(val, digits=1, na="N/A"):
    """Signed numeric with an explicit +/- sign, so a DOWN move never renders
    as "+-12.0".  Returns N/A for anything non-numeric."""
    if val is None:
        return na
    try:
        return f"{float(val):+.{digits}f}"
    except (TypeError, ValueError):
        return na


def _direction_line(report, best=None):
    """Print the direction evidence block. Nothing is fabricated: every line
    prints only when the underlying data genuinely supports it."""
    dir_value = report.get("direction") if best is None else best.get("direction")
    confirmed = report.get("confirmed") if best is None else best.get("confirmed")
    move_points = report.get("move_points") if best is None else best.get("move_points")
    confirmation_reason = (
        report.get("confirmation_reason") if best is None else best.get("confirmation_reason", [])
    )

    print(f"Direction   : {_fmt(dir_value)}")
    if move_points is None:
        # NEUTRAL / no usable move: nothing can be claimed about confirmation.
        print("Confirmed   : N/A")
    elif confirmed is True:
        print(f"Confirmed   : {_fmt_signed(move_points)} pts (confirmed)")
    elif confirmed is False:
        print(f"Confirmed   : {_fmt_signed(move_points)} pts (not confirmed)")
    else:
        print(f"Confirmed   : {_fmt(confirmed)}")
    setup = best.get("setup") if best is not None else report.get("setup")
    if setup:
        print(f"Setup       : {_fmt(setup)}")


def _market_context(report):
    """Cycle/time and market context shared by both single-result blocks."""
    print(f"Cycle       : {_fmt(report.get('cycle_id'))}")
    print(f"Time        : {_fmt(report.get('time'))}")
    print(f"Underlying  : {_fmt(report.get('underlying'))}")
    print(f"Spot        : {_fmt_num(report.get('spot'))}")
    print(f"Expiry      : {_fmt(report.get('expiry'))}")
    print(f"Regime      : {_fmt(report.get('regime'))}")


def _confidence(report):
    value = report.get("confidence_value")
    if value is None:
        return "N/A"
    return f"{_fmt(report.get('confidence_label'))} ({_fmt_num(value)})"


def print_best_strike(report, best):
    """BLOCKORA §55: the single highest-quality strike, nothing else."""
    print(_RULE)
    print("BLOCKORA — BEST STRIKE")
    print(_RULE)
    _market_context(report)
    _direction_line(report, best)
    print(f"Strike      : {_fmt_strike(best.get('strike'))} {_fmt(best.get('option_type'))}")
    print(f"LTP         : {_fmt_num(best.get('ltp'))}")
    entry_low = best.get("entry_low")
    entry_high = best.get("entry_high")
    if entry_low is not None and entry_high is not None:
        print(f"Entry Zone  : {_fmt_num(entry_low)} - {_fmt_num(entry_high)}")
    else:
        print("Entry Zone  : N/A")
    print(f"Stop Loss   : {_fmt_num(best.get('stop_loss'))}")
    print(f"Target      : {_fmt_num(best.get('target'))}")
    rr = best.get("risk_reward")
    if rr is not None:
        print(f"Risk/Reward : 1 : {_fmt_num(rr)}")
    else:
        print("Risk/Reward : N/A")
    print(f"Score       : {_fmt_num(best.get('total_score'))}/100")
    print(f"Confidence  : {_confidence(report)}")
    print(f"Data Quality: {_fmt_num(report.get('data_quality'), 1)}/100")
    print(f"State       : {_fmt(report.get('signal_state'))}")
    reasons = best.get("reasons") or []
    if reasons:
        print("Reasons     : " + "; ".join(str(r) for r in reasons))
    else:
        print("Reasons     : N/A")
    if report.get("previous_symbol") is not None or report.get("previous_score") is not None:
        print(
            f"Previous    : {_fmt(report.get('previous_symbol'), 'None')} | "
            f"score {_fmt_num(report.get('previous_score'))} -> "
            f"{_fmt_num(best.get('total_score'))} "
            f"(change {_fmt_num(report.get('score_change'))})"
        )
    print(_RULE)


def print_no_clear_strike(report):
    """BLOCKORA §56: insufficient evidence, stated exactly and honestly."""
    print(_RULE)
    print("BLOCKORA — NO CLEAR STRIKE")
    print(_RULE)
    _market_context(report)
    print(f"Direction   : {_fmt(report.get('direction'), 'NEUTRAL')}")
    no_clear_move = report.get("move_points")
    no_clear_confirmed = report.get("confirmed")
    if no_clear_move is None:
        print("Confirmed   : N/A")
    elif no_clear_confirmed is True:
        print(f"Confirmed   : {_fmt_signed(no_clear_move)} pts (confirmed)")
    else:
        print(f"Confirmed   : {_fmt_signed(no_clear_move)} pts (not confirmed)")
    print(f"Setup       : {_fmt(report.get('setup'), 'N/A')}")
    print(f"Best Score  : {_fmt_num(report.get('best_raw_score'))}/100")
    print(f"Required    : {_fmt_num(report.get('required_score'))}/100")
    print(f"Confidence  : {_confidence(report)}")
    print(f"Data Quality: {_fmt_num(report.get('data_quality'), 1)}/100")
    print(f"Gate Failure: {_fmt(report.get('gate_reason'))}")
    reasons = report.get("no_signal_reasons") or []
    if reasons:
        print("Reason      : " + "; ".join(str(r) for r in reasons))
    else:
        print("Reason      : N/A")
    print("RESULT      : NO SIGNAL")
    print(_RULE)


def print_cycle_report(report):
    """Render the single decision-support result for one cycle."""
    best = report.get("best")
    if best:
        print_best_strike(report, best)
    else:
        print_no_clear_strike(report)