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
- Reachability: any affordance label a turn names must be present in that
  turn's own ``suggested_follow_ups`` (prose and the suggestion list are built
  a few lines apart and drift silently), over all eight user-visible outcomes
  of ``_handle_runbook_creation``; and the three completion notifications —
  which carry no suggestion list at all and are read in the Dashboard, where
  no chip UI exists — must name no affordance
- The kickoff turn promises only what every client delivers: no in-chat
  notification claim (that system row is invisible in the copilot, failure
  notices included), the Dashboard location named, a way forward that survives
  a silent failure, and no recovery advice keyed on the draft's absence while
  the conversion is still running
- RG4: the case→runbook chat path (``_handle_runbook_creation``) uses the
  canonical ``CaseConversionRequest.from_case`` factory (no inline extraction)
- RG5: missing ``runbook_kb`` is logged at WARNING when dedup is skipped
"""

import inspect
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.cause_assurance import CauseAssuranceGrade
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ReportGenerationResponse,
    RootCauseConclusion,
    Solution,
    SolutionType,
    ValidationMethod,
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
        ),
        # A harvestable cause must be CONFIRMED (#590 A1): a VALIDATED root
        # bearing a counterfactual confirmation (causal_absence SUPPORTS, M2
        # gone⇒gone), not bare RCC prose.
        Evidence(
            evidence_id="ev_d00dfeed0001",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="diagnosis",
            summary="pool exhausted at 14:03",
            extract="pool: 0 idle / 0 free",
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabb12345678",
            collected_by="u1",
            collected_at_turn=1,
        ),
        Evidence(
            evidence_id="ev_d00dfeed0002",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            primary_purpose="diagnosis",
            summary="pool limit raised; timeouts gone for 30 minutes",
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="u1",
            collected_at_turn=2,
        ),
    ]
    case.causal_nodes["cn_d00dfeed0001"] = CausalNode(
        node_id="cn_d00dfeed0001",
        statement="connection pool exhausted",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        actionable=True,
        belief=0.8,
        generated_at_turn=1,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id="ev_d00dfeed0001",
                stance=EvidenceStance.SUPPORTS,
                reasoning="observed exhausted pool matches the posited cause",
                linked_at_turn=1,
            ),
            NodeEvidenceLink(
                evidence_id="ev_d00dfeed0002",
                stance=EvidenceStance.SUPPORTS,
                reasoning="removing the cause removed the problem",
                linked_at_turn=2,
            ),
        ],
    )


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
        object.__setattr__(case, "closure_reason", "closed_insufficient_evidence")
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

    def test_success_resolved_returns_minimal_suggestions(self, monkeypatch):
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_ack_suggestions,
            _select_ack_follow_ups,
        )

        # Runbook affordance is grade-gated (#695 Defect A); pin CONFIRMED so the
        # MagicMock case yields a deterministic grade for both sides.
        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine.grade_cause_assurance",
            lambda case: CauseAssuranceGrade.CONFIRMED,
        )
        case = MagicMock()
        case.state = CaseState.RESOLVED

        follow_ups = _select_ack_follow_ups(case, summary_failed=False, remaining=5)
        assert follow_ups == _resolved_ack_suggestions(case)

    def test_success_closed_returns_empty(self):
        from faultmaven.core.investigation.milestone_engine import (
            _select_ack_follow_ups,
        )

        case = MagicMock()
        case.state = CaseState.CLOSED

        follow_ups = _select_ack_follow_ups(case, summary_failed=False, remaining=5)
        assert follow_ups == []

    def test_failure_resolved_includes_regen_and_runbook(self, monkeypatch):
        """G2: failed RESOLVED summary → ack-turn offers regen + runbook
        (runbook because the cause is CONFIRMED — #695 Defect A)."""
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_suggestions,
            _select_ack_follow_ups,
        )

        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine.grade_cause_assurance",
            lambda case: CauseAssuranceGrade.CONFIRMED,
        )
        case = MagicMock()
        case.state = CaseState.RESOLVED

        follow_ups = _select_ack_follow_ups(case, summary_failed=True, remaining=5)
        assert follow_ups == _resolved_suggestions(case, remaining=5)
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
            case,
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
# A turn may only name what the reader can act on while reading it
# =============================================================================


_EMBED_QUERY = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"
_SEED_ORIGIN = (
    "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin"
)
_FROM_CASE = (
    "faultmaven.modules.knowledge.domain.models.conversion."
    "CaseConversionRequest.from_case"
)


def _raise_from_case(cls, case, scope="personal"):
    raise RuntimeError("conversion request could not be built")


def _dedup_kb(matches=None):
    """A real ``RunbookKnowledgeBase`` with only its search stubbed.

    Mocking at the KB boundary rather than patching
    ``_find_similar_runbooks_for_case`` keeps the dedup path itself — the code
    that turns matches into a verdict — under test.
    """
    from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase

    kb = RunbookKnowledgeBase(vector_store=MagicMock())
    kb.search_runbooks = AsyncMock(return_value=matches or [])
    return kb


async def _run_creation_turn(mock_llm, mock_repo, monkeypatch, scenario: str) -> dict:
    """Drive one user-visible outcome of ``_handle_runbook_creation``.

    Returns the turn dict, so callers can judge prose and suggestions together.
    """
    from faultmaven.infrastructure.knowledge.runbook_kb import RunbookMatch

    case = _make_resolved_case()
    if scenario != "not-ready":
        _make_runbook_ready(case)

    engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
    engine.knowledge_service = MagicMock(spec=[])

    conversion_service = MagicMock()
    conversion_service.convert_from_case = AsyncMock(return_value=MagicMock(drafts=[]))
    conversion_service.get_conversion_by_case = AsyncMock(return_value=None)
    engine.conversion_service = conversion_service

    # The provenance short-circuit fires before everything else, so every other
    # scenario has to hold it off explicitly.
    monkeypatch.setattr(_SEED_ORIGIN, lambda case: None)

    # `runbook_kb=None` means dedup is skipped, not clean — the honest "could
    # not check" caveat. Scenarios that need a real verdict install a KB.
    embed_patch = nullcontext()

    if scenario == "seed-origin":
        monkeypatch.setattr(_SEED_ORIGIN, lambda case: "rb_seed00000001")
    elif scenario == "already-exists":
        existing = MagicMock()
        existing.has_live_draft.return_value = True
        conversion_service.get_conversion_by_case = AsyncMock(return_value=existing)
    elif scenario == "service-unavailable":
        engine.conversion_service = None
    elif scenario == "start-failure":
        monkeypatch.setattr(_FROM_CASE, classmethod(_raise_from_case))
    elif scenario == "similar-found":
        engine.runbook_kb = _dedup_kb(
            [
                RunbookMatch(
                    item_id="kb-1",
                    title="Pool timeout recovery",
                    scope="global",
                    similarity_score=0.91,
                )
            ]
        )
        embed_patch = patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024))
    elif scenario == "kickoff-clean-dedup":
        engine.runbook_kb = _dedup_kb([])
        embed_patch = patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024))

    with embed_patch:
        return await engine._handle_runbook_creation(case, metadata={})


# Every user-visible outcome of `_handle_runbook_creation`. The function has
# six `return` statements; the last one carries three distinct texts (plain
# SUGGEST, the SUGGEST_WITH_CAVEATS prefix, and the "failed to start" except
# arm), so eight outcomes reach a reader. All eight are driven here — none had
# to be left out, which is what lets the reachability property below claim the
# whole surface rather than a sample of it.
_CREATION_TURN_SCENARIOS = [
    pytest.param("seed-origin", id="seed-origin"),
    pytest.param("not-ready", id="not-ready"),
    pytest.param("similar-found", id="similar-found"),
    pytest.param("service-unavailable", id="service-unavailable"),
    pytest.param("already-exists", id="already-exists"),
    pytest.param("kickoff-clean-dedup", id="kickoff-clean-dedup"),
    pytest.param("kickoff", id="kickoff-with-caveats"),
    pytest.param("start-failure", id="start-failure"),
]

_NOTIFICATION_OUTCOMES = ["success", "no-drafts", "exception"]


async def _notification_content(mock_llm, outcome: str) -> str:
    """Drive ``_run_runbook_conversion`` and return the transcript notice."""
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
    if outcome == "exception":
        conversion_service.convert_from_case = AsyncMock(
            side_effect=RuntimeError("LLM exploded")
        )
    else:
        draft = MagicMock()
        draft.title = "Pool Timeout Runbook"
        drafts = [draft] if outcome == "success" else []
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=drafts)
        )

    repo = MagicMock()
    repo.get = AsyncMock(return_value=case)
    repo.save = AsyncMock()
    engine = MilestoneEngine(mock_llm, repo, investigation_tools=MagicMock())

    await engine._run_runbook_conversion(conversion_service, request, "u1", "o1")
    return case.messages[-1]["content"]


# The Dashboard's navigation item is "Knowledge Base"; there is no section
# called "Knowledge". Naming a place the reader cannot find in the nav fails
# the same rule as naming a button that is not on screen, so any mention of
# the section must carry the real label.
_WRONG_NAV_LABEL_RE = re.compile(r"Knowledge(?! Base)")

_LABEL_LITERAL_RE = re.compile(r'"label":\s*"([^"]+)"')


def _known_affordance_labels() -> set[str]:
    """Every suggestion label the engine can offer, scraped from its source.

    Derived from the module rather than hand-listed on purpose: a newly added
    affordance joins the vocabulary automatically, so the reachability
    property cannot quietly stop covering the newest label. Fails closed if
    the scrape stops matching.
    """
    import faultmaven.core.investigation.milestone_engine as engine_module

    labels = set(_LABEL_LITERAL_RE.findall(inspect.getsource(engine_module)))
    assert "Generate runbook from this case" in labels, (
        "affordance-label scrape found no runbook label — the pattern has "
        "drifted and this property would pass vacuously"
    )
    return labels


class TestNamedAffordancesAreReachableOnTheirOwnTurn:
    """Reachability: if a turn names an affordance, that turn must offer it.

    Prose and the suggestion list are produced a few lines apart and drift
    silently — the message says "click X" while X is filtered out of
    ``suggested_follow_ups`` further down. Nothing about the text alone can
    catch that; the turn has to be judged as a whole. Free-typed text is no
    escape hatch either: ``_RUNBOOK_CREATION_PATTERNS`` exact-matches the
    DECIDE payload, so a label the user reads is not a label they can type.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", _CREATION_TURN_SCENARIOS)
    async def test_every_named_label_is_offered_on_that_turn(
        self, scenario, mock_llm, mock_repo, monkeypatch
    ):
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, scenario)
        response = result["agent_response"]
        offered = {s["label"] for s in result["suggested_follow_ups"]}

        # The property is a subset check over text that must exist: a branch
        # that returned no prose would satisfy it while saying nothing.
        assert response.strip(), "branch produced an empty turn — nothing to check"

        unreachable = {
            label for label in _known_affordance_labels() if label in response
        } - offered
        assert not unreachable, (
            f"turn names {sorted(unreachable)} but does not offer it — the "
            f"reader is sent looking for a chip that is not on screen, and "
            f"the label is not typeable. Turn text: {response!r}; offered: "
            f"{sorted(offered)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", _NOTIFICATION_OUTCOMES)
    async def test_completion_notifications_name_no_affordance(self, outcome, mock_llm):
        """The same rule, extended to text that reaches the reader elsewhere.

        ``_run_runbook_conversion`` appends its outcome to the transcript
        rather than returning a turn, so it carries no ``suggested_follow_ups``
        at all — the offered set is empty by construction and the subset rule
        collapses to "name nothing". That is the correct reading: the copilot
        drops system rows, and the Dashboard, which does render them, has no
        suggestion-chip UI for a label to refer to.

        Binding the rule only where it was first tested is how the defect
        recurred one function away, so it is bound here too.
        """
        content = await _notification_content(mock_llm, outcome)
        assert content.strip(), "notification is empty — nothing to check"

        named = {label for label in _known_affordance_labels() if label in content}
        assert not named, (
            f"completion notification names {sorted(named)}, but a "
            f"notification reaches no suggestion list: the copilot drops the "
            f"row entirely and the Dashboard has no chip UI, so this points "
            f"at a control the reader cannot have. Content: {content!r}"
        )


class TestDashboardSectionIsNamedAsTheReaderSeesIt:
    """A named place must be findable, exactly as a named button must be.

    The Dashboard nav item reads "Knowledge Base". A message that says
    "Knowledge > Drafts" sends the reader hunting for a section that is not
    in the nav — the same failure as naming an absent chip, one word smaller.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", _CREATION_TURN_SCENARIOS)
    async def test_turn_text_uses_the_real_nav_label(
        self, scenario, mock_llm, mock_repo, monkeypatch
    ):
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, scenario)
        response = result["agent_response"]

        assert not _WRONG_NAV_LABEL_RE.search(response), (
            f"turn names a Dashboard section called 'Knowledge'; the nav item "
            f"is 'Knowledge Base': {response!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", _NOTIFICATION_OUTCOMES)
    async def test_notification_text_uses_the_real_nav_label(self, outcome, mock_llm):
        content = await _notification_content(mock_llm, outcome)

        assert not _WRONG_NAV_LABEL_RE.search(content), (
            f"notification names a Dashboard section called 'Knowledge'; the "
            f"nav item is 'Knowledge Base': {content!r}"
        )


# Phrasings that all assert the same thing: "the result will be delivered to
# you in this conversation". The completion notification is written as a
# `role: "system"` message and the copilot's conversation loader keeps only
# user/assistant rows, with no push channel for case messages — so no client
# reliably shows it, on the turn or after a reload. The FAILURE notifications
# ride the same row, which is the half that matters: a failed or empty
# conversion is silent, so a user told to wait for word would wait forever.
_IN_CHAT_DELIVERY_CLAIMS = (
    "let you know here",
    "let you know when",
    "i'll let you know",
    "i will let you know",
    "notify you here",
    "tell you here",
    "here when it's ready",
    "here when it is ready",
    "in this chat",
    "in this conversation",
    "message you when",
    "ping you when",
    "post it here",
    "come back here",
    "watch this space",
)

# Conditionals that ask the reader to read failure out of an empty Drafts
# list. ``_persist_job`` runs only after the conversion pipeline finishes, so
# nothing is written while the work is in flight: "not there yet" and "it
# failed" are indistinguishable to the reader. A recovery instruction keyed on
# absence therefore fires on the healthy path, telling a user whose conversion
# is working normally to go around it.
_ABSENCE_AS_FAILURE_CONDITIONALS = (
    "if it doesn't",
    "if it does not",
    "if it isn't",
    "if it is not",
    "if it hasn't",
    "if it has not",
    "if it never",
    "if nothing",
    "if you don't see",
    "if you do not see",
)


class TestRunbookInitiationMessagePromisesOnlyWhatIsDelivered:
    @pytest.mark.asyncio
    async def test_initiating_turn_makes_no_in_chat_notification_claim(
        self, mock_llm, mock_repo, monkeypatch
    ):
        """The kickoff turn must not tell the user to wait for word in chat.

        Pins the invariant, not the prose: any rewording is free as long as
        it does not reintroduce a delivery promise the transport cannot keep.
        """
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, "kickoff")
        response = result["agent_response"]
        lowered = response.lower()

        offenders = [c for c in _IN_CHAT_DELIVERY_CLAIMS if c in lowered]
        assert not offenders, (
            f"the runbook kickoff turn promises in-chat delivery ({offenders}), "
            f"but the completion notification is a system-role message no "
            f"client surfaces — including the failure notice: {response!r}"
        )

    @pytest.mark.asyncio
    async def test_initiating_turn_names_the_dashboard_location(
        self, mock_llm, mock_repo, monkeypatch
    ):
        """The one true destination must be named, or the draft is unfindable."""
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, "kickoff")
        response = result["agent_response"]

        assert "Dashboard" in response
        # "Knowledge Base" is the Dashboard's actual nav label; the section is
        # not called "Knowledge". Naming a place the reader cannot find in the
        # nav fails the same rule as naming an absent button.
        assert "Knowledge Base > Drafts" in response

    @pytest.mark.asyncio
    async def test_initiating_turn_offers_a_way_forward_that_outlives_the_turn(
        self, mock_llm, mock_repo, monkeypatch
    ):
        """Silent failure needs an exit the reader keeps after this turn ends.

        The background task's failure notice rides the invisible system row,
        so nothing will ever tell the user that generation did not work. The
        turn must therefore name a path that does not depend on a chip, on a
        notification, or on the user first deducing that something broke —
        the Dashboard's own runbook creation path.
        """
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, "kickoff")
        response = result["agent_response"]

        assert "Dashboard" in response
        assert "create" in response.lower(), (
            f"the turn names no way to produce a runbook without the "
            f"background task, leaving a silent failure with no exit: "
            f"{response!r}"
        )

    @pytest.mark.asyncio
    async def test_initiating_turn_does_not_read_failure_out_of_absence(
        self, mock_llm, mock_repo, monkeypatch
    ):
        """Recovery advice must not fire while the conversion is still running."""
        result = await _run_creation_turn(mock_llm, mock_repo, monkeypatch, "kickoff")
        response = result["agent_response"]
        lowered = response.lower()

        offenders = [c for c in _ABSENCE_AS_FAILURE_CONDITIONALS if c in lowered]
        assert not offenders, (
            f"the turn treats an empty Drafts list as failure ({offenders}), "
            f"but nothing is persisted until the pipeline finishes — this "
            f"fires on the healthy path: {response!r}"
        )


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

        await engine._run_runbook_conversion(conversion_service, request, "u1", "o1")

        assert len(case.messages) == initial_message_count + 1
        notification = case.messages[-1]
        assert notification["role"] == "system"
        # No human wrote this, so it carries no author. It previously held the
        # sentinel string "system", which was harmless only because author_id
        # was dropped before it reached the database; now that the column
        # persists, a sentinel would surface to clients as a non-resolvable
        # principal id on a field documented as "User who created the message".
        # `role` already carries the system signal (ADR-013 D4).
        assert notification["author_id"] is None
        assert "Pool Timeout Runbook" in notification["content"]
        assert notification["metadata"]["source"] == "runbook_conversion_complete"
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_writes_recovery_message(self, mock_llm):
        """LLM exception → system message naming a way forward, save called.

        Asserts the two things the reader needs — that it failed, and where to
        go next — rather than the word "retry", which named a chat affordance
        the Dashboard (the only client that renders this row) does not have.
        """
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

        await engine._run_runbook_conversion(conversion_service, request, "u1", "o1")

        notification = case.messages[-1]
        assert notification["role"] == "system"
        content_lower = notification["content"].lower()
        assert "fail" in content_lower
        assert "Dashboard" in notification["content"]
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_drafts_writes_recovery_message(self, mock_llm):
        """Empty drafts (e.g. quality-rejected) → recovery message, save called.

        The no-drafts outcome is a failure from the reader's side — the draft
        they were told to expect will never arrive — so the notice must say
        nothing was saved, not merely that the run "completed".
        """
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

        await engine._run_runbook_conversion(conversion_service, request, "u1", "o1")

        notification = case.messages[-1]
        assert notification["role"] == "system"
        assert "nothing was saved" in notification["content"].lower()
        assert "Dashboard" in notification["content"]
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
        await engine._run_runbook_conversion(conversion_service, request, "u1", "o1")
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
# RG4: case→runbook conversion uses the canonical factory
# =============================================================================


class TestCaseConversionUsesFactory:
    """RG4: the case→runbook path uses ``CaseConversionRequest.from_case``.

    Static guard — the inline ~90-line extraction was removed in favor of
    the canonical factory. Case→runbook is now chat-initiated only (the dead
    ``POST /knowledge/convert-from-case`` endpoint was removed in Phase 5.1;
    the Dashboard is view-only), so the single live extraction site is
    ``MilestoneEngine._handle_runbook_creation``. A future regression that
    reintroduces inline extraction there would resurrect the drift this factory
    exists to kill.
    """

    def test_chat_path_uses_from_case_factory(self):
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        source = inspect.getsource(MilestoneEngine._handle_runbook_creation)
        assert "CaseConversionRequest.from_case(" in source, (
            "RG4 violation: _handle_runbook_creation no longer uses "
            "CaseConversionRequest.from_case factory — inline extraction "
            "would reintroduce case→runbook drift."
        )

    def test_chat_path_has_no_inline_extraction_markers(self):
        """Pins that the cleanup stayed clean — old inline-extraction
        markers must not return."""
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        source = inspect.getsource(MilestoneEngine._handle_runbook_creation)
        forbidden_markers = [
            "Root cause — from RootCauseConclusion",
            "Problem description — from ProblemVerification",
            "Solutions — use rich Solution model fields",
        ]
        for marker in forbidden_markers:
            assert marker not in source, (
                f"RG4 violation: inline-extraction marker '{marker}' "
                f"reappeared in the case→runbook path."
            )


# =============================================================================
# #1143: the case→runbook background task carries the CASE's organization
# =============================================================================


class TestRunbookConversionCarriesOrg:
    """The conversion writes RLS-tenanted rows; it must be told whose they are.

    ``_persist_job`` stamps three tenanted tables (``uploaded_files``,
    ``conversion_jobs``, ``conversion_drafts``) with the org it is handed. The
    chat path passed none, so the service fell back to the single-tenant
    sentinel and every write under ``TENANT_PROVIDER=multi`` was refused by the
    RLS ``WITH CHECK`` — the user saw "Runbook generation failed, so no draft
    was created" (#1143). SQLite has no RLS, which is why this was invisible in
    every standalone rehearsal; these tests bite on the *stamp*, so they fail on
    SQLite too.
    """

    @pytest.mark.asyncio
    async def test_kickoff_passes_case_org_to_conversion_service(
        self, mock_llm, mock_repo
    ):
        """The org reaching ``convert_from_case`` is the source case's own."""
        import asyncio

        case = _make_resolved_case()
        _make_runbook_ready(case)
        object.__setattr__(case, "organization_id", "org_guest_7f2a")

        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[])
        )
        conversion_service.get_conversion_by_case = AsyncMock(return_value=None)

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        engine.conversion_service = conversion_service
        engine.knowledge_service = MagicMock(spec=[])

        # The kickoff fires the conversion as a fire-and-forget task, so capture
        # the coroutine and drive it here rather than racing the event loop.
        spawned = []

        def _capture(coro):
            spawned.append(coro)
            return MagicMock()

        with patch("asyncio.create_task", side_effect=_capture):
            await engine._handle_runbook_creation(case, metadata={})

        assert spawned, "kickoff did not schedule the background conversion"
        for coro in spawned:
            await coro

        conversion_service.convert_from_case.assert_awaited_once()
        kwargs = conversion_service.convert_from_case.await_args.kwargs
        assert kwargs["organization_id"] == "org_guest_7f2a", (
            "#1143: the background conversion must carry the case's org — "
            "without it the service stamps the single-tenant sentinel and "
            "PostgreSQL RLS refuses every write."
        )
        # Guard the whole call, not just the happy field: a future refactor that
        # reintroduces a positional call would silently drop the org again.
        assert kwargs["user_id"] == case.user_id

        await asyncio.sleep(0)

    def test_org_is_a_required_parameter(self):
        """``organization_id`` has no default — omitting it must not be silent.

        A defaulted parameter is how the org was lost in the first place: the
        service's ``organization_id: str = None`` let the only caller omit it
        and turned a tenancy bug into a signature nobody had to notice.
        """
        sig = inspect.signature(MilestoneEngine._run_runbook_conversion)
        param = sig.parameters["organization_id"]
        assert param.default is inspect.Parameter.empty, (
            "#1143: _run_runbook_conversion.organization_id must stay required; "
            "a default reopens the silent-omission path."
        )
