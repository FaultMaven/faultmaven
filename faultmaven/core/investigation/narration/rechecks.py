"""The text of a cause re-check's status (#2811): why a cause-bound
causal_absence row does not count toward the cause leg yet, rendered from its
code and parameters (``RecheckWhy``, ``cause_assurance.cause_recheck_status``).
The ``<fix_verification>`` block states it beside the row."""

from typing import Optional

from faultmaven.modules.case.contracts import RecheckReason, RecheckWhy

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


def recheck_text(why: Optional[RecheckWhy]) -> Optional[str]:
    """The clause for ``why``, or None for a row that counts. The two
    clauses another derivation composed (no confirming window, an uncited
    re-check) are stated as composed."""
    if why is None or why.reason == RecheckReason.COUNTS:
        return None
    if why.reason in (RecheckReason.NO_CONFIRMING_WINDOW, RecheckReason.UNCITED):
        return why.clause
    return _TEXTS[why.reason].format(turn=why.turn)
