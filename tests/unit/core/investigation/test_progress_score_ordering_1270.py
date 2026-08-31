"""Progress is scored after EVERY arm writer has run (#1270).

``check_if_progress_made`` is a NOR over nine arms written onto one working
dict during a turn. The generation path used to take that reading five lines
before ``_check_automatic_transitions`` wrote the ``status_transitioned`` arm,
so an automatic INQUIRY->INVESTIGATING transition never counted as progress and
``turns_without_progress`` climbed through a turn that demonstrably advanced the
case. The emitted #1142 row said so in one breath —
``arms.status_transitioned: 1`` beside ``progress_made: false``, a shape the
predicate cannot produce.

The guards here are deliberately ARM-GENERIC. Pinning ``status_transitioned``
alone would pass the next time an arm is written after the read, which is the
same defect wearing a different key: what is asserted instead is that the LAST
reading of the turn saw exactly the arms the emitted row reports, whatever those
arms are.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation.case_telemetry import (
    PREDICATE_ARM_KEYS,
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    build_case_turn_event,
    collect_progress_arms,
)
from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    check_if_progress_made,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData


class _StubLLM(ILLMProvider):
    """Returns whatever ``payload`` currently holds, for both call shapes."""

    def __init__(self) -> None:
        self.payload = "{}"

    async def generate(self, prompt, **kwargs):
        return self.payload

    async def generate_stream(self, prompt, **kwargs):
        yield self.payload

    async def generate_with_history(self, messages, **kwargs):
        return self.payload

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "S", "strict": True, "schema": schema},
            },
        )


@pytest.fixture
def engine_and_llm():
    llm = _StubLLM()
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    return MilestoneEngine(llm, repo, investigation_tools=MagicMock()), llm


def _inquiry_case() -> Case:
    return Case(
        case_id="case_1270aaaaaaaa",
        title="Checkout API returning 503s",
        state=CaseState.INQUIRY,
        user_id="user_123",
        organization_id="org_123",
        description="",
        inquiry=InquiryData(thread_id="thread_1270"),
    )


_TURN1 = json.dumps(
    {
        "agent_response": "Let me confirm: the checkout API returns 503s. Right?",
        "state_updates": {
            "problem_confirmation": {
                "problem_type": "unavailability",
                "severity_guess": "high",
                "preliminary_guidance": "Checkout API returning 503s",
            },
            "preliminary_urgency": {
                "level": "HIGH",
                "is_ongoing": True,
                "is_incident_report": True,
                "impact_assessment": "Customers cannot check out",
            },
            "proposed_problem_statement": "Checkout API returning 503s for all users",
            "user_confirmed_investigation": False,
        },
    }
)

_TURN2_CONFIRM = json.dumps(
    {
        "agent_response": "Confirmed. Starting the investigation.",
        "state_updates": {"user_confirmed_investigation": True},
    }
)


async def _two_turn_transition(engine, llm):
    """Turn 1 proposes, turn 2 confirms -> the automatic transition fires."""
    llm.payload = _TURN1
    first = await engine.process_turn(_inquiry_case(), "Our checkout API is 503ing")
    assert first["case_updated"].state == CaseState.INQUIRY
    llm.payload = _TURN2_CONFIRM
    second = await engine.process_turn(first["case_updated"], "yes, that is it")
    assert second["case_updated"].state == CaseState.INVESTIGATING, (
        "positive control: the turn under test must actually transition, or "
        "this file asserts nothing"
    )
    return second


@pytest.mark.asyncio
async def test_an_automatic_transition_counts_as_progress_on_its_own_turn(
    engine_and_llm,
):
    """The turn the engine moved the case on is a progress turn.

    All three surfaces that report the decision must agree: the returned
    metadata, the stall counter, and the turn-history record.
    """
    engine, llm = engine_and_llm
    result = await _two_turn_transition(engine, llm)
    case = result["case_updated"]

    assert result["metadata"]["status_transitioned"] is True
    assert result["metadata"]["progress_made"] is True
    assert case.turns_without_progress == 0
    assert case.turn_history[-1].progress_made is True


@pytest.mark.asyncio
async def test_no_emitted_row_carries_a_fired_arm_beside_progress_false(
    engine_and_llm,
):
    """The row shape the predicate cannot produce must not be emittable.

    Stated over the arm counts as a whole rather than over ``status_transitioned``:
    a row with ANY arm fired and ``progress_made: false`` is self-contradictory,
    whichever arm it is.

    **The denominator is asserted before the universal.** "No row violates X" is
    trivially true over zero rows, and over rows with no arm fired — either way
    the assertion would pass on a completely broken fix. So this establishes,
    in order: a handoff exists, it carries arms, at least one arm actually
    fired, and only then that the row's verdict agrees with them.
    """
    engine, llm = engine_and_llm
    result = await _two_turn_transition(engine, llm)
    metadata = result["metadata"]

    assert TELEMETRY_HANDOFF_KEY in metadata, (
        "denominator: the turn produced no telemetry handoff, so there is no "
        "row for the invariant to quantify over"
    )
    arms = metadata[TELEMETRY_HANDOFF_KEY]["arms"]
    assert arms, "denominator: the handoff carried no arm counts at all"

    event = build_case_turn_event(
        result["case_updated"],
        path=TurnPath.LLM,
        arms=arms,
        progress_made=bool(metadata["progress_made"]),
        outcome=metadata.get("outcome"),
    )
    fired = {k: v for k, v in event["arms"].items() if v}
    assert fired, (
        "denominator: no arm fired on this turn, so the universal has nothing "
        "to quantify over and would pass on unfixed code"
    )
    assert (
        event["progress_made"] is True
    ), f"row claims progress_made=false while these arms fired: {fired}"


@pytest.mark.asyncio
async def test_the_last_reading_of_the_turn_saw_every_arm_the_row_reports(
    engine_and_llm, monkeypatch
):
    """The ordering invariant itself, named by no arm.

    Every DECISION taken during the turn is recorded with the arms visible to
    it at that moment. The LAST one is the one that lands in ``progress_made``,
    so it must have seen the same arms the emitted row reports — otherwise some
    writer ran after the decision, which is #1270 whatever the key is called.

    **Wrap ``score_progress``, not ``check_if_progress_made``.** The decision is
    the write, not the predicate, and the two are not the same event: the write
    is monotone (``already_true or predicate(...)``), so on any turn where an
    arm fired before step 4 the ``or`` short-circuits and the PREDICATE is never
    called at 4b at all. A spy on the predicate then reports the step-4 reading
    as the turn's last, computes ``status_transitioned`` as written late, and
    fails on a turn where 4b behaved exactly as designed — measured on 12 of the
    170 corpus transition cases, which carried an upload on the transition turn
    (``novel_files_uploaded`` fires at Step 0). ``test_a_transition_turn_carrying
    _an_upload_is_not_a_late_write`` below is that shape, pinned.

    **Denominators first, again.** ``late`` is empty both when nothing was
    written late and when the row reports no arms at all, so the count of fired
    arms in the row is asserted before the comparison — otherwise a turn that
    fired nothing would satisfy this on unfixed code.
    """
    engine, llm = engine_and_llm
    seen: list[dict[str, int]] = []
    real = me.score_progress

    def recording(metadata):
        verdict = real(metadata)
        seen.append(collect_progress_arms(metadata))
        return verdict

    monkeypatch.setattr(me, "score_progress", recording)

    result = await _two_turn_transition(engine, llm)
    reported = result["metadata"][TELEMETRY_HANDOFF_KEY]["arms"]

    assert seen, "denominator: no progress decision was taken on this turn"
    assert any(
        reported.values()
    ), f"denominator: the row reports no fired arm, nothing to compare: {reported}"
    last = seen[-1]
    late = {k: reported[k] for k in reported if reported[k] and not last.get(k)}
    assert not late, (
        f"arms written AFTER the final progress reading: {late}; "
        f"last reading saw {last}"
    )


@pytest.mark.asyncio
async def test_a_transition_turn_carrying_an_upload_is_not_a_late_write(
    engine_and_llm, monkeypatch
):
    """An arm before step 4 and the transition after — one decision, no late write.

    ``novel_files_uploaded`` is merged onto the working dict at Step 0, above the
    path fork, so this turn has an arm standing well before the transition lands.
    It is the shape that made a predicate-spy guard red on healthy code, and it
    is not hypothetical: 12 of the 170 corpus transition cases are exactly this
    (an upload riding the confirmation), which is why those 12 scored correctly
    even before #1270 was fixed.

    Now that the dead provisional reading is gone there is ONE decision per
    generation turn, and it must have seen BOTH arms.
    """
    engine, llm = engine_and_llm
    decisions: list[dict[str, int]] = []
    real_score = me.score_progress

    def recording_score(metadata):
        verdict = real_score(metadata)
        decisions.append(collect_progress_arms(metadata))
        return verdict

    monkeypatch.setattr(me, "score_progress", recording_score)

    llm.payload = _TURN1
    first = await engine.process_turn(_inquiry_case(), "Our checkout API is 503ing")
    assert first["case_updated"].state == CaseState.INQUIRY

    llm.payload = _TURN2_CONFIRM
    decisions.clear()
    second = await engine.process_turn(
        first["case_updated"],
        "yes, that is it",
        attachments=[
            {
                "file_id": "file_aaaaaaaaaaaa",
                "filename": "checkout.log",
                "data_type": "logs_and_errors",
                "size": 1024,
                "source_type": "file_upload",
                "summary": "",
                "storage_ref": "ref/checkout.log",
                "is_novel": True,
            }
        ],
    )

    metadata = second["metadata"]
    reported = metadata[TELEMETRY_HANDOFF_KEY]["arms"]

    # Denominators: the turn really did transition AND really did carry a novel
    # upload, so both arms are live and the comparison has something to bite on.
    assert second["case_updated"].state == CaseState.INVESTIGATING
    assert reported["novel_files_uploaded"] == 1, reported
    assert reported["status_transitioned"] == 1, reported
    assert decisions, "denominator: no progress decision was taken"

    assert len(decisions) == 1, (
        f"the generation path must take ONE progress decision per turn; took "
        f"{len(decisions)}. A second, provisional one is the read-before-write "
        "hazard #1270 removed."
    )
    assert metadata["progress_made"] is True
    assert second["case_updated"].turns_without_progress == 0
    last = decisions[-1]
    late = {k: reported[k] for k in reported if reported[k] and not last.get(k)}
    assert not late, (
        f"arms written AFTER the final progress DECISION: {late}; "
        f"last decision saw {last}"
    )


@pytest.mark.asyncio
async def test_the_guard_survives_a_short_circuited_decision():
    """The monotone write short-circuits, and the guard must not mind.

    ``score_progress`` is ``already_true or predicate(...)``, so when a caller
    seeds ``progress_made=True`` the PREDICATE is never called. Every
    deterministic branch that passes ``progress_made=True`` does exactly that.

    A guard spying on the PREDICATE sees nothing on such a turn and either goes
    vacuous or reports the arms as written late — on code behaving exactly as
    designed. Wrapping the DECISION instead is what makes it robust, and the
    short-circuit is asserted here so this test cannot quietly stop covering it.
    """
    real_pred = me.check_if_progress_made
    predicate_calls: list[int] = []
    decisions: list[dict[str, int]] = []
    real_score = me.score_progress

    def counting_pred(metadata):
        predicate_calls.append(1)
        return real_pred(metadata)

    def recording_score(metadata):
        verdict = real_score(metadata)
        decisions.append(collect_progress_arms(metadata))
        return verdict

    engine = _terminal_confirm_engine()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(me, "check_if_progress_made", counting_pred)
        mp.setattr(me, "score_progress", recording_score)
        result = await engine.process_turn(
            case=_case_awaiting_confirmation("resolved", needs_info=False),
            user_message="yes, resolved",
            intent_type="status_transition",
            intent_data={"to_state": "resolved"},
        )

    assert result["case_updated"].state == CaseState.RESOLVED
    assert decisions, "denominator: no progress decision was taken"
    assert not predicate_calls, (
        "positive control: the monotone write did NOT short-circuit "
        f"({len(predicate_calls)} predicate calls), so this test is no longer "
        "covering the shape a predicate-spy guard gets wrong"
    )

    reported = result["metadata"][TELEMETRY_HANDOFF_KEY]["arms"]
    assert any(reported.values()), reported
    last = decisions[-1]
    late = {k: reported[k] for k in reported if reported[k] and not last.get(k)}
    assert not late, (
        f"arms written AFTER the final progress DECISION: {late}; "
        f"last decision saw {last}"
    )


@pytest.mark.asyncio
async def test_an_ordinary_turn_is_scored_on_the_same_ordering(engine_and_llm):
    """The guard is not specific to the transition turn.

    An INQUIRY turn that proposes a statement and transitions nothing has no arm
    to fire, so it must score ``False`` — the fix must not have made the reading
    unconditionally true.
    """
    engine, llm = engine_and_llm
    llm.payload = _TURN1
    result = await engine.process_turn(_inquiry_case(), "Our checkout API is 503ing")

    metadata = result["metadata"]
    assert result["case_updated"].state == CaseState.INQUIRY
    assert metadata["progress_made"] is False
    assert not any(metadata[TELEMETRY_HANDOFF_KEY]["arms"].values())
    assert result["case_updated"].turns_without_progress == 1


def _terminal_confirm_engine():
    """An engine whose only live path is a deterministic terminal confirm."""
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(return_value=None)
    engine = MilestoneEngine(_StubLLM(), repo, investigation_tools=MagicMock())
    engine._auto_generate_report = AsyncMock(return_value=(None, False))
    engine._remaining_regens_for = AsyncMock(return_value=1)
    # Proves the turn short-circuited rather than reaching generation.
    engine._generate_structured_output = AsyncMock(
        side_effect=AssertionError("reached the LLM; not the deterministic branch")
    )
    return engine


def _case_awaiting_confirmation(to_state: str, *, needs_info: bool):
    """An INVESTIGATING case with a standing terminal proposal.

    ``needs_info`` is the router: step 0b's confirm short-circuit is guarded by
    ``elif not needs_info``, so setting it sends the same click to the 0c
    dropdown handler instead. That is what lets one fixture drive BOTH confirm
    branches and compare them.
    """
    from datetime import UTC, datetime

    from faultmaven.modules.case.domain.models import (
        InvestigationProgress,
        ProblemVerification,
    )

    case = Case(
        case_id="case_1270bbbbbbbb",
        title="Confirmed terminal transition",
        state=CaseState.INQUIRY,
        user_id="user_123",
        organization_id="org_123",
        description="etcd connectivity",
        problem_verification=ProblemVerification(
            symptom_statement="recurring etcdInsufficientMembers alerts",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    pending = {
        "to_state": to_state,
        "summary": f"Shall we mark this {to_state}?",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    if to_state == "closed":
        # ``propose_transition`` derives and stores this; the executor reads it
        # unguarded, so a hand-built pending without it never reaches the branch
        # under test.
        pending["closure_reason"] = "solution_deferred"
    if needs_info:
        pending["needs_info"] = True
    case.pending_transition = pending
    return case


@pytest.mark.asyncio
async def test_both_confirm_branches_answer_a_resolve_identically():
    """One state change, one set of arms — whichever branch served the click.

    Two branches confirm a standing terminal proposal without an LLM call: the
    step-0b pending-transition short-circuit and the 0c status-transition
    dropdown. They disagreed twice about the SAME event — 0c wrote
    ``status_transitioned`` onto the outer working dict it never returns (so its
    row reported no transition), and they hand-wrote ``milestones_completed``
    differently (0c ``["solution_verified"]``, 0b none), so a consumer counting
    gate completions off the stream mis-counted by which affordance was used.

    Driving ONE branch cannot see that: it is a cross-branch agreement, so the
    test drives both and compares. The earlier version of this test drove 0c
    only — and would not have caught the milestone half at all.
    """
    engine_0b = _terminal_confirm_engine()
    result_0b = await engine_0b.process_turn(
        case=_case_awaiting_confirmation("resolved", needs_info=False),
        user_message="yes, resolved",
        intent_type="status_transition",
        intent_data={"to_state": "resolved"},
    )

    engine_0c = _terminal_confirm_engine()
    result_0c = await engine_0c.process_turn(
        case=_case_awaiting_confirmation("resolved", needs_info=True),
        user_message="yes, resolved",
        intent_type="status_transition",
        intent_data={"to_state": "resolved"},
    )

    # Denominators: BOTH branches ran, both reached RESOLVED, and they are
    # genuinely different branches — 0c is the one that composes its reply
    # through _auto_generate_report after the 0b guard has been routed past.
    for label, result in (("0b", result_0b), ("0c", result_0c)):
        assert result["case_updated"].state == CaseState.RESOLVED, label
    assert engine_0b._auto_generate_report.await_count == 1
    assert engine_0c._auto_generate_report.await_count == 1

    arms_0b = result_0b["metadata"][TELEMETRY_HANDOFF_KEY]["arms"]
    arms_0c = result_0c["metadata"][TELEMETRY_HANDOFF_KEY]["arms"]

    assert arms_0b == arms_0c, (
        "the two confirm branches report different arms for the same state "
        f"change: 0b={arms_0b} 0c={arms_0c}"
    )
    assert arms_0b["status_transitioned"] == 1
    assert arms_0b["milestones_completed"] == 1
    for label, result in (("0b", result_0b), ("0c", result_0c)):
        assert result["metadata"]["progress_made"] is True, label
        assert result["metadata"]["status_transitioned"] is True, label
        assert result["metadata"]["milestones_completed"] == [
            "solution_verified"
        ], label


@pytest.mark.asyncio
async def test_a_confirmed_close_does_not_claim_a_resolution_milestone():
    """``solution_verified`` is a RESOLUTION milestone, not a terminal one.

    The 0b branch serves closes as well as resolves. Making the two branches
    agree by giving both an unconditional ``["solution_verified"]`` would have
    manufactured a gate completion on every confirmed CLOSE — a case closed
    without a verified solution is precisely the case that milestone must not
    claim. So the arms are derived from where the transition LANDED.
    """
    engine = _terminal_confirm_engine()
    result = await engine.process_turn(
        case=_case_awaiting_confirmation("closed", needs_info=False),
        user_message="yes, close it",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
    )

    case = result["case_updated"]
    assert case.state == CaseState.CLOSED, "denominator: the close did not execute"
    metadata = result["metadata"]
    assert metadata["status_transitioned"] is True
    assert metadata["milestones_completed"] == []
    assert metadata[TELEMETRY_HANDOFF_KEY]["arms"]["status_transitioned"] == 1
    assert metadata[TELEMETRY_HANDOFF_KEY]["arms"]["milestones_completed"] == 0


def test_a_progress_true_already_on_the_dict_is_never_taken_back():
    """``_score_progress`` is monotone.

    ``check_if_progress_made`` reads the nine ARMS, never the ``progress_made``
    key, so a plain assignment destroys a ``True`` an earlier writer put there —
    and the generation path has one: ``_apply_stage_gate_side_effects`` sets it
    beside ``compliance_detected`` before the first reading.
    """
    engine = MilestoneEngine(_StubLLM(), MagicMock(), investigation_tools=MagicMock())

    metadata = {"progress_made": True}
    # Positive control: with no arm set the predicate says False, so a
    # non-monotone write would visibly clobber.
    assert check_if_progress_made(metadata) is False

    assert engine._score_progress(metadata) is True
    assert metadata["progress_made"] is True

    # And it does not invent progress on a dict that never claimed any.
    empty: dict = {}
    assert engine._score_progress(empty) is False
    assert empty["progress_made"] is False


@pytest.mark.parametrize("arm", sorted(PREDICATE_ARM_KEYS))
def test_every_scored_arm_on_its_own_means_progress(arm):
    """Any single fired arm implies progress.

    This is what makes the ordering guard above sufficient rather than merely
    necessary: if the final reading sees every fired arm, and any fired arm
    implies ``True``, then no emitted row can carry a fired arm beside
    ``progress_made: false``. Parametrised over the arm set the telemetry
    module derives from the predicate, so a new arm is covered on the day it is
    added.
    """
    from faultmaven.core.investigation.turn_outcome import TurnOutcome

    if arm == "outcome_progress":
        metadata = {"outcome": TurnOutcome.DATA_REQUESTED}
    elif arm in ("status_transitioned", "hypothesis_evidence_links_applied"):
        metadata = {arm: True}
    else:
        metadata = {arm: ["x"]}

    assert (
        collect_progress_arms(metadata)[arm] >= 1
    ), f"{arm} was not fired by the fixture"
    assert check_if_progress_made(metadata) is True, (
        f"{arm} is carried by the telemetry row but does not score as progress; "
        "a turn firing only this arm would emit a self-contradictory row"
    )
