"""Integration tests for MilestoneEngine investigation lifecycle (Gap #13).

Tests the complete investigation lifecycle through MilestoneEngine with
InMemoryCaseRepository and mocked LLM responses, validating:
- Multi-turn INQUIRY → INVESTIGATING → RESOLVED lifecycle
- Concurrent process_turn locking
- Evidence accumulation across turns
- Checkpoint creation at status transitions
- Explicit status transitions via intent_type
- Terminal transitions via User-Agent Handshake

These are integration tests because they exercise the full MilestoneEngine
processing pipeline (prompt generation, response processing, state transitions,
progress tracking) with a real repository, only mocking the external LLM.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.checkpoint_service import CheckpointService
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    InquiryResponse,
    InternalReasoning,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Treatment,
    MilestoneUpdates,
    PreliminaryUrgency,
    ProblemConfirmation,
    ProposedTransition,
    TurnOutcome,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.modules.case.contracts import (
    CaseStatus,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)
from faultmaven.modules.case.domain.models import (
    Case,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

# ============================================================
# Helpers
# ============================================================


def _make_inquiry_case(**overrides) -> Case:
    """Create a Case in INQUIRY status with sensible defaults."""
    defaults = {
        "user_id": "test-user-123",
        "organization_id": "test-org-456",
        "title": "API latency spike in production",
        "description": "",
        "status": CaseStatus.INQUIRY,
        "current_turn": 0,
    }
    defaults.update(overrides)
    return Case(**defaults)


def _make_investigating_case(**overrides) -> Case:
    """Create a Case in INVESTIGATING status with required fields.

    Pydantic validates INVESTIGATING requires confirmed problem statement
    and investigation commitment at construction time, so we pass a
    pre-configured InquiryData in the constructor.
    """
    description = overrides.pop(
        "description",
        "API experiencing latency spikes in production with p99 > 5s",
    )
    defaults = {
        "user_id": "test-user-123",
        "organization_id": "test-org-456",
        "title": "API latency spike in production",
        "description": description,
        "status": CaseStatus.INVESTIGATING,
        "current_turn": 3,
        "inquiry": InquiryData(
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=datetime.now(UTC),
            decided_to_investigate=True,
            proposed_problem_statement=description,
        ),
        "problem_verification": ProblemVerification(
            symptom_statement=description,
            severity="LOW",
        ),
    }
    defaults.update(overrides)
    return Case(**defaults)


def _mock_llm_provider():
    """Create an AsyncMock LLM provider with structured output support."""
    provider = AsyncMock()
    provider.provider_name = "mock"
    provider.config = MagicMock()
    provider.config.default_model = "mock-model"
    # get_structured_output_strategy is a sync method, not async
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.BEST_EFFORT,
            mode=StructuredOutputMode.JSON_OBJECT,
            include_schema_in_prompt=True,
            response_format={"type": "json_object"},
            extra_config={},
        )
    )
    return provider


# --- LLM Response Factories ---


def _inquiry_response_low_urgency() -> InquiryResponse:
    """Inquiry response with LOW urgency (no auto-confirm, stays in INQUIRY)."""
    return InquiryResponse(
        agent_response=(
            "I understand you're experiencing API latency spikes in production. "
            "Can you share the relevant logs so I can take a closer look?"
        ),
        state_updates=InquiryResponse.InquiryStateUpdate(
            problem_confirmation=ProblemConfirmation(
                problem_type="slowness",
                severity_guess="medium",
                preliminary_guidance="API latency spike affecting production, likely related to recent deployment",
            ),
            proposed_problem_statement="API experiencing latency spikes in production with p99 response times exceeding 5 seconds",
            preliminary_urgency=PreliminaryUrgency(
                level="LOW",
                is_ongoing=False,
                impact_assessment="Intermittent issue, investigating root cause",
            ),
        ),
    )


def _inquiry_response_high_urgency() -> InquiryResponse:
    """Inquiry response with HIGH urgency + ongoing — presents problem, waits for confirmation."""
    return InquiryResponse(
        agent_response=(
            "This is an urgent production issue. Let me confirm my understanding: "
            "API is experiencing critical latency spikes in production. Is this accurate?"
        ),
        state_updates=InquiryResponse.InquiryStateUpdate(
            problem_confirmation=ProblemConfirmation(
                problem_type="slowness",
                severity_guess="high",
                preliminary_guidance="Critical API latency spike affecting all production traffic",
            ),
            proposed_problem_statement="API experiencing critical latency spikes in production with p99 > 5s affecting all users",
            preliminary_urgency=PreliminaryUrgency(
                level="HIGH",
                is_ongoing=True,
                is_incident_report=True,
                impact_assessment="All production traffic affected with degraded user experience",
            ),
        ),
    )


def _inquiry_response_user_confirms() -> InquiryResponse:
    """Inquiry response where user confirmed the problem statement."""
    return InquiryResponse(
        agent_response=("Confirmed. Starting investigation into API latency spikes."),
        state_updates=InquiryResponse.InquiryStateUpdate(
            user_confirmed_investigation=True,
        ),
    )


def _inquiry_response_with_evidence() -> InquiryResponse:
    """Inquiry response: user provides log data → evidence created."""
    return InquiryResponse(
        agent_response=(
            "I see the error patterns in your logs. The connection pool exhaustion "
            "correlates with the deployment time."
        ),
        state_updates=InquiryResponse.InquiryStateUpdate(
            evidence_to_add=[
                EvidenceToAdd(
                    summary="Application logs showing connection pool exhaustion after v2.1.3 deployment",
                    category=EvidenceCategory.SYMPTOM_EVIDENCE,
                    source_type=EvidenceSourceType.LOGS,
                    content_ref="2026-02-15 14:03:21 ERROR [pool] Connection pool exhausted: max=50, active=50, waiting=127",
                ),
            ],
        ),
    )


def _investigation_verification_response() -> InvestigationResponse_Diagnosis:
    """Investigation response with milestone progress (verification stage)."""
    return InvestigationResponse_Diagnosis(
        agent_response=(
            "Based on the logs, I can confirm the symptoms. The connection pool "
            "is being exhausted due to leaked connections from the new deployment. "
            "I've verified the symptoms and assessed the scope."
        ),
        internal_reasoning=InternalReasoning(
            evidence_analyzed=["ev_placeholder"],
            milestone_justifications={
                "symptom_verified": "Connection pool exhaustion confirmed via log evidence",
            },
        ),
        state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            milestones=MilestoneUpdates(
                symptom_verified=True,
            ),
            evidence_to_add=[
                EvidenceToAdd(
                    summary="Metrics data showing p99 latency at 5.2s (normal: 200ms)",
                    category=EvidenceCategory.SYMPTOM_EVIDENCE,
                    source_type=EvidenceSourceType.METRICS,
                    content_ref="p99=5200ms, p95=3100ms, error_rate=12.5%",
                ),
            ],
            outcome=TurnOutcome.MILESTONE_COMPLETED,
        ),
    )


def _investigation_propose_resolved_response() -> InvestigationResponse_Treatment:
    """Investigation response: agent proposes resolution."""
    return InvestigationResponse_Treatment(
        agent_response=(
            "The root cause has been identified: a connection leak in the new "
            "database connection pool configuration. Rolling back fixed the issue."
        ),
        internal_reasoning=InternalReasoning(
            evidence_analyzed=["ev_placeholder"],
            milestone_justifications={
                "root_cause_identified": "Connection pool config change in v2.1.3 caused leak",
                "solution_proposed": "Rollback connection pool settings",
                "solution_accepted": "Configuration reverted; p99 returned to normal",
            },
        ),
        state_updates=InvestigationResponse_Treatment.TreatmentStateUpdate(
            milestones=MilestoneUpdates(
                root_cause_identified=True,
                solution_proposed=True,
                solution_accepted=True,
            ),
            proposed_transition=ProposedTransition(
                to_status="resolved",
                reason="Root cause identified and fix applied.",
                summary="Connection pool config issue fixed by rolling back.",
                evidence_ids=[],
            ),
            outcome=TurnOutcome.CASE_RESOLVED,
        ),
    )


def _investigation_no_progress_response() -> InvestigationResponse_Diagnosis:
    """Investigation response with no milestone progress (conversation only)."""
    return InvestigationResponse_Diagnosis(
        agent_response="Can you provide more details about the issue?",
        state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            outcome=TurnOutcome.CONVERSATION,
        ),
    )


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def case_repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def checkpoint_service(case_repo) -> CheckpointService:
    return CheckpointService(case_repo=case_repo)


@pytest.fixture
def mock_llm() -> AsyncMock:
    return _mock_llm_provider()


@pytest.fixture
def engine(mock_llm, case_repo, checkpoint_service) -> MilestoneEngine:
    return MilestoneEngine(
        llm_provider=mock_llm,
        repository=case_repo,
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
        knowledge_service=None,
        trace_enabled=False,
        checkpoint_service=checkpoint_service,
    )


# ============================================================
# Test: INQUIRY → INVESTIGATING → RESOLVED lifecycle
# ============================================================


@pytest.mark.asyncio
class TestInvestigationLifecycle:
    """Multi-turn lifecycle tests."""

    async def test_inquiry_turn_extracts_problem_statement(self, engine, case_repo):
        """INQUIRY turn: agent extracts problem statement from user description."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            result = await engine.process_turn(
                case, "Our API is experiencing high latency"
            )

        updated = result["case_updated"]
        # LOW urgency, not ongoing → stays in INQUIRY
        assert updated.status == CaseStatus.INQUIRY
        assert updated.inquiry.proposed_problem_statement is not None
        assert "latency" in updated.inquiry.proposed_problem_statement.lower()
        assert updated.inquiry.problem_confirmation is not None
        assert updated.inquiry.problem_confirmation.problem_type == "slowness"
        assert len(result["agent_response"]) > 0

    async def test_inquiry_evidence_created_from_submitted_data(
        self, engine, case_repo
    ):
        """INQUIRY turn with submitted_data → evidence record created."""
        case = _make_inquiry_case(current_turn=2)
        case.inquiry.proposed_problem_statement = "API latency spikes in production"
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_with_evidence(),
        ):
            result = await engine.process_turn(
                case,
                "Here are the logs: 2026-02-15 ERROR [pool] Connection pool exhausted",
            )

        updated = result["case_updated"]
        assert len(updated.evidence) >= 1
        ev = updated.evidence[0]
        assert ev.category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert ev.source_type == EvidenceSourceType.LOGS
        # Engine-created evidence from evidence_to_add uses SUBMITTED_DATA
        # (evidence derived from agent/LLM processing, not direct user input).
        assert ev.form == EvidenceForm.SUBMITTED_DATA

    async def test_explicit_transition_to_investigating(self, engine, case_repo):
        """Explicit intent_type='status_transition' routes through normal INQUIRY flow.

        Design: Dropdown = message. The dropdown does NOT bypass the agent.
        Instead, it injects a pre-composed message and lets the LLM handle
        the multi-turn problem statement flow. When the LLM confirms the
        user's intent, the transition fires through _check_automatic_transitions.
        """
        case = _make_inquiry_case(current_turn=3)
        case.inquiry.proposed_problem_statement = "API latency spikes with p99 > 5s"
        await case_repo.save(case)

        # LLM sees the user wants to investigate and confirms (sets user_confirmed_investigation=True)
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_user_confirms(),
        ):
            result = await engine.process_turn(
                case,
                "Let's investigate this",
                intent_type="status_transition",
                intent_data={"to_status": "investigating", "from_status": "inquiry"},
            )

        updated = result["case_updated"]
        assert updated.status == CaseStatus.INVESTIGATING
        assert updated.description != ""
        assert updated.progress is not None
        assert updated.problem_verification is not None
        assert updated.path_selection is not None
        assert any(
            t.to_status == CaseStatus.INVESTIGATING for t in updated.action_history
        )

    async def test_investigation_turn_with_milestone_progress(self, engine, case_repo):
        """INVESTIGATING turn: milestones completed, evidence added, progress tracked."""
        case = _make_investigating_case(current_turn=4)
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_verification_response(),
        ):
            result = await engine.process_turn(case, "Here are the metrics: p99=5200ms")

        updated = result["case_updated"]
        assert updated.status == CaseStatus.INVESTIGATING
        assert updated.progress.symptom_verified is True
        assert len(updated.evidence) >= 1
        # Engine-created evidence from evidence_to_add uses SUBMITTED_DATA
        ev = updated.evidence[0]
        assert ev.form == EvidenceForm.SUBMITTED_DATA
        assert len(updated.turn_history) >= 1
        assert result["metadata"]["progress_made"] is True

    async def test_full_lifecycle_inquiry_to_resolved(self, engine, case_repo):
        """Complete lifecycle: INQUIRY → INVESTIGATING → RESOLVED."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        # === Turn 1: User describes problem (LOW urgency, stays in INQUIRY) ===
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            result = await engine.process_turn(
                case, "Our API is experiencing high latency"
            )
        case = result["case_updated"]
        assert case.status == CaseStatus.INQUIRY

        # === Turn 2: Transition to INVESTIGATING via explicit intent ===
        # Design: Dropdown routes through normal INQUIRY flow. LLM confirms investigation.
        case.current_turn = 2
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_user_confirms(),
        ):
            result = await engine.process_turn(
                case,
                "Let's investigate",
                intent_type="status_transition",
                intent_data={"to_status": "investigating", "from_status": "inquiry"},
            )
        case = result["case_updated"]
        assert case.status == CaseStatus.INVESTIGATING

        # === Turn 3: Investigation with milestone progress ===
        case.current_turn = 3
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_verification_response(),
        ):
            result = await engine.process_turn(case, "Here are the metrics: p99=5200ms")
        case = result["case_updated"]
        assert case.progress.symptom_verified is True

        # === Turn 4: Agent proposes resolution ===
        case.current_turn = 4
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_propose_resolved_response(),
        ):
            result = await engine.process_turn(
                case, "I rolled back the config and p99 is back to 200ms"
            )
        case = result["case_updated"]
        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "resolved"

        # === Turn 5: User confirms resolution via explicit intent ===
        case.current_turn = 5
        result = await engine.process_turn(
            case,
            "yes",
            intent_type="status_transition",
            intent_data={"to_status": "resolved", "from_status": "investigating"},
        )
        case = result["case_updated"]
        assert case.status == CaseStatus.RESOLVED
        assert case.is_terminal is True

        # Verify persistence
        persisted = await case_repo.get(case.case_id)
        assert persisted is not None
        assert persisted.status == CaseStatus.RESOLVED

    async def test_high_urgency_stays_inquiry_until_confirmed(self, engine, case_repo):
        """HIGH urgency + ongoing stays INQUIRY → user confirms → transitions to INVESTIGATING."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        # Turn 1: HIGH urgency + is_ongoing=True → stays in INQUIRY (waits for confirmation)
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_high_urgency(),
        ):
            result1 = await engine.process_turn(case, "Our API is down in production!")

        updated1 = result1["case_updated"]
        assert updated1.status == CaseStatus.INQUIRY
        assert updated1.inquiry.problem_statement_confirmed is False
        assert updated1.inquiry.decided_to_investigate is False

        # Turn 2: User confirms → transitions to INVESTIGATING
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_user_confirms(),
        ):
            result2 = await engine.process_turn(
                updated1, "Yes, that's correct. Please investigate."
            )

        updated2 = result2["case_updated"]
        assert updated2.status == CaseStatus.INVESTIGATING
        assert updated2.inquiry.problem_statement_confirmed is True
        assert updated2.inquiry.decided_to_investigate is True

    async def test_close_from_inquiry_via_intent(self, engine, case_repo):
        """Close case from INQUIRY via dropdown proposes pending transition."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        result = await engine.process_turn(
            case,
            "Close this case",
            intent_type="status_transition",
            intent_data={"to_status": "closed", "from_status": "inquiry"},
        )

        updated = result["case_updated"]
        # Should propose CLOSED, not execute immediately
        assert updated.status == CaseStatus.INQUIRY
        assert updated.pending_transition is not None
        assert updated.pending_transition["to_status"] == "closed"

    async def test_abandon_investigation_via_intent(self, engine, case_repo):
        """Close INVESTIGATING case via dropdown proposes pending transition."""
        case = _make_investigating_case(current_turn=5)
        await case_repo.save(case)

        result = await engine.process_turn(
            case,
            "Close without resolution",
            intent_type="status_transition",
            intent_data={"to_status": "closed", "from_status": "investigating"},
        )

        updated = result["case_updated"]
        # Should propose CLOSED, not execute immediately
        assert updated.status == CaseStatus.INVESTIGATING
        assert updated.pending_transition is not None
        assert updated.pending_transition["to_status"] == "closed"


# ============================================================
# Test: Checkpointing
# ============================================================


@pytest.mark.asyncio
class TestCheckpointing:
    """Verify checkpoint creation at status transitions."""

    async def test_checkpoint_created_on_transition_to_investigating(
        self, engine, case_repo, checkpoint_service
    ):
        """Checkpoint created before INQUIRY → INVESTIGATING transition."""
        case = _make_inquiry_case(current_turn=2)
        case.inquiry.proposed_problem_statement = "API latency spike"
        await case_repo.save(case)

        # Design: Dropdown routes through normal INQUIRY flow. LLM confirms investigation.
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_user_confirms(),
        ):
            result = await engine.process_turn(
                case,
                "Investigate this",
                intent_type="status_transition",
                intent_data={"to_status": "investigating", "from_status": "inquiry"},
            )

        assert result["case_updated"].status == CaseStatus.INVESTIGATING

        # Verify checkpoint was created
        checkpoints = await case_repo.get_checkpoints(case.case_id)
        pre_change_cps = [cp for cp in checkpoints if cp.trigger == "pre_case_action"]
        assert len(pre_change_cps) >= 1
        cp = pre_change_cps[0]
        assert cp.case_id == case.case_id
        assert cp.metadata["from_status"] == "inquiry"
        assert cp.metadata["to_status"] == "investigating"

    async def test_explicit_ui_resolve_proposes_then_confirms(
        self, engine, case_repo, checkpoint_service
    ):
        """Explicit UI resolve via status_transition proposes transition, then confirms.

        Design: Dropdown = message. First click proposes transition via
        User-Agent Handshake. Second click (or confirm) executes it.
        """
        from faultmaven.modules.case.contracts import (
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = _make_investigating_case(current_turn=5)
        # Add root cause + solution so resolution readiness check passes
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Connection pool timeout misconfigured",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Timeout too low for production load",
        )
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Increase pool timeout to 30s",
                longterm_fix="Update application config",
            )
        ]
        await case_repo.save(case)

        # Turn 1: First Resolve dropdown — proposes transition, does NOT execute
        result = await engine.process_turn(
            case,
            "Mark as resolved",
            intent_type="status_transition",
            intent_data={"to_status": "resolved", "from_status": "investigating"},
        )

        case = result["case_updated"]
        assert case.status == CaseStatus.INVESTIGATING  # NOT resolved yet
        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "resolved"

        # Turn 2: Second Resolve dropdown — confirms pending transition
        case.current_turn = 6
        result = await engine.process_turn(
            case,
            "yes",
            intent_type="status_transition",
            intent_data={"to_status": "resolved", "from_status": "investigating"},
        )

        assert result["case_updated"].status == CaseStatus.RESOLVED
        persisted = await case_repo.get(case.case_id)
        assert persisted.status == CaseStatus.RESOLVED


# ============================================================
# Test: Concurrent turn locking
# ============================================================


@pytest.mark.asyncio
class TestConcurrentTurnLocking:
    """Verify per-case locking prevents concurrent turn interleaving."""

    async def test_concurrent_turns_on_same_case_are_serialized(
        self, engine, case_repo
    ):
        """Two concurrent process_turn calls on same case are serialized."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        execution_order = []
        original_impl = engine._process_turn_impl

        async def tracking_impl(case, user_message, *args, **kwargs):
            execution_order.append(f"start:{user_message[:10]}")
            await asyncio.sleep(0.05)
            result = await original_impl(case, user_message, *args, **kwargs)
            execution_order.append(f"end:{user_message[:10]}")
            return result

        engine._process_turn_impl = tracking_impl

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            task1 = asyncio.create_task(engine.process_turn(case, "Message A - first"))
            task2 = asyncio.create_task(engine.process_turn(case, "Message B - second"))
            await asyncio.gather(task1, task2, return_exceptions=True)

        # Serialized: start_A, end_A, start_B, end_B (or B first)
        assert len(execution_order) == 4
        first_msg = execution_order[0].split(":")[1]
        assert execution_order[1] == f"end:{first_msg}"

    async def test_concurrent_turns_on_different_cases_are_parallel(
        self, engine, case_repo
    ):
        """Two concurrent process_turn calls on different cases run in parallel."""
        case_a = _make_inquiry_case(current_turn=1)
        case_b = _make_inquiry_case(current_turn=1)
        await case_repo.save(case_a)
        await case_repo.save(case_b)

        start_times = {}
        original_impl = engine._process_turn_impl

        async def tracking_impl(case, user_message, *args, **kwargs):
            start_times[case.case_id] = asyncio.get_event_loop().time()
            await asyncio.sleep(0.05)
            return await original_impl(case, user_message, *args, **kwargs)

        engine._process_turn_impl = tracking_impl

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            task1 = asyncio.create_task(engine.process_turn(case_a, "Turn for case A"))
            task2 = asyncio.create_task(engine.process_turn(case_b, "Turn for case B"))
            await asyncio.gather(task1, task2, return_exceptions=True)

        assert len(start_times) == 2
        times = list(start_times.values())
        assert abs(times[0] - times[1]) < 0.1  # Started within 100ms of each other


# ============================================================
# Test: Evidence accumulation
# ============================================================


@pytest.mark.asyncio
class TestEvidenceAccumulation:
    """Test evidence collection across multiple turns."""

    async def test_evidence_accumulates_across_turns(self, engine, case_repo):
        """Evidence from multiple turns accumulates in the case."""
        case = _make_investigating_case(current_turn=3)
        await case_repo.save(case)

        # Turn 3: First evidence (metrics)
        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_verification_response(),
        ):
            result = await engine.process_turn(case, "Here are the metrics")
        case = result["case_updated"]
        count_after_turn3 = len(case.evidence)
        assert count_after_turn3 >= 1

        # Turn 4: More evidence (timeline)
        case.current_turn = 4
        response_turn4 = InvestigationResponse_Diagnosis(
            agent_response="Root cause confirmed: v2.1.3 deployment introduced a DB connection leak.",
            internal_reasoning=InternalReasoning(
                evidence_analyzed=["ev_placeholder"],
                milestone_justifications={
                    "root_cause_identified": "Connection leak introduced in v2.1.3 deployment at 14:00",
                },
            ),
            state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
                milestones=MilestoneUpdates(root_cause_identified=True),
                evidence_to_add=[
                    EvidenceToAdd(
                        summary="v2.1.3 deployment introduced DB connection leak causing errors from 14:03",
                        category=EvidenceCategory.CAUSAL_EVIDENCE,
                        source_type=EvidenceSourceType.LOGS,
                        content_ref="deploy: 14:00:00 v2.1.3; errors: 14:03:21",
                    ),
                    EvidenceToAdd(
                        summary="Connection pool exhaustion trace confirms leak in v2.1.3 connection handler",
                        category=EvidenceCategory.CAUSAL_EVIDENCE,
                        source_type=EvidenceSourceType.LOGS,
                        content_ref="pool.max_connections exceeded; handler.py:L42 missing conn.close()",
                    ),
                ],
                outcome=TurnOutcome.MILESTONE_COMPLETED,
            ),
        )

        with patch.object(
            engine, "_generate_structured_output", return_value=response_turn4
        ):
            result = await engine.process_turn(case, "Deployment was at 14:00")
        case = result["case_updated"]

        assert len(case.evidence) > count_after_turn3
        assert case.progress.root_cause_identified is True

    async def test_evidence_milestone_attribution(self, engine, case_repo):
        """Evidence advances_milestones correctly attributed via Tier 2 inference."""
        case = _make_investigating_case(current_turn=3)
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_verification_response(),
        ):
            result = await engine.process_turn(case, "Here are the metrics")

        updated = result["case_updated"]
        new_evidence = [e for e in updated.evidence if e.collected_at_turn == 3]
        assert len(new_evidence) >= 1

        symptom_ev = [
            e for e in new_evidence if e.category == EvidenceCategory.SYMPTOM_EVIDENCE
        ]
        if symptom_ev:
            # Tier 2 inference: SYMPTOM_EVIDENCE + milestones completed this turn
            assert len(symptom_ev[0].advances_milestones) >= 1


# ============================================================
# Test: Turn history and progress tracking
# ============================================================


@pytest.mark.asyncio
class TestTurnHistoryAndProgress:
    """Test turn record creation and progress metrics."""

    async def test_turn_history_recorded(self, engine, case_repo):
        """Each process_turn creates a turn history record."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            result = await engine.process_turn(case, "Our API is experiencing latency")

        updated = result["case_updated"]
        assert len(updated.turn_history) >= 1
        turn = updated.turn_history[-1]
        assert turn.turn_number == 1
        # TurnProgress stores user_message_summary, not user_message
        assert turn.user_message_summary is not None

    async def test_no_progress_increments_counter(self, engine, case_repo):
        """Turns without milestone progress increment turns_without_progress."""
        case = _make_investigating_case(current_turn=5)
        case.turns_without_progress = 0
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_no_progress_response(),
        ):
            result = await engine.process_turn(case, "Can you help?")

        updated = result["case_updated"]
        assert result["metadata"]["progress_made"] is False
        assert updated.turns_without_progress >= 1

    async def test_case_saved_to_repository_after_turn(self, engine, case_repo):
        """Case is persisted to repository after each turn."""
        case = _make_inquiry_case(current_turn=1)
        await case_repo.save(case)
        original_updated_at = case.updated_at

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_inquiry_response_low_urgency(),
        ):
            result = await engine.process_turn(case, "Our API is having issues")

        persisted = await case_repo.get(case.case_id)
        assert persisted is not None
        assert persisted.updated_at >= original_updated_at


# ============================================================
# Test: Natural-language intent detection (REMOVED)
# ============================================================
# The engine no longer pattern-matches typed transition phrases — that
# duplicate path was deleted in favor of routing typed text through the
# LLM via the proposed_transition mechanism (see InvestigationService and
# the prompt rules in INQUIRY_TEMPLATE / INVESTIGATION_BASE).
#
# Coverage migrated:
# - Structured-intent path: tests/unit/core/investigation/
#   test_milestone_engine.py::test_explicit_status_transition_*
# - Alignment of UI/agent/NL trigger paths:
#   tests/unit/core/investigation/test_transition_alignment.py
# - End-to-end LLM compliance for typed NL (Tier-2 prompt eval):
#   fm-sre-simulator/prompt-evals/transition-intent/


# ============================================================
# Test: Working conclusion generation
# ============================================================


@pytest.mark.asyncio
class TestWorkingConclusion:
    """Test working conclusion generation during INVESTIGATING."""

    async def test_working_conclusion_generated_during_investigating(
        self, engine, case_repo
    ):
        """Working conclusion updated every turn during INVESTIGATING."""
        case = _make_investigating_case(current_turn=4)
        await case_repo.save(case)

        with patch.object(
            engine,
            "_generate_structured_output",
            return_value=_investigation_verification_response(),
        ):
            result = await engine.process_turn(case, "Here are the metrics")

        updated = result["case_updated"]
        assert updated.working_conclusion is not None
