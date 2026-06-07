"""Tests for stage-gate milestone-ordering rejection in
``_apply_investigation_updates``.

Pins the engine-side rejection of ``stabilization_verified=True`` when the
stabilization has not yet been accepted. Without this guard, the
out-of-order milestone update would crash the
``StabilizationRecord._verified_requires_accepted`` validator with a
500 — the turn is lost and the case enters a broken state.

Post-redesign (unified opportunistic flow): the LLM still emits the
``stabilization_accepted`` / ``stabilization_verified`` schema signals, but the
engine routes them into ``progress.stabilization`` (a forward-only
record) rather than progress booleans. The ordering guard lives at the
``stabilization.verified`` materialization step.

Design choice: REJECT + re-surface via ``system_feedback`` so the LLM
gets explicit feedback to retry with the prerequisite in place. NOT
auto-promote (same antipattern as the deleted
``diagnostic_reasoning_validator`` — "silently fix the LLM" hides
compliance signals).

If the LLM emits BOTH signals in the same response,
``stabilization_accepted`` is processed first, so ``stabilization_verified``
passes the check naturally.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
)


def _stab_accepted(case) -> bool:
    """Read the redesign-model equivalent of the old
    ``progress.stabilization_accepted`` boolean."""
    stab = case.progress.stabilization
    return bool(stab is not None and stab.accepted)


def _stab_verified(case) -> bool:
    stab = case.progress.stabilization
    return bool(stab is not None and stab.verified)


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


def _stabilization_case() -> Case:
    """INVESTIGATING case with a pending stabilization ProposedAction
    (required for the stabilization-signal guard to pass), a
    symptom_evidence row (so the reasoning validator allows milestone
    completion), and no stabilization record yet.
    """
    from datetime import UTC, datetime

    from faultmaven.modules.case.domain.models import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        InvestigationActionType,
        ProposedAction,
    )

    case = Case(
        case_id="case_aaaaaaaaaaab",
        title="Order-rejection test",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )
    # The stage-gate-accepted guard rejects milestone updates if no
    # pending ProposedAction exists. Provide one so the guard passes
    # and the rejection we're testing is specifically the ordering one.
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.STABILIZATION,
            description="Test stabilization action",
            proposed_in_turn=case.current_turn,
        )
    )
    # The reasoning validator at _process_response_structured rejects
    # milestone-completing responses when no evidence has been collected
    # ("Cannot complete milestones when no actionable evidence...").
    # Add a symptom_evidence row so reasoning validation passes and we
    # exercise the milestone-ordering rejection downstream.
    case.evidence.append(
        Evidence(
            summary="Test symptom evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="Symptom verification",
            preprocessed_content="Test content",
            content_size_bytes=100,
            preprocessing_method="manual",
            collected_at_turn=case.current_turn,
        )
    )
    return case


def _llm_response_setting_milestones(
    milestones: dict, agent_response: str = "ok"
) -> str:
    """Mock LLM JSON output that sets the given stage-gate milestones."""
    return json.dumps(
        {
            "agent_response": agent_response,
            "internal_reasoning": {
                "evidence_analyzed": [],
                "conclusions": [],
                "milestone_justifications": {
                    field: "auto" for field in milestones if milestones[field]
                },
            },
            "state_updates": {
                "milestones": milestones,
                "outcome": "milestone_completed",
            },
        }
    )


# ---------------------------------------------------------------------------
# Rejection path — the bug fix
# ---------------------------------------------------------------------------


class TestRejectionWhenPrerequisiteMissing:
    """When the LLM emits ``stabilization_verified=True`` without
    ``stabilization_accepted`` already True, the engine rejects the update,
    appends the violation to ``system_feedback`` for the next turn, and
    does NOT crash the save."""

    @pytest.mark.asyncio
    async def test_stabilization_verified_alone_is_rejected(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()
        # Precondition: no stabilization yet
        assert _stab_accepted(case) is False
        assert _stab_verified(case) is False

        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_verified": True}
        )

        # Should NOT raise. Previously this crashed at save with
        # "Cannot verify stabilization without acceptance first".
        result = await engine.process_turn(case, "test message")
        updated = result["case_updated"]

        # Verified is rejected because accept was never signalled. With no
        # accept signal either, no stabilization record is materialized at
        # all — nothing verified, no completed_at_turn boundary stamped.
        assert _stab_verified(updated) is False
        if updated.progress.stabilization is not None:
            assert updated.progress.stabilization.completed_at_turn is None

    @pytest.mark.asyncio
    async def test_rejection_writes_system_feedback_for_next_turn(
        self, mock_llm, mock_repo
    ):
        """The whole point of REJECT-not-AUTO-PROMOTE is to give the
        LLM a feedback signal. The engine records system_feedback on
        the turn's ``TurnProgress`` so context_builder can inject it
        into the next turn's prompt. Pin both that the feedback is
        recorded and that it carries the actionable error message.
        """
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()

        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_verified": True}
        )

        result = await engine.process_turn(case, "test message")
        # system_feedback flows through TurnProgress, not through the
        # returned metadata payload (which only carries turn-summary
        # signals for the API layer).
        turn_record = result["case_updated"].turn_history[-1]
        feedback = turn_record.system_feedback or ""

        # The exact phrasing is policy — we pin the load-bearing pieces:
        assert "MILESTONE ORDER ERROR" in feedback
        assert "stabilization_verified" in feedback
        assert "stabilization_accepted" in feedback
        # The retry instruction (the actionable part)
        assert "BOTH" in feedback or "first" in feedback

    @pytest.mark.asyncio
    async def test_save_does_not_crash_after_rejection(self, mock_llm, mock_repo):
        """The original bug: the engine applied side effects, then state
        validation flagged the error (non-blocking warning only), then
        save's Pydantic validator crashed with 500. The rejection should
        prevent the side effect from firing, so save's validator has
        nothing to reject.
        """
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()
        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_verified": True}
        )
        # The fixture mock_repo.save would surface a ValidationError if the
        # case were invalid (the real SQLite repo calls Case.model_validate).
        # Here we just ensure no exception bubbles up from process_turn.
        result = await engine.process_turn(case, "test message")
        assert mock_repo.save.called  # save did get attempted
        assert "case_updated" in result


# ---------------------------------------------------------------------------
# Pass-through paths — these must NOT be rejected
# ---------------------------------------------------------------------------


class TestPrerequisiteSatisfiedPasses:
    """Three shapes that satisfy the prerequisite must reach the setattr:
    1. Both milestones in same response (iteration order handles it)
    2. stabilization_accepted already True from a prior turn
    3. stabilization_accepted-only update (no ordering issue)
    """

    @pytest.mark.asyncio
    async def test_both_milestones_same_response_both_set(self, mock_llm, mock_repo):
        """If the LLM emits ``stabilization_accepted=True`` AND
        ``stabilization_verified=True`` in one response, the iteration
        order processes ``stabilization_accepted`` first, so the
        ``stabilization_verified`` check sees the prerequisite met.
        Both get set."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()
        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_accepted": True, "stabilization_verified": True}
        )

        result = await engine.process_turn(case, "applied + verified")
        updated = result["case_updated"]

        assert _stab_accepted(updated) is True
        assert _stab_verified(updated) is True
        # Side effects ran: stabilization completion boundary stamped
        assert updated.progress.stabilization.completed_at_turn is not None

    @pytest.mark.asyncio
    async def test_verified_alone_succeeds_when_accepted_already_true(
        self, mock_llm, mock_repo
    ):
        """When ``stabilization_accepted`` was set on a prior turn, a
        verified-only update on a later turn passes the ordering check
        cleanly — no rejection, no system_feedback noise."""
        from faultmaven.modules.case.domain.models import StabilizationRecord

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()
        # Accept signalled on a prior turn -> stabilization already accepted.
        case.progress.stabilization = StabilizationRecord(
            proposed_at_turn=case.current_turn, accepted=True
        )
        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_verified": True}
        )

        result = await engine.process_turn(case, "verified")
        updated = result["case_updated"]

        assert _stab_verified(updated) is True
        # No rejection feedback should have been added.
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""
        assert "MILESTONE ORDER ERROR" not in feedback

    @pytest.mark.asyncio
    async def test_accepted_alone_is_not_affected(self, mock_llm, mock_repo):
        """The ordering check is specifically about ``stabilization_verified``.
        Setting just ``stabilization_accepted`` must pass through normally.
        """
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _stabilization_case()
        mock_llm.generate.return_value = _llm_response_setting_milestones(
            {"stabilization_accepted": True}
        )

        result = await engine.process_turn(case, "accepted")
        updated = result["case_updated"]

        assert _stab_accepted(updated) is True
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""
        assert "MILESTONE ORDER ERROR" not in feedback
