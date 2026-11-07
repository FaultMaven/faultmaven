"""#1906 D7: the prompt says one thing about verification, and shows the model
what the engine holds about the fix.

1. Each contradiction is pinned on both sides on the RENDERED prompt: the
   losing text is gone, the kept rule is there.
2. The ``<fix_verification>`` block renders in TREATMENT, and in MITIGATION
   once a fix is dated, with the attempts and their window state, the legs,
   and the open asks; it is absent in DIAGNOSIS. Without it a model narrated a readiness the engine did
   not hold (#2427, turn 11).
3. The allocator reserves it within its own fixed budget, so the pressure that
   drops the variable sections leaves it whole.
"""

import inspect

import pytest

from faultmaven.core.investigation.cause_assurance import pending_symptom_check
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
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceNeed,
    InvestigationActionType,
    MitigationRecord,
    NeedPriority,
    NeedPurpose,
    NeedState,
    ProblemStatus,
    ProposedAction,
)
from tests.case_builders import investigating_case, with_declined_offer
from tests.unit.core.investigation.bound_rows import (
    SUPPORT_ID,
    bound_row,
    ensure_subjects,
)
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
        "After a fix, one recorded here or one the user reports carrying out\n"
        "   outside the case, each absence row names in `rechecks` the row it",
    ),
    # FAILURE PATH step 4 registers the revised fix by its action.
    "failure path": (
        "revised fix will not be recognized.\n   - See HYPOTHESIS-EVIDENCE",
        "When the user runs the revised fix, set `solution_accepted` with\n"
        "     `accepted_action_ref` naming its ACTION_ID",
    ),
    # Ids are cited where a field asks for them.
    # Ids go where a field asks for them; the two named are examples.
    "evidence ids": (
        "You don't need to cite evidence IDs.",
        "A field that asks for evidence IDs takes\nthem, as its description says",
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
    # The anchor ``symptom_elimination_rows`` implements.
    assert "the earlier\n    of the data of the causal-absence row" in source


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
    # The cited basis to re-check follows (#2557): the root's one support.
    assert f"  - cause: missing; the cited basis to re-check: {SUPPORT_ID}\n" in block
    assert "  - problem: read once the cause leg stands" in block


def test_the_problem_checked_before_the_cause_is_shown_checked():
    """The symptom re-checked first (turn 5), the cause not yet: what the gate
    asks for is the cause re-check (``_relief_without_cause_recheck``), not
    the problem's check again."""
    case = _case("treatment", cause=False, problem=True)
    gone = next(
        e
        for e in case.evidence
        if e.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
    )
    block = build_fix_verification_block(case)
    assert (
        "executed turn 3: problem re-checked gone at turn 5; cause re-check owed"
        in block
    )
    assert f"  - problem: re-checked gone at turn 5 ({gone.evidence_id})" in block
    assert "not yet observed" not in block
    assert "read once the cause leg stands" not in block


@pytest.mark.parametrize(
    "hold, reason",
    [
        ("held open", "the cause's identification is held open"),
        ("posited later", "its data predates the cause's positing (turn 5)"),
    ],
)
def test_a_cause_re_check_that_does_not_count_says_why(hold, reason):
    case = _case("treatment")
    if hold == "held open":
        case.progress.cause_identification_held_open = True
    else:
        for node in case.causal_nodes.values():
            node.metadata["root_attached_turn"] = 5
    row = next(
        e
        for e in case.evidence
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    )
    block = build_fix_verification_block(case)
    assert (
        f"  - cause: missing; re-checked, not counted yet: {row.evidence_id} "
        f"({reason})" in block
    )


def test_pending_re_checks_overflow_like_every_list():
    case = _case("treatment")
    case.progress.cause_identification_held_open = True
    for turn in range(5, 11):
        bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=turn)
    assert "held open) (+1 more)\n" in build_fix_verification_block(case)


def test_an_ask_the_engine_stopped_surfacing_is_not_listed_as_live():
    """``<evidence_needs>`` says the engine stopped putting it to the user
    (#1079); the block must not list it as an outstanding ask. Asked before
    the fix, so it holds no offer (``pending_symptom_check``)."""
    case = _case("treatment")
    need = case.evidence_needs[0]
    need.created_at_turn = 1
    need.surfaced_turns = [1, 2]
    prompt = _prompt(case)
    assert "Asks the engine has STOPPED surfacing" in prompt, "premise"
    block = _block(prompt)
    assert need.need_id not in block
    assert "Outstanding symptom-verification needs: none" in block
    assert "(1 the engine has STOPPED surfacing: see <evidence_needs>)" in block


_HOLDS = " (holds the resolution offer until answered)"


def _held(case, asked: int):
    """``case`` (both legs: the cause at 4, the problem at 5) with its need
    asked at ``asked``."""
    need = case.evidence_needs[0]
    need.created_at_turn = asked
    return need


@pytest.mark.parametrize("fulfilled", [False, True])
def test_the_block_marks_the_check_that_holds_the_offer(fulfilled):
    """Asked at 5, with both legs standing and nothing since (#1906 D5.2). A
    FULFILLED need citing only a row from before its ask still holds, and the
    mark says why."""
    case = _case("treatment", problem=True)
    need = _held(case, 5)
    if fulfilled:
        need.state = NeedState.FULFILLED
        need.fulfilling_evidence_ids = list(
            case.problem_verification.verifying_evidence_ids
        )
    assert pending_symptom_check(case) is need, "premise: held"
    mark = (
        " (holds the resolution offer until answered: the rows it cites "
        "predate the ask)"
        if fulfilled
        else _HOLDS
    )
    assert f"  - [{need.need_id}] {_ASK}{mark}\n" in build_fix_verification_block(case)


@pytest.mark.parametrize("fulfilled", [False, True])
@pytest.mark.parametrize("declined", [False, True])
def test_the_held_mark_promises_no_offer_a_decline_still_binds(declined, fulfilled):
    """Under a standing resolve decline the answer alone does not bring the
    offer back (it lapses only on a new confirmation row), and the mark says
    so, for a pending need and for one FULFILLED on a row from before its
    ask; without a decline it is unchanged."""
    case = _case("treatment", problem=True)
    need = _held(case, 5)
    why = ""
    if fulfilled:
        need.state = NeedState.FULFILLED
        need.fulfilling_evidence_ids = list(
            case.problem_verification.verifying_evidence_ids
        )
        why = ": the rows it cites predate the ask"
    if declined:
        with_declined_offer(case, kind="resolve", basis="backstop")
    decline = (
        "; the user's decline of the resolution still binds after it, unless the "
        "answer is a new problem-bound row showing the problem gone"
        if declined
        else ""
    )
    mark = f" (holds the resolution offer until answered{why}{decline})"
    assert pending_symptom_check(case) is need, "premise: held"
    assert f"  - [{need.need_id}] {_ASK}{mark}\n" in build_fix_verification_block(case)


def test_a_need_on_the_cause_leg_alone_is_not_marked():
    """Fix list 6: with no problem leg, answering the need releases nothing,
    so the block does not say it holds the offer."""
    case = _case("treatment")
    need = case.evidence_needs[0]
    assert pending_symptom_check(case) is None
    block = build_fix_verification_block(case)
    assert f"  - [{need.need_id}] {_ASK}\n" in block
    assert "holds the resolution offer" not in block


def test_a_held_check_is_listed_when_the_engine_stopped_surfacing_it():
    """Asked at 7 and 8, the block at 9: ask-exhausted (#1079), and still the
    need that holds the offer, so it is listed by id for the model to
    dispose of (critique 6)."""
    case = _case("treatment", problem=True)
    case.current_turn = 9
    need = _held(case, 7)
    need.surfaced_turns = [7, 8]
    prompt = _prompt(case)
    assert "Asks the engine has STOPPED surfacing" in prompt, "premise"
    block = _block(prompt)
    assert f"  - [{need.need_id}] {_ASK}{_HOLDS}" in block
    assert "the engine has STOPPED surfacing: see" not in block


#: The post-fix check as a need (#1906 D5.2), whitespace-normalised: the rule
#: in RE-VERIFICATION, rendered in both stages.
_NEED_RULE = (
    "A post-fix check of the reported problem that you ask the user for is a "
    "NEW `symptom_verification` need in `evidence_need_updates`, even when an "
    "older need asked the same: SUPERSEDE that older need in the same update, "
    'with `superseded_reason` "replaced by the new ask".',
    "an ask the engine has stopped surfacing still holds: do not propose "
    "`resolved` meanwhile",
    "mark the need FULFILLED with that row in `fulfilling_evidence_ids` (a row "
    "from before the ask does not answer it)",
    "If the user cannot run the check, SUPERSEDE the need and give the reason.",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("stage", ["treatment", "mitigation"])
def test_the_prompt_makes_the_post_fix_check_a_need(stage):
    prompt = _flat(_prompt(_case(stage)))
    for clause in _NEED_RULE:
        assert clause in prompt
    # The MITIGATION gate allows the re-check's need; the text it replaced
    # allowed a new symptom only.
    assert (
        "Fresh symptom-purpose needs are still allowed if mitigation work "
        "surfaces a NEW symptom" not in prompt
    )
    if stage == "mitigation":
        assert (
            "Fresh symptom-purpose needs are still allowed: for a NEW symptom "
            "the original problem didn't cover, and for the re-check of the "
            "reported one (RE-VERIFICATION)." in prompt
        )


def test_the_treatment_prompt_holds_the_proposal_on_the_open_check():
    prompt = _flat(_prompt(_case("treatment")))
    assert "ask for that check, and do not propose a transition" not in prompt
    assert (
        "ask for that check as a NEW `symptom_verification` need "
        "(RE-VERIFICATION), and do not propose a transition" in prompt
    )
    # COMPLETION: the "propose and let the confirmation step ask" bullet's
    # exception, and the answer marked FULFILLED or the need superseded.
    assert (
        "do not). The exception: do not propose while a symptom check you "
        "asked for is still open; the engine refuses it." in prompt
    )
    assert (
        "The answer to a symptom check you asked for arrives as data → record "
        "its row (`rechecks` naming the SYMPTOM row it re-checks, where one "
        "shows it) and mark the need FULFILLED with that row in "
        "`fulfilling_evidence_ids`; a row from before the ask does not answer "
        "it. If the user cannot run the check, SUPERSEDE the need and give the "
        "reason." in prompt
    )


def test_the_treatment_rules_that_propose_resolved_keep_the_hold():
    """Fix list 7: CO-EMIT ALL, the success turn and the user's transition
    intent each carry the exception, so no rule tells the model to propose
    while its check is open."""
    prompt = _flat(_prompt(_case("treatment")))
    assert (
        "the case meets the resolution criteria, and no symptom check you asked "
        "for is still open." in prompt
    )
    assert (
        "emit all three this turn or emit none. The exception: while a symptom "
        "check you asked for is still open, record the rows and do not propose "
        "resolved (the engine refuses it); the check is what is left." in prompt
    )
    assert (
        '"issue is gone"), and not while a symptom check you asked for is still '
        "open: the engine refuses it, so say the check is what is left." in prompt
    )


def _fix(case, turn: int) -> None:
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description=f"fix {turn}",
            proposed_in_turn=turn - 1,
            accepted_in_turn=turn,
            state="accepted",
        )
    )


def test_no_re_check_is_owed_on_a_ready_case():
    """The cause re-checked at 2, the fix stamped at 3, the problem gone at 4:
    window 0 confirms (the stamp lags the report), readiness is READY, and
    nothing is owed. The need is asked at 3, at the fix, so the turn-4 row
    answers it (#1906 D5.2)."""
    case = _case("treatment", cause=False)
    case.evidence_needs[0].created_at_turn = 3
    bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=2)
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=4)
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    block = build_fix_verification_block(case)
    assert "owed" not in block
    assert "executed turn 3: observed, no verdict yet" in block


def test_a_failed_latest_fix_needs_a_new_fix():
    """Fix at 3, the cause re-checked at 4, the problem back at 5 and gone at
    6: the window FAILED, and readiness asks for a new fix."""
    case = _case("treatment", persists=True)
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=6)
    block = build_fix_verification_block(case)
    assert "executed turn 3: failed at turn 5" in block
    assert (
        "  - problem: the latest fix was seen failing at turn 5; a new fix is "
        "needed" in block
    )
    assert "None" not in block and "owed" not in block


def test_only_the_latest_window_can_owe_a_re_check():
    """Fix 1 at 3, the problem gone at 4, fix 2 at 5: fix 1's window takes no
    later data, so it shows its state and owes nothing."""
    case = _case("treatment", cause=False)
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=4)
    _fix(case, 5)
    block = build_fix_verification_block(case)
    assert "executed turn 3: observed, no verdict yet" in block
    assert "executed turn 5: not yet observed" in block
    assert "owed" not in block


def test_older_attempts_are_counted_not_dropped():
    case = _case("treatment", cause=False)
    for turn in (4, 5, 6):
        _fix(case, turn)
    block = build_fix_verification_block(case)
    assert "  …and 1 earlier\n" in block
    assert "executed turn 3" not in block
    assert "executed turn 6" in block


@pytest.mark.parametrize("shape", [dict(problem=True), dict(cause=False, problem=True)])
def test_one_build_reads_one_index_and_one_window_list(monkeypatch, shape):
    from faultmaven.core.investigation import cause_assurance, terminal_transitions
    from faultmaven.core.investigation.prompts.context_builder import (
        fix_verification_block as block_module,
    )

    case = _case("treatment", **shape)
    calls = {"subject_index": 0, "fix_windows": 0}
    for name in calls:
        real = getattr(cause_assurance, name)

        def counted(*args, _real=real, _name=name, **kwargs):
            calls[_name] += 1
            return _real(*args, **kwargs)

        for module in (cause_assurance, block_module, terminal_transitions):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, counted)
    build_fix_verification_block(case)
    assert calls == {"subject_index": 1, "fix_windows": 1}


@pytest.mark.parametrize("stage", ["treatment", "mitigation"])
def test_the_block_renders_after_diagnosis(stage):
    assert _prompt(_case(stage)).count(OPEN_TAG) == 1


def test_diagnosis_has_no_block():
    case = _case("diagnosis")
    assert build_fix_verification_block(case) == ""
    assert OPEN_TAG not in _prompt(case)


def test_a_mitigation_before_any_dated_fix_has_no_block():
    """Mitigation attempts are not records until Phase 4 (D3); the block
    would point a workaround at RESOLVED's legs."""
    case = _case("mitigation", cause=False)
    case.proposed_actions.clear()
    assert build_fix_verification_block(case) == ""


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
