"""The text of a cause re-check's status (#2811): why a cause-bound
causal_absence row does not count toward the cause leg yet, rendered from its
code and typed parameters, the ones the record holds (``RecheckWhy``,
``CauseOutcome``). The ``<fix_verification>`` block states it beside the row."""

from typing import Optional

from faultmaven.modules.case.contracts import (
    NoConfirmingWindow,
    NoWindowKind,
    RecheckReason,
    RecheckWhy,
    UncitedKind,
    UncitedRecheck,
)

_TEXTS = {
    RecheckReason.HELD_OPEN: "the cause's identification is held open",
    RecheckReason.DISCONFIRMATION: "it is a failed fix's disconfirmation",
    RecheckReason.PREDATES_POSITING: (
        "its data predates the cause's positing (turn {turn})"
    ),
    RecheckReason.PREDATES_FAILED_FIX: (
        "its data predates the failed fix seen at turn {turn}"
    ),
    RecheckReason.OUTSIDE_WINDOW: "it is outside the latest observed fix's window",
    RecheckReason.OTHER_CAUSE: (
        "it re-checks a cause the fix executed at turn {turn} was not run against"
    ),
}

_LATEST_FAILED = "the latest fix was seen failing; only a new fix can confirm"


def recheck_text(why: Optional[RecheckWhy]) -> Optional[str]:
    """The clause for ``why``, or None for a row that counts."""
    if why is None or why.reason == RecheckReason.COUNTS:
        return None
    if why.reason == RecheckReason.NO_CONFIRMING_WINDOW:
        return no_window_text(why.window)
    if why.reason == RecheckReason.UNCITED:
        return uncited_text(why.uncited)
    return _TEXTS[why.reason].format(turn=why.turn)


def no_window_text(window: NoConfirmingWindow) -> str:
    """Why no fix window confirms."""
    if window.kind == NoWindowKind.OFF_CAUSE_ATTEMPT:
        return (
            f"the fix executed at turn {window.fix_turn} did not act on the cause "
            "now identified; only a fix of that cause can confirm it"
        )
    pending = (
        f"; the newer fix (executed turn {window.newer_fix_turn}) is not "
        "re-checked yet"
        if window.newer_fix_turn is not None
        else None
    )
    if window.kind == NoWindowKind.NO_LONGER_IDENTIFIED:
        return (
            f"the fix executed at turn {window.fix_turn} acted on a cause no "
            "longer identified"
        ) + (pending or "; only a fix of the cause now identified can confirm it")
    if window.kind == NoWindowKind.FAILED:
        which = (
            "the fix done outside the case"
            if window.fix_turn is None
            else f"the fix executed at turn {window.fix_turn}"
        )
        seen = f"{which} was seen failing at turn {window.failure_turn}"
        if pending:
            return seen + pending
        if window.fix_turn is None:
            return f"{seen}; only a new fix can confirm"
    return _LATEST_FAILED


def uncited_text(uncited: UncitedRecheck) -> str:
    """Why a window-bound cause re-check confirms nothing, naming the cited
    basis to re-check (#2557)."""
    if uncited.kind == UncitedKind.NO_ROOT:
        return "no root cause is identified to confirm"
    if uncited.kind == UncitedKind.CITED:
        return (
            "it re-checks a row the root's verdict does not cite; name one of "
            f"{', '.join(uncited.cited_row_ids)} (the cited basis)"
        )
    return "the root cause's terminal verdict cites no row yet"
