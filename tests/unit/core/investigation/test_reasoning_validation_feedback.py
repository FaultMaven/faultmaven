"""The reasoning-first strip reaches the model, and judges only new claims (fm#1677).

Two halves of one contract:

1. When ``validate_reasoning_first`` strips a milestone, the next turn's prompt
   says so. Before fm#1677 the errors reached only the log: Step 5.9b read a
   metadata key nothing wrote, so the model re-claimed the same milestone
   unjustified and was stripped again.
2. A milestone the case already records is a restatement, not a completion.
   The prompt asks for a justification only for a milestone the model
   CHANGES, and models restate standing booleans. Judging the restatement
   stripped a no-op, and once the strip was fed back it would have told the
   model an achieved milestone was rejected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _milestone_already_recorded,
    validate_reasoning_first,
)
from faultmaven.core.investigation.schemas import InternalReasoning, MilestoneUpdates
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationProgress,
    MitigationRecord,
    ProblemVerification,
)

# How the case records each LLM-settable milestone as reached. Written out
# here rather than read from the engine, so the test cannot agree with a wrong
# mapping; ``test_every_settable_milestone_has_a_recorded_state`` keeps it in
# step with the schema.
_RECORD = {
    "symptom_verified": lambda p: setattr(p, "symptom_verified", True),
    "solution_accepted": lambda p: setattr(p, "solution_accepted", True),
    "mitigation_accepted": lambda p: setattr(
        p, "mitigation", MitigationRecord(proposed_at_turn=1, accepted=True)
    ),
    "mitigation_verified": lambda p: setattr(
        p,
        "mitigation",
        MitigationRecord(proposed_at_turn=1, accepted=True, verified=True),
    ),
}

# The strict wire shape: every justification key present, null where silent.
_NO_JUSTIFICATIONS = {name: None for name in _RECORD}


def _response(milestones: MilestoneUpdates, justifications: dict | None):
    """Minimal duck-typed investigation response (not Inquiry/Terminal)."""
    ir = (
        None
        if justifications is None
        else InternalReasoning(
            milestone_justifications=justifications, evidence_analyzed=[]
        )
    )
    state_updates = SimpleNamespace(milestones=milestones, evidence_to_add=[])
    return SimpleNamespace(internal_reasoning=ir, state_updates=state_updates)


def _validator_case(progress: InvestigationProgress):
    return SimpleNamespace(
        state=CaseState.INVESTIGATING,
        is_terminal=False,
        pending_transition=None,
        progress=progress,
        evidence=["ev_1"],
        current_turn=5,
        case_id="case_test",
    )


class TestRestatementIsNotJudged:
    def test_every_settable_milestone_has_a_recorded_state(self):
        settable = {
            name
            for name, field in MilestoneUpdates.model_fields.items()
            if field.annotation == Optional[bool]
        }
        assert settable == set(_RECORD), (
            "a milestone was added to or removed from MilestoneUpdates; decide "
            "how the case records it here and in _milestone_already_recorded"
        )

    @pytest.mark.parametrize("milestone", sorted(_RECORD))
    def test_restated_milestone_without_justification_passes(self, milestone):
        progress = InvestigationProgress()
        _RECORD[milestone](progress)

        is_valid, errors, offending = validate_reasoning_first(
            _response(MilestoneUpdates(**{milestone: True}), _NO_JUSTIFICATIONS),
            _validator_case(progress),
        )

        assert (is_valid, errors, offending) == (True, [], set())

    @pytest.mark.parametrize("milestone", sorted(_RECORD))
    def test_the_same_milestone_newly_claimed_is_still_judged(self, milestone):
        """The other direction: an unrecorded milestone must not slip through."""
        is_valid, errors, offending = validate_reasoning_first(
            _response(MilestoneUpdates(**{milestone: True}), _NO_JUSTIFICATIONS),
            _validator_case(InvestigationProgress()),
        )

        assert is_valid is False
        assert offending == {milestone}

    def test_verification_is_judged_while_only_acceptance_is_recorded(self):
        """The mitigation record carries both gates; recording acceptance must
        not make a first claim of verification read as a restatement."""
        progress = InvestigationProgress()
        _RECORD["mitigation_accepted"](progress)

        is_valid, _, offending = validate_reasoning_first(
            _response(MilestoneUpdates(mitigation_verified=True), _NO_JUSTIFICATIONS),
            _validator_case(progress),
        )

        assert is_valid is False
        assert offending == {"mitigation_verified"}

    def test_an_unknown_milestone_reads_as_not_recorded(self):
        """Fail closed: a name the lookup does not know is still validated."""
        progress = InvestigationProgress(symptom_verified=True, solution_accepted=True)

        assert _milestone_already_recorded(progress, "not_a_milestone") is False

    def test_restatement_beside_a_new_claim_implicates_only_the_new_one(self):
        progress = InvestigationProgress(symptom_verified=True)

        is_valid, errors, offending = validate_reasoning_first(
            _response(
                MilestoneUpdates(symptom_verified=True, solution_accepted=True),
                _NO_JUSTIFICATIONS,
            ),
            _validator_case(progress),
        )

        assert is_valid is False
        assert offending == {"solution_accepted"}

    def test_restatement_alone_needs_no_internal_reasoning(self):
        progress = InvestigationProgress(symptom_verified=True)

        is_valid, _, offending = validate_reasoning_first(
            _response(MilestoneUpdates(symptom_verified=True), None),
            _validator_case(progress),
        )

        assert is_valid is True
        assert offending == set()


# ---------------------------------------------------------------------------
# The strip reaches the next turn's prompt
# ---------------------------------------------------------------------------


class _MockLLM(ILLMProvider):
    async def generate(self, prompt, **kwargs):
        return "{}"

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return "{}"

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "Test", "strict": True, "schema": schema},
            },
        )


@pytest.fixture
def mock_llm():
    llm = _MockLLM()
    llm.generate = AsyncMock()
    return llm


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    return repo


def _investigating_case() -> Case:
    """An INVESTIGATING case carrying a symptom evidence row, so the only
    reason the validator can reject a claim is a missing justification."""
    case = Case(
        case_id="case_aaaaaaaa1677",
        title="Reasoning feedback test",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        enterprise_id="org_123",
        description="billing-exporter fails to start on metrics-01",
        problem_verification=ProblemVerification(
            symptom_statement="billing-exporter fails to start",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            thread_id="thread_123",
            proposed_problem_statement="billing-exporter fails to start",
        ),
    )
    case.evidence.append(
        Evidence(
            summary="systemd reports status=203/EXEC for billing-exporter",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="Symptom verification",
            preprocessed_content="status=203/EXEC",
            content_size_bytes=15,
            preprocessing_method="manual",
            collected_at_turn=case.current_turn,
        )
    )
    return case


def _claims_symptom_verified_unjustified() -> str:
    return json.dumps(
        {
            "agent_response": "The service fails at exec.",
            "internal_reasoning": {
                "evidence_analyzed": [],
                "conclusions": [],
                "milestone_justifications": _NO_JUSTIFICATIONS,
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "outcome": "milestone_completed",
            },
        }
    )


_NOTICE = "Milestones ['symptom_verified'] were NOT recorded"


class TestStripReachesNextPrompt:
    @pytest.mark.asyncio
    async def test_stripped_milestone_is_named_in_the_next_prompt(
        self, mock_llm, mock_repo
    ):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())

        mock_llm.generate.return_value = _claims_symptom_verified_unjustified()
        first = await engine.process_turn(_investigating_case(), "it still fails")
        case = first["case_updated"]

        assert case.progress.symptom_verified is False, "the claim was stripped"
        # At the head of the record: the record truncates from the tail.
        assert case.turn_history[-1].system_feedback.startswith(
            f"REASONING VALIDATION: {_NOTICE}"
        )

        mock_llm.generate.reset_mock()
        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "Let's check the unit file.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        await engine.process_turn(case, "what next?")

        prompts = [c.kwargs["prompt"] for c in mock_llm.generate.call_args_list]
        assert prompts, "the second turn made no LLM call"
        assert any(_NOTICE in p for p in prompts)

    @pytest.mark.asyncio
    async def test_restated_milestone_produces_no_feedback(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _investigating_case()
        case.progress.symptom_verified = True

        mock_llm.generate.return_value = _claims_symptom_verified_unjustified()
        result = await engine.process_turn(case, "it still fails")
        updated = result["case_updated"]

        assert updated.progress.symptom_verified is True
        assert "REASONING VALIDATION" not in (
            updated.turn_history[-1].system_feedback or ""
        )
