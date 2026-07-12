"""INV-40 (§7.9) — narration-truth coherence guard.

The narration channel (``agent_response``) is LLM free text and sits outside
every truth surface the §7.6 reconciliation lane reads. #668: a long-context
haiku turn narrated *"Case resolved."* 3/3 while every engine surface stood at
INVESTIGATING. INV-40 reconciles the existing narrow ``_COMPLETION_PHRASES``
scan against engine truth and, on an over-claim, APPENDS a corrective notice
below the LLM's prose (never a substitution — the DF-4 lesson).

These tests pin the fire predicate (``_narration_overclaim_notice``) and the
append composition (``_prose_with_gate_notice``). The per-turn counter
``narration_overclaim_total`` increments iff the predicate returns non-None (its
live increment is covered by the sim gate, as with INV-39), so pinning the
predicate pins the metric's firing condition. Pass/fail is mechanical — a canned
prose string + an engine state — with no model-graded judge (LLM-agnostic
testing invariant).
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _narration_asserts_disposition,
    _narration_overclaim_notice,
    _prose_with_gate_notice,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


def _case(state: CaseState, *, pending_transition: dict | None = None) -> Case:
    # Build in INVESTIGATING (terminal states require resolved_at/closed_at/
    # closure_reason cross-field validation); the guard predicate only reads
    # ``case.state``, so bypass the validators with object.__setattr__ to place
    # a bare terminal state for the read (documented Case-testing pattern).
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="intermittent latency",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="intermittent latency", severity=CaseSeverity.HIGH
        ),
    )
    # Assign pending_transition while still INVESTIGATING — ``validate_assignment``
    # re-validates the whole model on set, and a bare terminal state would fail
    # the timestamp cross-field check. Flip the state last via object.__setattr__.
    case.pending_transition = pending_transition
    if state is not CaseState.INVESTIGATING:
        object.__setattr__(case, "state", state)
    return case


# ---------------------------------------------------------------------------
# Detector — the same narrow _COMPLETION_PHRASES scan, reused verbatim (INV-15).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prose",
    [
        "Case resolved.",
        "Great — the case is resolved now.",
        "I've resolved this for you.",
        "Marking as resolved.",
        "The CASE IS NOW CLOSED.",  # case-insensitive
    ],
)
def test_detector_fires_on_disposition_claims(prose):
    assert _narration_asserts_disposition(prose) is True


@pytest.mark.parametrize(
    "prose",
    [
        "Let me check the logs before we conclude anything.",
        "The fix appears to have helped; can you confirm the errors are gone?",
        "This looks resolved on the metrics side, but the cause is unconfirmed.",
        "",
        None,
    ],
)
def test_detector_silent_on_non_disposition_prose(prose):
    # "resolved on the metrics side" contains no completion PHRASE
    # ("case resolved", "marked as resolved", …) — the scan is deliberately
    # narrow (INV-15 / PR #299), so this is a true negative, not a miss.
    assert _narration_asserts_disposition(prose) is False


# ---------------------------------------------------------------------------
# Fire predicate — reconcile the scan against engine truth.
# ---------------------------------------------------------------------------


def test_overclaim_on_investigating_returns_notice():
    """Completion phrase + non-terminal state + no pending gate → correct."""
    case = _case(CaseState.INVESTIGATING)
    notice = _narration_overclaim_notice(case, "Case resolved.")
    assert notice is not None
    assert "still under investigation" in notice.lower()


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED])
def test_true_claim_on_terminal_state_returns_none(state):
    """When a terminal transition executed (or the case is already terminal),
    the disposition claim is TRUE — nothing to correct."""
    case = _case(state)
    assert _narration_overclaim_notice(case, "Case resolved.") is None


def test_gate_prose_already_appended_suppresses_notice():
    """When one of the five prose gate-override branches already appended a
    state-framing notice this turn (gate_prose_appended=True), INV-40 does not
    stack a second notice — regardless of whether a transition is pending."""
    case = _case(CaseState.INVESTIGATING, pending_transition={"to_state": "resolved"})
    assert (
        _narration_overclaim_notice(case, "Case resolved.", gate_prose_appended=True)
        is None
    )
    # Also suppressed with no pending transition (defensive — the flag governs).
    case2 = _case(CaseState.INVESTIGATING)
    assert (
        _narration_overclaim_notice(case2, "Case resolved.", gate_prose_appended=True)
        is None
    )


def test_pending_proposal_without_gate_prose_still_corrects():
    """#684 review finding 1: the guard's most probable real-world shape — the
    LLM narrates "Case resolved." AND emits proposed_transition the same turn, so
    the suggestions-only override branch mints a pending transition but appends
    NO prose (gate_prose_appended=False). The over-claim must still be corrected,
    with pending-aware wording that points at the confirm/decline options."""
    case = _case(CaseState.INVESTIGATING, pending_transition={"to_state": "resolved"})
    notice = _narration_overclaim_notice(
        case, "Case resolved.", gate_prose_appended=False
    )
    assert notice is not None
    lowered = notice.lower()
    assert "proposed" in lowered  # frames it as not-yet-effective
    assert "confirm" in lowered  # points at the affordances below
    # The strictly-worse variant: prose says "resolved" while CLOSE is proposed.
    case_close = _case(
        CaseState.INVESTIGATING, pending_transition={"to_state": "closed"}
    )
    assert (
        _narration_overclaim_notice(
            case_close, "Case resolved.", gate_prose_appended=False
        )
        is not None
    )


def test_clean_prose_passes_through():
    """No completion phrase → no notice; the prose is delivered verbatim."""
    case = _case(CaseState.INVESTIGATING)
    assert (
        _narration_overclaim_notice(case, "Can you share the post-fix error rate?")
        is None
    )


# ---------------------------------------------------------------------------
# Graceful denial — a false positive degrades to a still-true notice (§7.9).
# ---------------------------------------------------------------------------


def test_conditional_prose_false_positive_is_still_true():
    """The scan fires on conditional prose ("once you confirm, the case is
    resolved"), but the appended notice is *still true* on a non-terminal case —
    mildly redundant, never wrong, never state-mutating."""
    case = _case(CaseState.INVESTIGATING)
    notice = _narration_overclaim_notice(
        case, "Once you confirm the fix held, the case is resolved."
    )
    assert notice is not None
    assert "not been resolved" in notice.lower()
    # The predicate is pure — it must not mutate the case.
    assert case.state is CaseState.INVESTIGATING
    assert case.pending_transition is None


# ---------------------------------------------------------------------------
# Composition — append-only, prose preserved intact above the separator (DF-4).
# ---------------------------------------------------------------------------


def test_notice_is_appended_never_substituted():
    case = _case(CaseState.INVESTIGATING)
    prose = "Here's my analysis of the connection-pool timeouts. Case resolved."
    notice = _narration_overclaim_notice(case, prose)
    composed = _prose_with_gate_notice(prose, notice)
    # The LLM's analysis survives verbatim above the separator …
    assert composed.startswith(prose)
    assert "\n\n---\n\n" in composed
    # … and the correction lands below it.
    assert composed.endswith(notice)
