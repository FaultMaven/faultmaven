"""Outcome-based assertion helpers for real-LLM integration tests.

These helpers assert on **structural outcomes** (case status, presence
of state fields, structured suggestions with correct intents) rather
than on LLM-generated text content. Asserting on text content is
exactly the brittleness real-LLM tests are supposed to avoid — LLM
rewording is normal across runs, models, and provider versions; case
transitions are not.

When writing a new helper, the heuristic is: would this assertion pass
if the LLM phrased its response slightly differently but the system
state ended up the same? If yes, the helper is robust. If no,
reconsider what you're really testing.
"""

from typing import Any, Iterable

from faultmaven.modules.case.contracts import Case, CaseState


def assert_case_status(case: Case, expected: CaseState, context: str = "") -> None:
    """Assert the case is in the expected status; include context on failure.

    Use ``context`` to describe the turn under test — failure messages
    that say "after turn 2 the case should be in INQUIRY but is
    INVESTIGATING" are far more useful than the raw status mismatch.
    """
    msg = f"Expected status {expected.value}, got {case.state.value}"
    if context:
        msg = f"{context}: {msg}"
    assert case.state == expected, msg


def assert_handshake_deferred_at(case: Case, expected_turn: int) -> None:
    """Assert the same-turn-confirmation guard fired on the expected turn.

    This is the structural signature of an INV-01 guard fire: the engine
    captured the deferral by writing ``case.inquiry.handshake_deferred_at_turn``.
    """
    actual = case.inquiry.handshake_deferred_at_turn
    assert actual == expected_turn, (
        f"Expected handshake_deferred_at_turn={expected_turn}, got {actual}. "
        f"This usually means the LLM did not emit "
        f"user_confirmed_investigation=True on the test turn — the guard "
        f"only fires when both fields are set in one shot. Inspect the "
        f"raw LLM response to confirm the test scenario was reproduced."
    )


def assert_has_confirmation_suggestions(follow_ups: Iterable[dict[str, Any]]) -> None:
    """Assert the response includes a clickable confirmation pair.

    Required shape: at least one COOPERATIVE suggestion carrying
    ``intent.type == "confirmation"`` with ``confirmation_value == True``
    (the positive click), and ideally a companion with
    ``confirmation_value == False``. This is the structural contract
    the frontend relies on to route clicks to the deterministic
    CONFIRMATION intent path in milestone_engine.

    Asserts the positive option exists; the negative is optional
    (some flows omit it).
    """
    fups = list(follow_ups)
    positive = [
        f
        for f in fups
        if (f.get("intent") or {}).get("type") == "confirmation"
        and (f.get("intent") or {}).get("confirmation_value") is True
    ]
    assert positive, (
        f"Expected at least one COOPERATIVE suggestion with "
        f"intent={{type: 'confirmation', confirmation_value: True}}, "
        f"got: {fups!r}"
    )


def assert_no_silent_stall(case: Case, max_turns_without_progress: int = 5) -> None:
    """Assert the case isn't stuck — turns_without_progress is bounded.

    A case that keeps incrementing turns_without_progress without
    transitioning or resolving is the dynamic-drift signature we're
    trying to surface. Use as a sanity check at the end of a multi-turn
    real-LLM test.
    """
    actual = case.turns_without_progress
    assert actual <= max_turns_without_progress, (
        f"Case turns_without_progress={actual} exceeds threshold "
        f"{max_turns_without_progress} — likely a silent stall. "
        f"Status={case.state.value}, "
        f"inquiry.proposed_problem_statement="
        f"{case.inquiry.proposed_problem_statement!r}, "
        f"inquiry.problem_statement_confirmed="
        f"{case.inquiry.problem_statement_confirmed}."
    )
