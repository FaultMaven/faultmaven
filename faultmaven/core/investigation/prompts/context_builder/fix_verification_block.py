"""The ``<fix_verification>`` block: what the engine holds about the fix, shown
to the model on every MITIGATION and TREATMENT turn (#1906 D7).

Without it no prompt showed the model its fix attempts or which leg of a
resolution stands, so a model could narrate a readiness the engine did not
hold (#2427, turn 11). It shows, one line each:

- the latest fix attempts (accepted SOLUTIONs, by their execution turn) with
  their window state (``cause_assurance.fix_windows``);
- the two legs a resolution reads (``cause_elimination_rows`` and
  ``symptom_elimination_rows``), by row id, and the rows that showed the
  problem with no re-check in the problem leg;
- the outstanding symptom-verification needs.

Informational: no gate reads it. It cites ids and never repeats the
re-verification checklist's row texts (``evidence_needs``).

A prompt tag, not a stored key: ``Evidence.metadata["fix_verification"]`` is
the namespace a post-fix row's ``rechecks`` is stored under, a different thing
that shares the name.

The allocator RESERVES it (``assembly._allocate_sections``) within a fixed
budget, ``FIX_VERIFICATION_MAX_TOKENS``: a variable section with a floor of 0,
as the causal graph is, is dropped first under pressure, and pressure is what
the long post-fix cases that need this block bring.
"""

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    cause_elimination_rows,
    fix_windows,
    subject_index,
    symptom_elimination_rows,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InvestigationActionType,
    InvestigationStage,
    NeedPurpose,
)

from .budget import TokenBudget
from .history import _truncate_request_text

#: The block's fixed share of the prompt, reserved before any variable
#: section. Its construction bounds (below) keep it well under this; the cap
#: is the backstop.
FIX_VERIFICATION_MAX_TOKENS = 600
#: The latest attempts shown.
MAX_ATTEMPTS = 3
#: The row ids shown per leg, and the outstanding needs shown.
MAX_IDS = 6
MAX_NEEDS = 5
_DESCRIPTION_CHARS = 80

OPEN_TAG = "<fix_verification>"
CLOSE_TAG = "</fix_verification>"


def _ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:MAX_IDS])
    more = len(ids) - MAX_IDS
    return f"{shown} (+{more} more)" if more > 0 else shown


def _window_state(window) -> str:
    if window is None:
        return "not yet observed"
    if window.state == FIX_SUCCEEDED:
        return "succeeded"
    if window.state == FIX_FAILED:
        return f"failed at turn {window.failure_turn}"
    return "observed, no verdict yet" if window.observed else "not yet observed"


def _attempt_lines(case: Case) -> list[str]:
    """The accepted SOLUTIONs with an execution turn, as the fix windows date
    them (``cause_assurance._dated_fix_turns``), the latest ``MAX_ATTEMPTS``."""
    attempts = sorted(
        (
            a
            for a in case.proposed_actions or []
            if a.state == "accepted"
            and getattr(a.action_type, "value", a.action_type)
            == InvestigationActionType.SOLUTION.value
            and a.accepted_in_turn is not None
        ),
        key=lambda a: a.accepted_in_turn,
    )[-MAX_ATTEMPTS:]
    if not attempts:
        return ["Fix attempts: none recorded"]
    windows = {w.fix_turn: w for w in fix_windows(case) if w.fix_turn is not None}
    lines = [f"Fix attempts (latest {MAX_ATTEMPTS}, oldest first):"]
    for a in attempts:
        description = " ".join((a.description or "").split())
        if len(description) > _DESCRIPTION_CHARS:
            description = description[: _DESCRIPTION_CHARS - 1].rstrip() + "…"
        lines.append(
            f"  - [{a.action_id}] {description} — executed turn "
            f"{a.accepted_in_turn}: {_window_state(windows.get(a.accepted_in_turn))}"
        )
    return lines


def _leg_lines(case: Case) -> list[str]:
    cause_rows = cause_elimination_rows(case)
    problem_rows = symptom_elimination_rows(case, cause_rows)
    lines = [
        "Resolution legs (RESOLVED needs both):",
        "  - cause: " + (_ids([r.evidence_id for r in cause_rows]) or "missing"),
    ]
    if not cause_rows:
        # The problem leg is read from the cause leg's turn on.
        lines.append("  - problem: not read until the cause leg stands")
        return lines
    problem = "  - problem: " + (
        _ids([r.evidence_id for r in problem_rows]) or "missing"
    )
    subjects = subject_index(case)
    named = {getattr(r, "rechecks_evidence_id", None) for r in problem_rows}
    unchecked = [v for v in subjects.verifying if v in subjects.rows and v not in named]
    if unchecked:
        problem += "; not re-checked since the fix: " + _ids(unchecked)
    lines.append(problem)
    return lines


def _need_lines(case: Case) -> list[str]:
    needs = [
        n
        for n in case.evidence_needs
        if n.is_outstanding and n.purpose == NeedPurpose.SYMPTOM_VERIFICATION
    ]
    if not needs:
        return ["Outstanding symptom-verification needs: none"]
    lines = ["Outstanding symptom-verification needs:"]
    lines += [
        f"  - [{n.need_id}] {_truncate_request_text(n.request_text)}"
        for n in needs[:MAX_NEEDS]
    ]
    if len(needs) > MAX_NEEDS:
        lines.append(f"  …and {len(needs) - MAX_NEEDS} more")
    return lines


def build_fix_verification_block(case: Case) -> str:
    """The block, or ``""`` outside MITIGATION and TREATMENT (the stages the
    re-verification checklist renders in)."""
    if case.state != CaseState.INVESTIGATING or case.current_stage not in (
        InvestigationStage.MITIGATION,
        InvestigationStage.TREATMENT,
    ):
        return ""
    lines = [
        OPEN_TAG,
        "The engine's record of the fix (informational; ids name evidence rows):",
        *_attempt_lines(case),
        *_leg_lines(case),
        *_need_lines(case),
        CLOSE_TAG,
    ]
    return "\n".join(lines)


def cap_fix_verification_block(text: str, budget: TokenBudget) -> str:
    """``text`` within ``FIX_VERIFICATION_MAX_TOKENS``: the body cut from its
    tail with a marker, the tags kept, so a cut never leaves the block open."""
    if not text or budget.count(text) <= FIX_VERIFICATION_MAX_TOKENS:
        return text
    body = text[len(OPEN_TAG) + 1 : -(len(CLOSE_TAG) + 1)]
    room = FIX_VERIFICATION_MAX_TOKENS - budget.count(f"{OPEN_TAG}\n\n{CLOSE_TAG}")
    return f"{OPEN_TAG}\n{budget._truncate_to(body, room)}\n{CLOSE_TAG}"
