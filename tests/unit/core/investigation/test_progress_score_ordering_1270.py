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

    Every reading taken during the turn is recorded with the arms visible to it
    at that moment. The LAST one is the one that lands in ``progress_made``, so
    it must have seen the same arms the emitted row reports — otherwise some
    writer ran after the decision, which is #1270 whatever the key is called.

    **Denominators first, again.** ``late`` is empty both when nothing was
    written late and when the row reports no arms at all, so the count of fired
    arms in the row is asserted before the comparison — otherwise a turn that
    fired nothing would satisfy this on unfixed code.
    """
    engine, llm = engine_and_llm
    seen: list[dict[str, int]] = []
    real = check_if_progress_made

    def recording(metadata):
        seen.append(collect_progress_arms(metadata))
        return real(metadata)

    monkeypatch.setattr(me, "check_if_progress_made", recording)

    result = await _two_turn_transition(engine, llm)
    reported = result["metadata"][TELEMETRY_HANDOFF_KEY]["arms"]

    assert seen, "denominator: the predicate was never called on this turn"
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


@pytest.mark.asyncio
async def test_both_dropdown_confirm_branches_report_the_transition_arm():
    """The arm must be on the dict a deterministic branch RETURNS.

    Two branches confirm a standing terminal proposal without an LLM call: the
    pending-transition short-circuit, and the status-transition dropdown. Both
    transition the case, so both must report ``status_transitioned`` — the
    dropdown one wrote it onto the OUTER working dict, which it never returns,
    so its row said the case advanced with no transition recorded while its
    sibling's said the opposite about the same event.
    """
    from datetime import UTC, datetime

    from faultmaven.modules.case.domain.models import (
        InvestigationProgress,
        ProblemVerification,
    )

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

    case = Case(
        case_id="case_1270bbbbbbbb",
        title="Dropdown-confirmed resolution",
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
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "Shall we mark this resolved?",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
        # ``needs_info`` is what routes this turn PAST the step-0b confirm
        # short-circuit (its guard is ``elif not needs_info``) and into the 0c
        # dropdown handler — the branch under test. Without it 0b answers the
        # click, and 0b already passes the arm, so the test would pass on both
        # sides of the fix while exercising the wrong branch.
        "needs_info": True,
    }

    result = await engine.process_turn(
        case=case,
        user_message="yes, resolved",
        intent_type="status_transition",
        intent_data={"to_state": "resolved"},
    )

    # Positive controls: the case transitioned, AND it did so on the dropdown
    # branch — which is the one that composes a terminal reply through
    # ``_auto_generate_report``.
    assert result["case_updated"].state == CaseState.RESOLVED
    assert engine._auto_generate_report.await_count == 1
    metadata = result["metadata"]
    assert metadata["progress_made"] is True
    assert metadata["status_transitioned"] is True
    assert metadata[TELEMETRY_HANDOFF_KEY]["arms"]["status_transitioned"] == 1


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
