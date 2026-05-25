"""Tests for the path-conditional emission backstop.

When the dispatcher renders ``_SYMPTOM_VALIDATION_BLOCK`` (pre-mitigation
MITIGATION_FIRST) or ``_GATE3_PENDING_BLOCK`` (Gate 3 pending), those
prompt blocks explicitly forbid two RCA-side structured emissions:

- ``hypotheses_to_add`` (categorical ban)
- ``evidence_to_add`` items with ``category=causal_evidence``

The engine enforces those bans at the ingestion sites in
``_apply_investigation_updates`` and re-surfaces violations via
``system_feedback`` so the next turn's prompt carries an explicit
correction signal. Prompt-only enforcement is fragile against capable
LLMs; the engine backstop makes the structural invariant robust against
non-compliance. Same architectural shape as Phase 1's INV-19
``RuntimeError`` and the ``mitigation_verified`` ordering guard.

Semantic distinction across the two emissions:
- ``hypotheses_to_add`` is REJECT-ALL (categorical ban — no "good"
  hypothesis emission in these states).
- ``causal_evidence`` is PARTIAL-ACCEPT (drop only the offending items;
  ``symptom_evidence`` / ``mitigation_evidence`` in the same response
  are accepted normally — don't punish good emissions).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _is_pre_mitigation_mitigation_first,
    _path_conditional_emission_restriction,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationPath,
    PathSelection,
    ProblemVerification,
)


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


def _mitigation_first_case(
    *,
    mitigation_completed_at_turn: int | None = None,
    rca_after_mitigation_confirmed: bool = False,
) -> Case:
    """INVESTIGATING-stage MITIGATION_FIRST case with controllable Gate 3
    state, plus the minimal scaffolding the engine requires for milestone
    progression (pending ProposedAction + symptom_evidence row).
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
        title="Emission backstop test",
        status=CaseStatus.INVESTIGATING,
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
        path_selection=PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="ongoing high impact",
            alternate_path=InvestigationPath.ROOT_CAUSE,
            selected_by="user_123",
            mitigation_completed_at_turn=mitigation_completed_at_turn,
            rca_after_mitigation_confirmed=rca_after_mitigation_confirmed,
        ),
    )
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.MITIGATION,
            description="Test mitigation action",
            proposed_in_turn=case.current_turn,
        )
    )
    # Reasoning validator requires evidence before allowing milestone
    # completion in some test paths; add a symptom_evidence row so any
    # follow-on milestone work in tests isn't blocked by reasoning
    # validation noise.
    case.evidence.append(
        Evidence(
            summary="Pre-existing symptom evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="Symptom verification",
            preprocessed_content="Pre-existing content",
            content_size_bytes=100,
            preprocessing_method="manual",
            collected_at_turn=case.current_turn,
        )
    )
    return case


def _root_cause_case() -> Case:
    """ROOT_CAUSE-path case — no path-conditional restrictions apply."""
    case = _mitigation_first_case()
    case.path_selection = case.path_selection.model_copy(
        update={"path": InvestigationPath.ROOT_CAUSE}
    )
    return case


def _llm_response(
    *,
    hypotheses: list[dict] | None = None,
    evidence: list[dict] | None = None,
    milestones: dict | None = None,
    agent_response: str = "ok",
) -> str:
    """Mock LLM JSON output with optional hypotheses, evidence, milestones."""
    state_updates: dict = {"outcome": "milestone_completed"}
    if hypotheses is not None:
        state_updates["hypotheses_to_add"] = hypotheses
    if evidence is not None:
        state_updates["evidence_to_add"] = evidence
    if milestones is not None:
        state_updates["milestones"] = milestones
    return json.dumps(
        {
            "agent_response": agent_response,
            "internal_reasoning": {
                "evidence_analyzed": ["ev_existing"],
                "conclusions": [],
                "milestone_justifications": (
                    {k: "auto" for k, v in (milestones or {}).items() if v}
                ),
            },
            "state_updates": state_updates,
        }
    )


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestStatePredicates:
    """The two helper predicates that gate the backstop. Pin behavior so
    a future refactor of path state can't silently shift enforcement."""

    def test_pre_mitigation_predicate_true_in_target_state(self):
        case = _mitigation_first_case(mitigation_completed_at_turn=None)
        assert _is_pre_mitigation_mitigation_first(case) is True

    def test_pre_mitigation_predicate_false_after_mitigation_verified(self):
        case = _mitigation_first_case(mitigation_completed_at_turn=5)
        assert _is_pre_mitigation_mitigation_first(case) is False

    def test_pre_mitigation_predicate_false_on_root_cause_path(self):
        case = _root_cause_case()
        assert _is_pre_mitigation_mitigation_first(case) is False

    def test_restriction_label_pre_mitigation(self):
        case = _mitigation_first_case(mitigation_completed_at_turn=None)
        assert (
            _path_conditional_emission_restriction(case)
            == "pre_mitigation_mitigation_first"
        )

    def test_restriction_label_gate3_pending(self):
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5, rca_after_mitigation_confirmed=False
        )
        assert _path_conditional_emission_restriction(case) == "gate3_pending"

    def test_restriction_label_none_post_gate3_rca(self):
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5, rca_after_mitigation_confirmed=True
        )
        assert _path_conditional_emission_restriction(case) is None

    def test_restriction_label_none_on_root_cause_path(self):
        """RCA path is unrestricted — the dispatcher renders
        ``_RCA_DIAGNOSIS_BLOCK`` which contains the hypothesis mandate."""
        case = _root_cause_case()
        assert _path_conditional_emission_restriction(case) is None


# ---------------------------------------------------------------------------
# hypotheses_to_add — reject-all in restricted states
# ---------------------------------------------------------------------------


class TestHypothesesRejection:
    """``hypotheses_to_add`` is categorically banned in restricted
    states. Reject the entire list; no partial-accept."""

    @pytest.mark.asyncio
    async def test_pre_mitigation_rejects_hypothesis_emission(
        self, mock_llm, mock_repo
    ):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)

        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "Test hypothesis about connection pool",
                    "category": "config",
                    "likelihood": 0.7,
                    "rationale": "Test rationale for hypothesis",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        # The hypothesis must not have landed on the case.
        assert len(updated.hypotheses) == 0
        # And TurnProgress should not record the (rejected) hypothesis.
        last_turn = updated.turn_history[-1]
        assert last_turn.hypotheses_generated == []

    @pytest.mark.asyncio
    async def test_gate3_pending_rejects_hypothesis_emission(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=False,  # Gate 3 still pending
        )

        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "RCA hypothesis the LLM jumped to early",
                    "category": "config",
                    "likelihood": 0.6,
                    "rationale": "Test rationale for Gate3 hypothesis",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert len(updated.hypotheses) == 0

    @pytest.mark.asyncio
    async def test_rejection_writes_system_feedback(self, mock_llm, mock_repo):
        """Reject + re-surface — the LLM gets explicit feedback for next turn."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)

        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "Forbidden hypothesis",
                    "category": "config",
                    "likelihood": 0.5,
                    "rationale": "Forbidden hypothesis rationale",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""

        assert "PATH-CONDITIONAL EMISSION ERROR" in feedback
        assert "hypotheses_to_add" in feedback
        # State-specific naming so the LLM knows WHICH block it violated
        assert "_SYMPTOM_VALIDATION_BLOCK" in feedback

    @pytest.mark.asyncio
    async def test_post_gate3_rca_allows_hypothesis_emission(self, mock_llm, mock_repo):
        """Post-Gate-3 (user opted to continue RCA), hypotheses are
        allowed again — the dispatcher renders ``_RCA_DIAGNOSIS_BLOCK``
        with the hypothesis mandate."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=True,  # Gate 3 resolved
        )

        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "Legitimate post-Gate-3 RCA hypothesis",
                    "category": "config",
                    "likelihood": 0.7,
                    "rationale": "Test rationale for hypothesis",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert len(updated.hypotheses) == 1

    @pytest.mark.asyncio
    async def test_root_cause_path_allows_hypothesis_emission(
        self, mock_llm, mock_repo
    ):
        """ROOT_CAUSE path is unrestricted — the backstop must not
        block legitimate hypothesis work."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _root_cause_case()

        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "Root-cause-path hypothesis",
                    "category": "config",
                    "likelihood": 0.7,
                    "rationale": "Test rationale for hypothesis",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert len(updated.hypotheses) == 1


# ---------------------------------------------------------------------------
# causal_evidence — partial-accept in restricted states
# ---------------------------------------------------------------------------


class TestCausalEvidenceRejection:
    """``causal_evidence`` is dropped from ``evidence_to_add`` in
    restricted states. Other categories in the same response are
    accepted normally — don't punish good emissions for accompanying
    bad ones."""

    @pytest.mark.asyncio
    async def test_pre_mitigation_drops_causal_evidence(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)
        evidence_before = len(case.evidence)

        mock_llm.generate.return_value = _llm_response(
            evidence=[
                {
                    "summary": "Causal evidence the LLM emitted in defiance of the prompt",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "Inline extract",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        # No new evidence was added.
        assert len(updated.evidence) == evidence_before

    @pytest.mark.asyncio
    async def test_pre_mitigation_partial_accept_keeps_symptom_evidence(
        self, mock_llm, mock_repo
    ):
        """If the response has [symptom_evidence, causal_evidence], the
        symptom_evidence is accepted, only the causal_evidence is
        dropped. Partial-accept semantics."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)
        evidence_before = len(case.evidence)

        mock_llm.generate.return_value = _llm_response(
            evidence=[
                {
                    "summary": "Good symptom evidence",
                    "category": "symptom_evidence",
                    "source_type": "user_description",
                    "extract": "Symptom observed",
                },
                {
                    "summary": "Forbidden causal evidence",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "Suspected cause",
                },
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        # Exactly one new evidence row — the symptom one
        assert len(updated.evidence) == evidence_before + 1
        new_ev = updated.evidence[-1]
        from faultmaven.modules.case.contracts import EvidenceCategory

        assert new_ev.category == EvidenceCategory.SYMPTOM_EVIDENCE

    @pytest.mark.asyncio
    async def test_causal_evidence_rejection_writes_system_feedback(
        self, mock_llm, mock_repo
    ):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)

        mock_llm.generate.return_value = _llm_response(
            evidence=[
                {
                    "summary": "Causal evidence",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "test",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""

        assert "PATH-CONDITIONAL EMISSION ERROR" in feedback
        assert "causal_evidence" in feedback
        assert "INV-17" in feedback  # cites the hypothesis-presupposes constraint

    @pytest.mark.asyncio
    async def test_gate3_pending_drops_causal_evidence(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=False,
        )
        evidence_before = len(case.evidence)

        mock_llm.generate.return_value = _llm_response(
            evidence=[
                {
                    "summary": "Causal evidence during Gate 3 pending",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "test",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert len(updated.evidence) == evidence_before

    @pytest.mark.asyncio
    async def test_post_gate3_rca_allows_causal_evidence(self, mock_llm, mock_repo):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=True,
        )
        # A hypothesis must exist for causal_evidence linking to make
        # sense in the broader audit trail. The backstop doesn't enforce
        # the hypothesis-presupposes rule here (INV-17 lives in
        # prompts), but the rejection should also not fire post-Gate-3.
        from faultmaven.modules.case.domain.models import (
            Hypothesis,
            HypothesisCategory,
            HypothesisGenerationMode,
        )

        case.hypotheses["hyp_aaaaaaaaaaab"] = Hypothesis(
            hypothesis_id="hyp_aaaaaaaaaaab",
            statement="Pre-existing hypothesis",
            category=HypothesisCategory.CONFIG,
            likelihood=0.5,
            rationale="Test rationale",
            generated_at_turn=case.current_turn,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        )
        evidence_before = len(case.evidence)

        mock_llm.generate.return_value = _llm_response(
            evidence=[
                {
                    "summary": "Post-Gate-3 causal evidence",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "test",
                }
            ]
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert len(updated.evidence) == evidence_before + 1


# ---------------------------------------------------------------------------
# root_cause_identified milestone — rejected in restricted states
# ---------------------------------------------------------------------------


class TestRootCauseIdentifiedRejection:
    """``root_cause_identified`` is an RCA-side milestone. Both
    ``_SYMPTOM_VALIDATION_BLOCK`` and ``_GATE3_PENDING_BLOCK`` forbid
    setting it. The pre-PR INV-21 guard covered the Gate-3-pending
    case only; this generalization extends the same enforcement to
    pre-mitigation MITIGATION_FIRST (where the prompt's
    ``DO NOT set root_cause_identified`` directive was previously
    prompt-only).
    """

    @pytest.mark.asyncio
    async def test_pre_mitigation_rejects_root_cause_identified(
        self, mock_llm, mock_repo
    ):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)

        mock_llm.generate.return_value = _llm_response(
            milestones={"root_cause_identified": True},
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        # The milestone was rejected — flag stays False
        assert updated.progress.root_cause_identified is False
        # And system_feedback names the violation
        feedback = updated.turn_history[-1].system_feedback or ""
        assert "PATH-CONDITIONAL MILESTONE ERROR" in feedback
        assert "root_cause_identified" in feedback
        assert "_SYMPTOM_VALIDATION_BLOCK" in feedback

    @pytest.mark.asyncio
    async def test_gate3_pending_rejects_root_cause_identified(
        self, mock_llm, mock_repo
    ):
        """Same rejection in Gate-3-pending state — was INV-21's
        original coverage, still works under the generalized predicate."""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=False,
        )

        mock_llm.generate.return_value = _llm_response(
            milestones={"root_cause_identified": True},
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        assert updated.progress.root_cause_identified is False
        feedback = updated.turn_history[-1].system_feedback or ""
        assert "PATH-CONDITIONAL MILESTONE ERROR" in feedback
        assert "_GATE3_PENDING_BLOCK" in feedback

    @pytest.mark.asyncio
    async def test_root_cause_path_does_not_fire_path_conditional_rejection(
        self, mock_llm, mock_repo
    ):
        """ROOT_CAUSE path is unrestricted — the path-conditional
        milestone rejection must NOT fire. (Whether the milestone
        ultimately persists depends on downstream
        ``validate_milestone_claims`` evidence-citation checks, which
        are orthogonal to this backstop. The test pins only that THIS
        backstop didn't reject — no PATH-CONDITIONAL MILESTONE ERROR
        in system_feedback.)"""
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _root_cause_case()
        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "ok",
                "internal_reasoning": {
                    "evidence_analyzed": ["ev_existing"],
                    "conclusions": [],
                    "milestone_justifications": {"root_cause_identified": "auto"},
                },
                "state_updates": {
                    "milestones": {
                        "root_cause_identified": True,
                        "root_cause_likelihood": 0.8,
                        "root_cause_method": "direct_analysis",
                    },
                    "outcome": "milestone_completed",
                },
            }
        )

        result = await engine.process_turn(case, "test")
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""

        # The backstop did not reject the milestone — no path-conditional
        # error message in system_feedback. (Downstream validators may
        # still revert the milestone for unrelated reasons; that's not
        # what this test guards.)
        assert "PATH-CONDITIONAL MILESTONE ERROR" not in feedback


# ---------------------------------------------------------------------------
# Combined emissions — both rejections fire AND accumulate in system_feedback
# ---------------------------------------------------------------------------


class TestCombinedEmissionsRejected:
    """When the LLM emits multiple forbidden things in one response,
    each rejection fires independently AND each error message accumulates
    in ``system_feedback``. Without this test, a future refactor that
    uses assignment (``=``) instead of append (``+=``-via-concat) on
    ``metadata["system_feedback"]`` would silently overwrite one error
    with another, and no test would catch it.
    """

    @pytest.mark.asyncio
    async def test_hypothesis_and_causal_evidence_both_rejected_with_both_feedbacks(
        self, mock_llm, mock_repo
    ):
        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        case = _mitigation_first_case(mitigation_completed_at_turn=None)
        evidence_before = len(case.evidence)

        # The LLM emits all three: 1 forbidden hypothesis, 1 forbidden
        # causal_evidence, 1 allowed symptom_evidence.
        mock_llm.generate.return_value = _llm_response(
            hypotheses=[
                {
                    "statement": "Forbidden hypothesis",
                    "category": "config",
                    "likelihood": 0.6,
                    "rationale": "test",
                }
            ],
            evidence=[
                {
                    "summary": "Forbidden causal evidence",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                    "extract": "test cause",
                },
                {
                    "summary": "Allowed symptom evidence",
                    "category": "symptom_evidence",
                    "source_type": "user_description",
                    "extract": "test symptom",
                },
            ],
        )

        result = await engine.process_turn(case, "test")
        updated = result["case_updated"]

        # Hypothesis dropped (REJECT-ALL).
        assert len(updated.hypotheses) == 0
        # Only the symptom_evidence landed (PARTIAL-ACCEPT).
        assert len(updated.evidence) == evidence_before + 1
        from faultmaven.modules.case.contracts import EvidenceCategory

        assert updated.evidence[-1].category == EvidenceCategory.SYMPTOM_EVIDENCE

        # Both error messages must appear in system_feedback — proves
        # that the per-site appends accumulate cumulatively rather
        # than the later site overwriting the earlier one.
        feedback = updated.turn_history[-1].system_feedback or ""
        # The evidence rejection cites causal_evidence
        assert "causal_evidence" in feedback
        # The hypothesis rejection cites hypotheses_to_add
        assert "hypotheses_to_add" in feedback
        # Both should carry the PATH-CONDITIONAL EMISSION ERROR marker
        # (one occurrence per rejection)
        assert feedback.count("PATH-CONDITIONAL EMISSION ERROR") == 2
