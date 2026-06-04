"""Tests for the runbook-flow gap closure (RG1-RG5) and resolution-summary
G1/G2 changes shipped on `feat/runbook-completion-notification`.

Covered behaviors:
- G1: ``_auto_generate_report`` uses a state-aware label in the failure note
- G2: ``_auto_generate_report`` returns ``(payload, summary_failed)``; the
  ack-turn offers the regen affordance only when ``summary_failed=True``
- RG3: ``_handle_runbook_creation`` success path re-offers
  ``_resolved_suggestions`` so the user can iterate while the background
  task runs
- RG1+RG2: ``_run_runbook_conversion`` writes a system message to the case
  transcript on success and on failure (best-effort, never raises)
- RG4: ``POST /knowledge/convert-from-case`` uses the canonical
  ``CaseConversionRequest.from_case`` factory (no inline extraction)
- RG5: missing ``runbook_kb`` is logged at WARNING when dedup is skipped
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
    ReportGenerationResponse,
    RootCauseConclusion,
    Solution,
    SolutionType,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.generate = AsyncMock(return_value=MagicMock())
    return llm


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(return_value=None)
    return repo


def _make_resolved_case(case_id: str = "case_aabb11223344") -> Case:
    """RESOLVED case with the bare-minimum bookkeeping the model demands.

    Built in INVESTIGATING first, then promoted via ``object.__setattr__``
    to bypass the Pydantic cross-field validators (resolved_at must be
    >= created_at, RESOLVED requires closure-state fields, etc.).
    """
    case = Case(
        case_id=case_id,
        user_id="u1",
        organization_id="o1",
        title="Pool timeout resolved",
        description="DB queries timing out",
        state=CaseState.INVESTIGATING,
        problem_verification=ProblemVerification(
            symptom_statement="Timeout errors",
            severity="HIGH",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Timeout",
        ),
    )
    now = datetime.now(timezone.utc)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", now)
    object.__setattr__(case, "closed_at", now)
    return case


def _make_runbook_ready(case: Case) -> None:
    """Populate root_cause + solution + evidence to clear runbook readiness."""
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool timeout misconfigured",
        confidence_level="verified",
        likelihood=0.9,
        mechanism="Timeout too low",
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Bump pool timeout to 30s",
            longterm_fix="Update application config",
            commands=["kubectl edit configmap"],
            verification_method="Check p99 < 500ms for 30 min",
        )
    ]
    case.evidence = [
        Evidence(
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="Timeout errors in logs",
            extract="Error: timeout",
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabb12345678",
            collected_by="u1",
            collected_at_turn=1,
        )
    ]


# =============================================================================
# G1 + G2: _auto_generate_report tuple return + state-aware failure note
# =============================================================================


class TestAutoGenerateReportTupleReturn:
    """Pin the new ``_auto_generate_report`` return contract.

    Returns ``(payload, summary_failed)`` so the ack-turn can decide
    whether to offer the regen affordance (G2). The failure note uses a
    state-aware label (G1).
    """

    @pytest.mark.asyncio
    async def test_returns_content_and_false_on_success(self, mock_llm, mock_repo):
        case = _make_resolved_case()
        report = MagicMock()
        report.content = "# Resolution Summary\n\nDetails here."
        report_service = MagicMock()
        # generate_reports returns ReportGenerationResponse, not a bare
        # list. Previously a bare-list mock masked a prod bug where
        # callers were doing reports[0] on the response object.
        # ``model_construct`` bypasses Pydantic validation so the inner
        # report can stay a MagicMock — this test is about the response
        # *wrapper's* unpacking, not the report's field schema.
        report_service.generate_reports = AsyncMock(
            return_value=ReportGenerationResponse.model_construct(
                case_id=case.case_id, reports=[report], remaining_regenerations=4
            )
        )

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            report_service=report_service,
        )
        payload, failed = await engine._auto_generate_report(case)
        assert payload == "# Resolution Summary\n\nDetails here."
        assert failed is False

    @pytest.mark.asyncio
    async def test_resolved_failure_uses_resolution_summary_label(
        self, mock_llm, mock_repo
    ):
        """G1: failure note for RESOLVED uses 'Resolution summary' label.

        Pins the bug fix — the old hardcoded 'Closure summary' text was
        wrong for RESOLVED cases.
        """
        case = _make_resolved_case()
        report_service = MagicMock()
        report_service.generate_reports = AsyncMock(
            side_effect=RuntimeError("LLM exploded")
        )

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            report_service=report_service,
        )
        payload, failed = await engine._auto_generate_report(case)
        assert failed is True, "RESOLVED failure must flag summary_failed=True"
        assert payload is not None
        assert payload.startswith(
            "Resolution summary generation did not complete"
        ), f"G1 regression: RESOLVED failure note starts with: {payload!r}"

    @pytest.mark.asyncio
    async def test_closed_failure_uses_closure_summary_label(self, mock_llm, mock_repo):
        """G1: CLOSED failure note uses 'Closure summary' label."""
        case = _make_resolved_case()
        # Promote RESOLVED → CLOSED; clear `resolved_at` first to satisfy
        # the cross-field validator (resolved_at only valid in RESOLVED).
        object.__setattr__(case, "resolved_at", None)
        object.__setattr__(case, "state", CaseState.CLOSED)
        object.__setattr__(case, "closure_reason", "closed_after_investigation")
        case.evidence = [
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="s",
                extract="e",
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabb12345678",
                collected_by="u1",
                collected_at_turn=1,
            )
        ]
        report_service = MagicMock()
        report_service.generate_reports = AsyncMock(
            side_effect=RuntimeError("LLM exploded")
        )

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            report_service=report_service,
        )
        payload, failed = await engine._auto_generate_report(case)
        assert failed is True
        assert payload is not None
        assert payload.startswith("Closure summary generation did not complete")


# =============================================================================
# G2: ack-turn offers regen affordance when summary generation failed
# =============================================================================


class TestAckTurnFollowUpsOnFailure:
    """G2: ``_select_ack_follow_ups`` returns regen affordance on failure."""

    def test_success_resolved_returns_minimal_suggestions(self):
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_ack_suggestions,
            _select_ack_follow_ups,
        )

        case = MagicMock()
        case.state = CaseState.RESOLVED

        follow_ups = _select_ack_follow_ups(case, summary_failed=False, remaining=5)
        assert follow_ups == _resolved_ack_suggestions()

    def test_success_closed_returns_empty(self):
        from faultmaven.core.investigation.milestone_engine import (
            _select_ack_follow_ups,
        )

        case = MagicMock()
        case.state = CaseState.CLOSED

        follow_ups = _select_ack_follow_ups(case, summary_failed=False, remaining=5)
        assert follow_ups == []

    def test_failure_resolved_includes_regen_and_runbook(self):
        """G2: failed RESOLVED summary → ack-turn offers regen + runbook."""
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_suggestions,
            _select_ack_follow_ups,
        )

        case = MagicMock()
        case.state = CaseState.RESOLVED

        follow_ups = _select_ack_follow_ups(case, summary_failed=True, remaining=5)
        assert follow_ups == _resolved_suggestions(remaining=5)
        labels = [s["label"] for s in follow_ups]
        assert any("regenerate" in label.lower() for label in labels)

    def test_failure_closed_includes_regen_when_substance_passes(self):
        """G2: failed CLOSED summary → ack-turn offers regen if substance passes.

        Failure can only happen when generation was attempted; for CLOSED
        that means the substance gate already PASSED.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _select_ack_follow_ups,
        )

        case = MagicMock()
        case.state = CaseState.CLOSED
        case.progress = MagicMock()
        case.progress.completed_milestones = ["symptom_verified"]
        case.evidence = []
        case.hypotheses = {}

        follow_ups = _select_ack_follow_ups(case, summary_failed=True, remaining=5)
        assert follow_ups, (
            "Failed CLOSED summary on a substantive case must offer regen — "
            "no inline summary means the 'noise' rationale doesn't apply."
        )
        labels = [s["label"] for s in follow_ups]
        assert any("regenerate" in label.lower() for label in labels)


# =============================================================================
# RG3: _handle_runbook_creation success path re-offers standard Q&A suggestions
# =============================================================================


class TestRunbookCreationFollowUps:
    @pytest.mark.asyncio
    async def test_success_path_returns_resolved_suggestions(self, mock_llm, mock_repo):
        """RG3: success branch re-offers the regen affordance only.

        The runbook affordance is hidden on this turn — we just kicked
        off generation as a background task, so re-offering would race
        the in-flight task and risk a duplicate draft. The user iterates
        on the resulting draft in the Dashboard Drafts editor, not via
        another chat click.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_suggestions,
        )

        case = _make_resolved_case()
        _make_runbook_ready(case)

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        # Stub conversion_service so the background task is scheduled cleanly
        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[])
        )
        engine.conversion_service = conversion_service
        # knowledge_service without runbook_kb → dedup is skipped
        engine.knowledge_service = MagicMock(spec=[])

        result = await engine._handle_runbook_creation(case, metadata={})

        # `runbook_already_exists=True` is passed at this call site (we
        # just kicked off conversion). The expected list contains only
        # the regen affordance.
        assert result["suggested_follow_ups"] == _resolved_suggestions(
            remaining=await engine._remaining_regens_for(case),
            runbook_already_exists=True,
        )
        labels = [s["label"] for s in result["suggested_follow_ups"]]
        assert "Generate runbook from this case" not in labels

    @pytest.mark.asyncio
    async def test_not_ready_branch_returns_empty(self, mock_llm, mock_repo):
        """RG3: text-only dead-end branches keep empty follow-ups.

        NOT_READY on a terminal case is a genuine dead-end — the user
        can't add evidence to fix readiness because the case is immutable.
        Pins the deliberate narrowing of Phase 3 to the success path only.
        """
        case = _make_resolved_case()  # No root_cause, no solutions → NOT_READY

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        engine.knowledge_service = MagicMock(spec=[])

        result = await engine._handle_runbook_creation(case, metadata={})
        assert result["suggested_follow_ups"] == []


# =============================================================================
# RG1 + RG2: background task writes a system message on completion
# =============================================================================


class TestRunbookCompletionNotification:
    @pytest.mark.asyncio
    async def test_success_writes_completion_message(self, mock_llm):
        """Successful conversion → system message names the new draft."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        case = _make_resolved_case()
        initial_message_count = len(case.messages)

        request = CaseConversionRequest(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            scope="global",
        )

        draft = MagicMock()
        draft.runbook_id = "rb_001"
        draft.title = "Pool Timeout Runbook"
        draft.quality_score = 85
        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[draft])
        )

        repo = MagicMock()
        repo.get = AsyncMock(return_value=case)
        repo.save = AsyncMock()

        engine = MilestoneEngine(mock_llm, repo, investigation_tools=MagicMock())

        await engine._run_runbook_conversion(conversion_service, request, "u1")

        assert len(case.messages) == initial_message_count + 1
        notification = case.messages[-1]
        assert notification["role"] == "system"
        assert notification["author_id"] == "system"
        assert "Pool Timeout Runbook" in notification["content"]
        assert notification["metadata"]["source"] == "runbook_conversion_complete"
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_writes_retry_message(self, mock_llm):
        """LLM exception → system message offering retry, save still called."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        case = _make_resolved_case()
        request = CaseConversionRequest(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            scope="global",
        )

        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            side_effect=RuntimeError("LLM exploded")
        )

        repo = MagicMock()
        repo.get = AsyncMock(return_value=case)
        repo.save = AsyncMock()

        engine = MilestoneEngine(mock_llm, repo, investigation_tools=MagicMock())

        await engine._run_runbook_conversion(conversion_service, request, "u1")

        notification = case.messages[-1]
        assert notification["role"] == "system"
        content_lower = notification["content"].lower()
        assert "fail" in content_lower
        assert "retry" in content_lower
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_drafts_writes_retry_message(self, mock_llm):
        """Empty drafts (e.g. quality-rejected) → retry message, save called."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        case = _make_resolved_case()
        request = CaseConversionRequest(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            scope="global",
        )

        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[])
        )

        repo = MagicMock()
        repo.get = AsyncMock(return_value=case)
        repo.save = AsyncMock()

        engine = MilestoneEngine(mock_llm, repo, investigation_tools=MagicMock())

        await engine._run_runbook_conversion(conversion_service, request, "u1")

        notification = case.messages[-1]
        assert notification["role"] == "system"
        assert "no draft was produced" in notification["content"].lower()
        assert "retry" in notification["content"].lower()
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_case_not_found_does_not_raise(self, mock_llm):
        """If the case was deleted while the background task ran, the
        notification write logs and exits cleanly — never raises."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        request = CaseConversionRequest(
            case_id="case_deleted_001",
            title="t",
            description="d",
            scope="global",
        )

        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[])
        )

        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.save = AsyncMock()

        engine = MilestoneEngine(mock_llm, repo, investigation_tools=MagicMock())

        # Must not raise
        await engine._run_runbook_conversion(conversion_service, request, "u1")
        repo.save.assert_not_called()


# =============================================================================
# RG5: missing runbook_kb is logged at WARNING
# =============================================================================


class TestDedupSkipObservability:
    @pytest.mark.asyncio
    async def test_dedup_skip_logs_warning(self, caplog):
        import logging

        from faultmaven.core.investigation.terminal_transitions import (
            evaluate_runbook_suggestion,
        )

        case = Case(
            user_id="u1",
            organization_id="o1",
            title="Pool timeout",
            description="DB queries timing out",
            state=CaseState.INVESTIGATING,
            problem_verification=ProblemVerification(
                symptom_statement="Timeout errors",
                severity="HIGH",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Timeout",
            ),
        )
        _make_runbook_ready(case)

        with caplog.at_level(
            logging.WARNING,
            logger="faultmaven.core.investigation.terminal_transitions",
        ):
            await evaluate_runbook_suggestion(case, runbook_kb=None)

        skip_warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "Runbook deduplication skipped" in r.message
        ]
        assert skip_warnings, (
            "RG5 violation: dedup skip with runbook_kb=None must log "
            "WARNING — silent skip masks production misconfiguration."
        )
        assert case.case_id in skip_warnings[0].message


# =============================================================================
# RG4: convert-from-case API uses the canonical factory
# =============================================================================


class TestConvertFromCaseAPIUsesFactory:
    """RG4: ``POST /knowledge/convert-from-case`` uses
    ``CaseConversionRequest.from_case``.

    Static guard — the inline ~90-line extraction was removed in favor of
    the canonical factory. A future regression that reintroduces inline
    extraction would let drift creep back in between this endpoint and
    the chat-side ``_handle_runbook_creation`` path.
    """

    def test_api_endpoint_uses_from_case_factory(self):
        from faultmaven.modules.knowledge.api import conversion_routes

        source = inspect.getsource(conversion_routes.convert_from_case)
        assert "CaseConversionRequest.from_case(" in source, (
            "RG4 violation: convert-from-case API no longer uses "
            "CaseConversionRequest.from_case factory — inline extraction "
            "would diverge from the chat-side path."
        )

    def test_api_endpoint_has_no_inline_extraction_markers(self):
        """Pins that the cleanup stayed clean — old inline-extraction
        markers must not return."""
        from faultmaven.modules.knowledge.api import conversion_routes

        source = inspect.getsource(conversion_routes.convert_from_case)
        forbidden_markers = [
            "Root cause — from RootCauseConclusion",
            "Problem description — from ProblemVerification",
            "Solutions — use rich Solution model fields",
        ]
        for marker in forbidden_markers:
            assert marker not in source, (
                f"RG4 violation: inline-extraction marker '{marker}' "
                f"reappeared. Both call sites must use the factory."
            )
