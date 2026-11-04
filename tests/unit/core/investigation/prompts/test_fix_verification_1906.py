"""#1906 D7: the prompt says one thing about verification, and shows the model
what the engine holds about the fix.

1. Each contradiction is pinned on both sides on the RENDERED prompt: the
   losing text is gone, the kept rule is there.
2. The ``<fix_verification>`` block renders in MITIGATION and TREATMENT with
   the attempts and their window state, the legs, and the open asks, and is
   absent in DIAGNOSIS. Without it a model narrated a readiness the engine did
   not hold (#2427, turn 11).
3. The allocator reserves it within its own fixed budget, so the pressure that
   drops the variable sections leaves it whole.
"""

import inspect

import pytest

from faultmaven.core.investigation.prompts.context_builder.assembly import (
    _allocate_sections,
)
from faultmaven.core.investigation.prompts.context_builder.budget import (
    _SECTION_DROPPED_MARKER,
    TokenBudget,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    CLOSE_TAG,
    FIX_VERIFICATION_MAX_TOKENS,
    OPEN_TAG,
    build_fix_verification_block,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceNeed,
    InvestigationActionType,
    MitigationRecord,
    NeedPriority,
    NeedPurpose,
    ProblemStatus,
    ProposedAction,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.bound_rows import bound_row, ensure_subjects
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

_FIX = "Point ExecStart at /usr/bin/billing-exporter and restart the unit"
_ASK = "Prometheus's view of the billing-exporter scrape target after the fix"


def _case(stage: str = "treatment", *, cause=True, problem=False, persists=False):
    """The systemd case after its fix (turn 3): the verifying row and a cause
    to bind to, an accepted fix, and the post-fix rows asked for."""
    case = investigating_case(
        statement="billing-exporter is down and its scrape target is DOWN",
        symptom="billing-exporter.service failed with 203/EXEC",
        current_turn=6,
        problem_status=ProblemStatus.VERIFIED,
    )
    ensure_subjects(case)
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description=_FIX,
            proposed_in_turn=2,
            accepted_in_turn=3,
            state="accepted",
        )
    )
    if stage == "treatment":
        case.progress.solution_accepted = True
    elif stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    if cause:
        bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=4)
    if problem:
        bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=5)
    if persists:
        bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=5)
    case.evidence_needs.append(
        EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text=_ASK,
            rationale="the reported symptom, re-checked",
            priority=NeedPriority.HIGH,
            created_at_turn=4,
        )
    )
    return case


def _prompt(case) -> str:
    return get_prompt_for_case(
        case, "status after the restart", given_tools=DEFAULT_GIVEN
    )


def _block(prompt: str) -> str:
    start = prompt.index(OPEN_TAG)
    return prompt[start : prompt.index(CLOSE_TAG, start) + len(CLOSE_TAG)]


# ---------------------------------------------------------------------------
# 1. The contradictions, both sides
# ---------------------------------------------------------------------------

#: (the losing text, the text that replaces it), on the rendered TREATMENT
#: prompt.
_CONTRADICTIONS = {
    # treatment.py: the user's words are a claim, never sufficient.
    "subjective confirmation": (
        "ACCEPT SUBJECTIVE CONFIRMATION",
        '"It\'s working now" or "looks good" is the user\'s account, a claim',
    ),
    # The RE-VERIFICATION addendum and base step 3 name `rechecks`.
    "re-verification addendum": (
        "and `source_file_id` pointing at the file\n  you re-checked. Both absence",
        "After a fix, also name in `rechecks` the checklist row",
    ),
    "base step 3": (
        "causal_absence row records THAT cause gone.\n   RESOLVED needs BOTH rows",
        "After a fix, each absence row names in `rechecks` the row it re-checks",
    ),
    # FAILURE PATH step 4 registers the revised fix by its action.
    "failure path": (
        "revised fix will not be recognized.\n   - See HYPOTHESIS-EVIDENCE",
        "When the user runs the revised fix, set `solution_accepted` with\n"
        "     `accepted_action_ref` naming its ACTION_ID",
    ),
    # Ids are cited where a field asks for them.
    "evidence ids": (
        "You don't need to cite evidence IDs.",
        "Cite evidence IDs only where a field asks\nfor them: "
        "`symptom_verification_evidence_ids` with the symptom claim, and\n"
        "`rechecks` on a post-fix observation.",
    ),
}


@pytest.mark.parametrize("name", list(_CONTRADICTIONS))
def test_each_contradiction_keeps_only_its_kept_side(name):
    losing, kept = _CONTRADICTIONS[name]
    prompt = _prompt(_case("treatment"))
    assert losing not in prompt
    assert kept in prompt


def test_the_symptom_absence_category_never_takes_the_users_words():
    """``evidence.py``: the user's words were an example of the row, and the
    row was "defense-in-depth" in TREATMENT, where RESOLVED needs it."""
    source = inspect.getsource(EvidenceCategory)
    assert '- User confirms "the dashboard loads now"' not in source
    assert "defense-in-depth confirmation" not in source
    assert '("the dashboard loads now" is a claim)' in source
    assert "one of the two rows RESOLVED needs" in source


# ---------------------------------------------------------------------------
# 2. The block
# ---------------------------------------------------------------------------


def test_the_block_shows_the_attempt_the_legs_and_the_open_ask():
    """The #1906 shape: the cause re-checked after the fix, the reported
    problem not, and the model's ask for it open."""
    case = _case("treatment")
    block = _block(_prompt(case))
    verifying = case.problem_verification.verifying_evidence_ids[0]
    cause_row = next(
        e
        for e in case.evidence
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    )
    action = case.proposed_actions[0]
    need = case.evidence_needs[0]
    assert (
        f"[{action.action_id}] {_FIX} — executed turn 3: observed, no verdict yet"
        in block
    )
    assert f"  - cause: {cause_row.evidence_id}" in block
    assert f"  - problem: missing; not re-checked since the fix: {verifying}" in block
    assert f"  - [{need.need_id}] {_ASK}" in block
    # It cites ids; it does not repeat the checklist's row texts.
    assert cause_row.summary not in block


@pytest.mark.parametrize(
    "shape, state",
    [
        (dict(cause=False), "not yet observed"),
        (dict(problem=True), "succeeded"),
        (dict(persists=True), "failed at turn 5"),
    ],
)
def test_an_attempt_carries_its_window_state(shape, state):
    block = build_fix_verification_block(_case("treatment", **shape))
    assert f"— executed turn 3: {state}" in block


def test_a_missing_cause_leg_does_not_read_the_problem_leg():
    block = build_fix_verification_block(_case("treatment", cause=False))
    assert "  - cause: missing" in block
    assert "  - problem: not read until the cause leg stands" in block


@pytest.mark.parametrize("stage", ["treatment", "mitigation"])
def test_the_block_renders_after_diagnosis(stage):
    assert _prompt(_case(stage)).count(OPEN_TAG) == 1


def test_diagnosis_has_no_block():
    case = _case("diagnosis")
    assert build_fix_verification_block(case) == ""
    assert OPEN_TAG not in _prompt(case)


# ---------------------------------------------------------------------------
# 3. Reserved, within its own budget
# ---------------------------------------------------------------------------


_RESERVE = dict(
    identity="IDENTITY",
    core_context="CORE",
    milestones_str="MILESTONES",
    inquiry_state_str="",
    pending_action_str="",
    user_message_block="the user asks something",
    feedback_str="",
    exclusions_str="",
)


def _allocate(budget: TokenBudget, case, block: str) -> dict:
    return _allocate_sections(
        budget=budget,
        case=case,
        provider_name=None,
        model_name=None,
        **_RESERVE,
        evidence_str="EVIDENCE " * 400,
        graduated_history="HISTORY " * 400,
        compact_history="LATEST TURN " * 40,
        journal_str="JOURNAL " * 200,
        conclusion_str="CONCLUSION " * 200,
        kb_str="KB " * 200,
        hypothesis_str="HYPOTHESIS " * 400,
        evidence_needs_str="NEEDS " * 200,
        entity_highlights_str="ENTITY " * 200,
        fix_verification_str=block,
    )


def test_the_block_survives_the_pressure_that_drops_variable_sections():
    """The causal graph is a floor-0 variable section and is dropped first;
    the block is reserved, so the same budget leaves it whole."""
    case = _case("treatment")
    block = build_fix_verification_block(case)
    probe = TokenBudget(10**9)
    reserve = sum(probe.count(text) for text in _RESERVE.values())

    budget = TokenBudget(reserve + probe.count(block) + 40)
    ctx = _allocate(budget, case, block)
    assert ctx["fix_verification"] == block
    assert ctx["hypotheses"] == _SECTION_DROPPED_MARKER, "premise: under pressure"


def test_an_oversized_block_is_cut_to_its_budget_and_stays_closed():
    case = _case("treatment")
    head, _, tail = build_fix_verification_block(case).rpartition("\n")
    oversized = head + "\n" + ("  - [eneed_x] filler ask\n" * 400) + tail
    budget = TokenBudget(10**9)
    ctx = _allocate(budget, case, oversized)
    capped = ctx["fix_verification"]
    assert capped.startswith(OPEN_TAG) and capped.endswith(CLOSE_TAG)
    # Within the budget, with a token's slack for the joins.
    assert budget.count(capped) <= FIX_VERIFICATION_MAX_TOKENS + 2
