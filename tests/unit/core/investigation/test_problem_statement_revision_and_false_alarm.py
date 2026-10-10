"""Two outcomes of checking the confirmed problem statement besides "verified".

* **Inaccurate** — the problem is real, its statement is not: the evidence-based
  revision waits for the user's re-confirmation (REVISION_PENDING); cause work
  arriving meanwhile is staged and replayed when they confirm, so the
  confirmation turn can still verify, hypothesize and identify.
* **False alarm** — the reported symptom was never present (INVALIDATED): the
  engine offers to close, never to resolve, and the case holds until new
  evidence names a problem or the user disputes the finding.

Driven through the real apply path and, for the handshake, the real engine
turn. No live LLM: the engine's generator is stubbed.
"""

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.cause_assurance import has_resolution_confirmation
from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.affordances import (
    engine_owned_affordances,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.progress import (
    check_if_progress_made,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    revision_offer_key,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.problem_status import (
    FALSE_ALARM_CLOSURE_REASON,
    cancel_revision,
    commit_revision,
    decline_revision,
    edit_statement,
    edit_statement_refusal,
    invalidate_problem,
    invalidation_refusal,
    propose_revision,
    revision_refusal,
    withdraw_invalidation,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    EvidenceToAdd,
    EvidenceTrail,
    HypothesisToAdd,
    InvestigationResponse_Diagnosis,
    MilestoneJustifications,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
    ProblemVerificationUpdate,
    ProposedTransition,
)
from faultmaven.core.investigation.terminal_transitions import (
    _execute_resolved_transition,
    assess_closure_readiness,
    assess_resolution_readiness,
    confirm_pending_transition,
    derive_closure_reason,
    derive_disposition_eligibility,
    execute_user_closure,
    propose_transition,
)
from faultmaven.core.investigation.verification_status import (
    assess_verification_status,
)
from faultmaven.models.case_ui import CaseUIResponse_Resolved
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    MitigationRecord,
    NeedPurpose,
    NeedState,
    NodeState,
    NodeType,
    ProblemStatus,
    ProblemVerification,
    StatementRecordKind,
    ValidationMethod,
    VerificationStatus,
)
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    _extract_problem_verification,
    transform_case_for_ui,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit

_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate

STATEMENT = "The checkout database is slow"
REVISED = "API requests to /orders time out after 30s; database latency is normal"
CAUSE = "the orders service connection pool is capped at 5 connections"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _case(status: ProblemStatus = ProblemStatus.UNVERIFIED) -> Case:
    case = Case(
        case_id="case_0000000000aa",
        user_id="u",
        enterprise_id="e",
        title="t",
        description=STATEMENT,
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement=STATEMENT,
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=datetime.now(UTC),
        ),
        problem_verification=ProblemVerification(
            symptom_statement=STATEMENT, severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 4
    case.progress.problem_status = status
    from faultmaven.core.investigation.problem_status import (
        record_confirmed_statement,
    )

    record_confirmed_statement(case)
    seed_problem_node(case)
    return case


def _evidence(category: EvidenceCategory, label: str) -> Evidence:
    return Evidence(
        evidence_id="ev_" + hashlib.md5(label.encode()).hexdigest()[:12],
        category=category,
        primary_purpose="p",
        summary=f"fact-{label} reading-{label}",
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="user",
        collected_at_turn=3,
    )


def _with(case: Case, *rows: Evidence) -> list[str]:
    case.evidence.extend(rows)
    return [r.evidence_id for r in rows]


def _row(category: EvidenceCategory, label: str) -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=f"fact-{label} metric-{label} reading-{label}",
        extract=f"observation {label}: value-{label} at host-{label}",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
    )


def _symptom_need(case: Case) -> EvidenceNeed:
    need = EvidenceNeed(
        need_id="eneed_00000000000a",
        created_at_turn=2,
        case_id=case.case_id,
        purpose=NeedPurpose.SYMPTOM_VERIFICATION,
        request_text="database latency for the checkout window",
        rationale="shows the slowness the user reported",
    )
    case.evidence_needs.append(need)
    return need


def _propose(case: Case, text: str = REVISED, *, evidence=None) -> None:
    ids = (
        evidence
        if evidence is not None
        else _with(case, _evidence(EvidenceCategory.SYMPTOM_EVIDENCE, "s9"))
    )
    propose_revision(
        case,
        text=text,
        evidence_ids=ids,
        basis="the gateway log shows timeouts with normal DB latency",
        offer_key=revision_offer_key(text),
    )


class _NoKb:
    async def prefetch_kb_context(self, *args, **kwargs) -> None:
        return None


def _engine(agent_response: str = "Noted.") -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response=agent_response, state_updates={}
        )
    )
    engine.kb_prefetcher.prefetch_kb_context = AsyncMock(return_value=None)
    return engine


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


class _Response:
    def __init__(self, justification: str | None = None):
        self.evidence_trail = EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(
                symptom_verified=justification
            ),
        )


async def _apply(engine, case, dsu, response=None) -> dict:
    meta = _meta()
    await engine.responses._apply_investigation_updates(
        case, dsu, meta, response or _Response()
    )
    return meta


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestRevisionGuard:
    def _refusal(self, case, text=REVISED, evidence=None, basis="differs"):
        ids = (
            evidence
            if evidence is not None
            else _with(case, _evidence(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"))
        )
        return revision_refusal(case, text, ids, basis, revision_offer_key(text))

    def test_a_grounded_symptom_level_revision_is_accepted(self):
        assert self._refusal(_case()) is None

    @pytest.mark.parametrize(
        "text, needle",
        [("", "empty"), ("x" * 501, "500 characters"), (STATEMENT, "restates")],
    )
    def test_the_wording_is_checked(self, text, needle):
        assert needle in self._refusal(_case(), text=text)

    def test_it_needs_a_basis_and_symptom_evidence(self):
        case = _case()
        assert "differs" in self._refusal(case, basis="  ")
        causal = _with(case, _evidence(EvidenceCategory.CAUSAL_EVIDENCE, "c1"))
        assert "symptom evidence" in self._refusal(case, evidence=causal)

    def test_a_cause_shaped_revision_is_refused(self):
        case = _case(ProblemStatus.VERIFIED)
        case.hypotheses["hyp_0000000000aa"] = Hypothesis(
            hypothesis_id="hyp_0000000000aa",
            statement=CAUSE,
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            generated_at_turn=2,
        )
        assert "never why" in self._refusal(case, text=CAUSE)

    def test_acted_on_statements_are_history(self):
        identified = _case(ProblemStatus.VERIFIED)
        identified.progress.cause_state = CauseState.IDENTIFIED
        assert "identified" in self._refusal(identified)

        mitigated = _case(ProblemStatus.VERIFIED)
        mitigated.progress.mitigation = MitigationRecord(
            proposed_at_turn=1, accepted=True, verified=True
        )
        assert "mitigation" in self._refusal(mitigated)

        fixed = _case(ProblemStatus.VERIFIED)
        fixed.progress.solution_verified = True
        assert "fix" in self._refusal(fixed)

    def test_an_accepted_but_unverified_fix_does_not_bar_a_revision(self):
        """A failed fix is exactly when a mis-statement surfaces."""
        case = _case(ProblemStatus.VERIFIED)
        case.progress.solution_accepted = True
        assert self._refusal(case) is None

    def test_a_declined_wording_is_not_proposed_again(self):
        case = _case()
        _propose(case)
        decline_revision(case)
        assert "declined" in self._refusal(case)


class TestInvalidationGuard:
    def test_it_needs_cited_symptom_absence_and_a_basis(self):
        case = _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        assert invalidation_refusal(case, absent, "nothing failed") is None
        assert "absence" in invalidation_refusal(case, [], "nothing failed")
        assert "never existed" in invalidation_refusal(case, absent, " ")

    def test_a_confirmed_elimination_proves_the_problem_existed(self):
        case = _case(ProblemStatus.VERIFIED)
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        case.evidence.append(_evidence(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g"))
        assert "eliminated" in invalidation_refusal(case, absent, "nothing failed")

    def test_the_cause_leg_alone_proves_the_problem_existed(self):
        """#1906: a cause observed removed is not yet a resolution confirmation
        (the problem leg is missing), but it still proves there was a problem."""
        case = _case(ProblemStatus.VERIFIED)
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        cause_gone = _evidence(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g")
        cause_gone.collected_at_turn = 5  # after the quiet reading at turn 3
        case.evidence.append(cause_gone)
        assert not has_resolution_confirmation(case)
        assert invalidation_refusal(case, absent, "nothing failed") == (
            "a cause was confirmed eliminated, so the problem existed"
        )

    def test_only_an_unverified_or_verified_problem_is_found_false(self):
        case = _case()
        _propose(case)
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        assert "revision_pending" in invalidation_refusal(case, absent, "b")


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_commit_writes_every_store_and_verifies(self):
        case = _case()
        need = _symptom_need(case)
        _propose(case)
        commit_revision(case)

        problem = next(
            n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
        )
        assert case.description == REVISED
        assert case.problem_verification.symptom_statement == REVISED
        assert problem.statement == REVISED
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert need.state == NeedState.SUPERSEDED
        kinds = [r.kind for r in case.problem_verification.statement_history]
        assert kinds == [StatementRecordKind.CONFIRMED, StatementRecordKind.REVISED]
        assert case.problem_verification.statement_history[0].text == STATEMENT
        assert case.inquiry.proposed_problem_statement == STATEMENT

    def test_decline_and_cancel_return_to_where_the_case_was(self):
        case = _case(ProblemStatus.VERIFIED)
        _propose(case)
        decline_revision(case)
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.description == STATEMENT
        assert case.problem_verification.declined_revision_keys == [
            revision_offer_key(REVISED)
        ]

        _propose(case, text=REVISED + " (eu-west)")
        cancel_revision(case)
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert len(case.problem_verification.declined_revision_keys) == 1

    def test_a_replacing_proposal_keeps_the_original_prior_status(self):
        case = _case(ProblemStatus.VERIFIED)
        _propose(case)
        _propose(case, text=REVISED + " for EU users")
        assert case.problem_verification.pending_revision.prior_status == (
            ProblemStatus.VERIFIED
        )

    def test_invalidation_and_its_withdrawal(self):
        case = _case()
        need = _symptom_need(case)
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        assert need.state == NeedState.SUPERSEDED

        assert withdraw_invalidation(case, basis="the user saw the outage too")
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert case.problem_verification.invalidation is None
        assert [r.kind for r in case.problem_verification.statement_history][-2:] == [
            StatementRecordKind.INVALIDATED,
            StatementRecordKind.INVALIDATION_WITHDRAWN,
        ]

    def test_a_withdrawn_finding_returns_a_verified_problem_to_verified(self):
        case = _case(ProblemStatus.VERIFIED)
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        assert withdraw_invalidation(case, basis="the user saw the outage too")
        assert case.progress.problem_status == ProblemStatus.VERIFIED

    def test_an_edit_longer_than_the_problem_node_holds_is_refused(self):
        case = _case(ProblemStatus.VERIFIED)
        assert "exceeds" in edit_statement_refusal(case, "x" * 501)
        assert edit_statement_refusal(case, "x" * 500) is None

    def test_an_edit_supersedes_the_open_symptom_needs(self):
        case = _case()
        need = _symptom_need(case)
        edit_statement(case, REVISED)
        assert need.state == NeedState.SUPERSEDED
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED

    async def test_an_edit_clears_a_false_alarm_and_withdraws_its_close(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no errors in the window",
                ),
            ),
        )
        assert case.pending_transition
        edit_statement(case, REVISED)
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert case.problem_verification.invalidation is None
        assert case.pending_transition is None
        assert derive_closure_reason(case) != FALSE_ALARM_CLOSURE_REASON

    def test_an_edit_on_a_case_missing_its_verification_record_creates_it(self):
        case = _case(ProblemStatus.VERIFIED)
        case.problem_verification = None
        assert edit_statement_refusal(case, REVISED) is None
        edit_statement(case, REVISED)
        assert case.problem_verification.symptom_statement == REVISED
        assert case.description == REVISED

    def test_a_user_edit_moves_every_store_but_not_the_status(self):
        case = _case(ProblemStatus.VERIFIED)
        assert edit_statement_refusal(case, "  ") is not None
        edit_statement(case, REVISED)
        assert case.problem_verification.symptom_statement == REVISED
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        _propose(case, text=REVISED + " only")
        assert "awaiting confirmation" in edit_statement_refusal(case, "x")


# ---------------------------------------------------------------------------
# The apply step (2c) and staging (2d)
# ---------------------------------------------------------------------------


def _revision_update(*refs: str) -> ProblemVerificationUpdate:
    return ProblemVerificationUpdate(
        revised_problem_statement=REVISED,
        revision_evidence_ids=list(refs),
        revision_basis="the gateway log shows timeouts with normal DB latency",
    )


class TestApplyStep:
    async def test_a_grounded_revision_waits_for_the_user(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert meta["revision_proposed_this_turn"] is True
        assert check_if_progress_made(meta)
        gate, pair = engine_owned_affordances(case)
        assert gate == "statement_revision"
        assert len(pair) == 2

    async def test_the_pending_wording_sent_again_is_not_progress(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        proposed_at = case.problem_verification.pending_revision.proposed_at_turn
        case.current_turn += 1
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s2")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert "problem_status_changed" not in meta
        assert case.problem_verification.pending_revision.proposed_at_turn == (
            proposed_at
        )

    async def test_a_same_turn_verification_is_granted_on_confirmation_instead(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
            _Response("timeouts in the gateway log"),
        )
        pending = case.problem_verification.pending_revision
        assert pending.prior_status == ProblemStatus.UNVERIFIED
        assert "symptom_verified" not in meta["milestones_completed"]

    async def test_a_revision_and_a_false_alarm_together_are_both_refused(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[
                    _row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"),
                    _row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1"),
                ],
                verification_updates=ProblemVerificationUpdate(
                    revised_problem_statement=REVISED,
                    revision_evidence_ids=["new_index_0"],
                    revision_basis="b",
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_1"],
                    invalidation_basis="b",
                ),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert "contradict" in meta["system_feedback"]

    async def test_a_false_alarm_offers_the_close_and_only_the_close(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no 5xx in the gateway log for the window",
                ),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        pending = case.pending_transition
        assert pending["to_state"] == "closed"
        assert pending["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        assert "justifying_signature" in pending
        assert "false alarm" in meta["false_alarm_closure_message"]
        readiness = assess_resolution_readiness(case)
        assert readiness.verdict == readiness.SUGGEST_CLOSE
        eligibility = derive_disposition_eligibility(case)
        assert eligibility["resolved"] == "not_eligible"
        assert (
            assess_verification_status(case) == VerificationStatus.PROBLEM_INVALIDATED
        )

    async def test_a_false_alarm_on_the_turn_that_verified_is_refused(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[
                    _row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"),
                    _row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1"),
                ],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_1"],
                    invalidation_basis="b",
                ),
            ),
            _Response("errors in the log"),
        )
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert "same response verified" in meta["system_feedback"]

    async def test_a_revision_from_a_false_alarm_withdraws_the_engine_close(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="the latency alert misfired",
                ),
            ),
        )
        assert case.pending_transition
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert case.pending_transition is None
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert case.problem_verification.pending_revision.prior_status == (
            ProblemStatus.INVALIDATED
        )

    async def test_a_revision_cannot_cut_in_on_another_pending_transition(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        from faultmaven.core.investigation.terminal_transitions import (
            propose_transition,
        )

        propose_transition(case, to_state="closed", summary="close?")
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert "awaiting the user's answer" in meta["system_feedback"]

    async def test_a_disputed_false_alarm_is_withdrawn_with_its_close(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no errors in the window",
                ),
            ),
        )
        meta = await _apply(
            engine,
            case,
            _DSU(
                verification_updates=ProblemVerificationUpdate(
                    invalidation_withdrawn=True,
                    withdrawal_basis="the user has customer tickets from that hour",
                )
            ),
        )
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert case.pending_transition is None
        assert meta["problem_status_changed"] is True

    async def test_a_causal_absence_before_verification_is_symptom_absence(self):
        engine, case = _engine(), _case()
        meta = await _apply(
            engine,
            case,
            _DSU(evidence_to_add=[_row(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g")]),
        )
        (row,) = case.evidence
        assert row.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
        assert meta["evidence_added"] == [row.evidence_id]
        readiness = assess_resolution_readiness(case)
        assert readiness.verdict != readiness.READY

    async def test_a_causal_absence_is_judged_after_an_unsupported_claim_reverts(self):
        """Step 1 verifies optimistically; 2b reverts a claim no evidence
        supports. The row is judged against the reverted status."""
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[_row(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g")],
            ),
        )
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        (row,) = case.evidence
        assert row.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE

    async def test_a_false_alarm_may_cite_a_row_reclassified_the_same_turn(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no errors in the window",
                ),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.INVALIDATED

    async def test_a_causal_absence_on_a_revision_turn_is_symptom_absence(self):
        engine, case = _engine(), _case()
        await _apply(
            engine,
            case,
            _DSU(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[
                    _row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"),
                    _row(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g"),
                ],
                verification_updates=_revision_update("new_index_0"),
            ),
            _Response("timeouts in the gateway log"),
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert case.evidence[1].category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE

    async def test_a_causal_absence_on_a_verified_problem_stays_causal(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        await _apply(
            engine,
            case,
            _DSU(evidence_to_add=[_row(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "g")]),
        )
        (row,) = case.evidence
        assert row.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE

    async def test_a_false_alarm_accepts_no_mitigation_signal(self):
        engine, case = _engine(), _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        case.progress.mitigation = MitigationRecord(
            description="fail over", proposed_at_turn=3
        )
        meta = await _apply(
            engine,
            case,
            _DSU(
                milestones=MilestoneUpdates(
                    mitigation_accepted=True, mitigation_verified=True
                )
            ),
        )
        assert not case.progress.mitigation.verified
        assert "mitigation_accepted, mitigation_verified NOT ACCEPTED" in (
            meta["system_feedback"]
        )

    async def test_rca_infeasible_is_applied(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        await _apply(
            engine,
            case,
            _DSU(
                verification_updates=ProblemVerificationUpdate(
                    rca_infeasible=True,
                    rca_infeasible_rationale="black-box vendor API",
                )
            ),
        )
        assert case.problem_verification.rca_infeasible is True
        assert case.problem_verification.rca_infeasible_rationale == (
            "black-box vendor API"
        )


# ---------------------------------------------------------------------------
# Staging and the confirmation turn — the opportunistic flow across the
# handshake
# ---------------------------------------------------------------------------


def _revision_turn_with_cause_work() -> _DSU:
    """One emission: the evidence shows a different problem AND its cause."""
    return _DSU(
        evidence_to_add=[
            _row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"),
            _row(EvidenceCategory.CAUSAL_EVIDENCE, "a1"),
            _row(EvidenceCategory.CAUSAL_EVIDENCE, "a2"),
        ],
        verification_updates=_revision_update("new_index_0"),
        hypotheses_to_add=[
            HypothesisToAdd(
                statement=CAUSE,
                category=HypothesisCategory.CODE,
                likelihood=0.6,
                rationale="timeouts start at pool exhaustion",
                root_node_ref="new_index_0",
            )
        ],
        causal_nodes_to_add=[
            CausalNodeToAdd(statement=CAUSE, node_type="root", produces="D")
        ],
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref="new_index_0",
                evidence_id_ref=f"new_index_{i}",
                stance=EvidenceStance.SUPPORTS,
                reasoning="the pool saturates exactly when requests time out",
            )
            for i in (1, 2)
        ],
    )


class TestStagingAcrossTheHandshake:
    async def test_cause_work_on_the_revision_turn_is_staged_not_applied(self):
        engine, case = _engine(), _case()
        meta = await _apply(engine, case, _revision_turn_with_cause_work())

        assert case.hypotheses == {}
        assert [n.node_type for n in case.causal_nodes.values()] == [NodeType.PROBLEM]
        (bundle,) = case.problem_verification.pending_revision.staged
        assert bundle.evidence_added == meta["evidence_added"]
        assert set(bundle.updates) >= {
            "hypotheses_to_add",
            "causal_nodes_to_add",
            "node_evidence_links",
        }
        assert "NOT ACCEPTED" not in meta.get("system_feedback", "")

    async def test_the_confirmation_turn_verifies_forms_and_identifies(self):
        """The opportunism pin through the handshake: a bare "yes" commits the
        revision before the LLM call and replays the staged work — the
        hypothesis forms, the chain grounds and the cause is identified on the
        confirmation turn, with nothing re-emitted."""
        engine, case = _engine(), _case()
        await _apply(engine, case, _revision_turn_with_cause_work())
        case.current_turn += 1

        result = await engine.process_turn(case=case, user_message="yes")

        updated = result["case_updated"]
        assert updated.progress.problem_status == ProblemStatus.VERIFIED
        assert updated.description == REVISED
        (hyp,) = updated.hypotheses.values()
        assert hyp.root_node_id is not None
        assert updated.progress.cause_state == CauseState.IDENTIFIED
        assert updated.problem_verification.pending_revision is None
        # The replay ran before the LLM's response metadata replaced the turn's
        # dict; the turn record still carries what it produced.
        record = updated.turn_history[-1]
        assert record.hypotheses_generated == [hyp.hypothesis_id]
        assert record.progress_made is True

    async def test_a_yes_to_the_revision_never_executes_an_offer_the_replay_made(self):
        """The replay runs the apply path end to end, engine proposers
        included. A deferred fix staged with its cause must not turn the
        user's "yes" to the revised statement into consent to close: the
        proposer is inert inside the replay, and the live turn makes any
        offer with its own card, unexecuted."""
        from faultmaven.core.investigation.schemas import SolutionToAdd

        engine, case = _engine(), _case()
        dsu = _revision_turn_with_cause_work()
        dsu.milestones = MilestoneUpdates(solution_feasible="deferred")
        dsu.solutions_to_add = [
            SolutionToAdd(
                solution_type="config_change",
                description="raise the orders pool to 50 connections",
                estimated_impact="clears the timeouts",
                risks="more DB connections",
            )
        ]
        await _apply(engine, case, dsu)
        case.current_turn += 1

        result = await engine.process_turn(case=case, user_message="yes")

        updated = result["case_updated"]
        assert updated.state == CaseState.INVESTIGATING
        assert updated.progress.problem_status == ProblemStatus.VERIFIED
        # The live turn makes the offer the replayed state warrants — the
        # cause is identified and the fix deferred — with its card.
        from faultmaven.core.investigation.milestone_engine.transition_consent import (
            terminal_offer_key,
        )

        assert updated.pending_transition["to_state"] == "closed"
        keys = {
            (f.get("intent") or {}).get("proposal_id")
            for f in result["suggested_follow_ups"]
        }
        assert keys == {terminal_offer_key(updated.pending_transition)}

    async def test_an_acceptance_of_a_staged_fix_is_held_with_it_and_staged_once(self):
        from faultmaven.core.investigation.schemas import SolutionToAdd

        def fix() -> SolutionToAdd:
            return SolutionToAdd(
                solution_type="config_change",
                description="Raise the orders pool to 50 connections",
                estimated_impact="clears the timeouts",
                risks="more DB connections",
            )

        engine, case = _engine(), _case()
        dsu = _revision_turn_with_cause_work()
        dsu.solutions_to_add = [fix()]
        dsu.milestones = MilestoneUpdates(solution_accepted=True)
        meta = await _apply(engine, case, dsu)

        assert "was not registered" not in meta["system_feedback"]
        assert "held until the user confirms" in meta["system_feedback"]
        (bundle,) = case.problem_verification.pending_revision.staged
        assert bundle.updates["milestones"]["solution_accepted"] is True
        assert case.progress.solution_accepted is False

        # The same fix re-sent on the next hold turn is not staged twice.
        case.current_turn += 1
        await _apply(engine, case, _DSU(solutions_to_add=[fix()]))
        staged = [
            item
            for b in case.problem_verification.pending_revision.staged
            for item in b.updates.get("solutions_to_add", [])
        ]
        assert len(staged) == 1

    async def test_a_re_root_while_the_revision_waits_is_refused_not_lost(self):
        """Hypothesis updates apply while a revision waits, but a re-root is
        chain structure: it is refused with a note, never dropped in silence."""
        from faultmaven.core.investigation.schemas import HypothesisUpdate

        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        case.hypotheses["hyp_0000000000dd"] = Hypothesis(
            hypothesis_id="hyp_0000000000dd",
            statement=CAUSE,
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            generated_at_turn=1,
        )
        root = CausalNode(
            node_id="cn_0000000000ee",
            statement="an unrelated root",
            node_type=NodeType.ROOT,
            node_state=NodeState.CANDIDATE,
            validation_method=ValidationMethod.NONE,
            belief=0.5,
            actionable=True,
            generated_at_turn=1,
        )
        case.causal_nodes[root.node_id] = root
        _propose(case)

        meta = await _apply(
            engine,
            case,
            _DSU(
                hypotheses_to_update=[
                    HypothesisUpdate(
                        hypothesis_id="hyp_0000000000dd", root_node_ref=root.node_id
                    )
                ]
            ),
        )
        assert case.hypotheses["hyp_0000000000dd"].root_node_id is None
        assert "hypothesis root refs" in meta["system_feedback"]

    async def test_a_yes_never_executes_an_rca_close_the_replay_made(self):
        """The stage-gate side effect proposes 'close as stabilized' when a
        mitigation verifies on an rca_infeasible problem. Replayed, it must
        still arrive as an offer with its card, never executed by the "yes"
        that confirmed the statement (#1871 review)."""
        from faultmaven.core.investigation.milestone_engine.transition_consent import (
            terminal_offer_key,
        )
        from faultmaven.core.investigation.schemas import SolutionToAdd

        engine, case = _engine(), _case()
        case.problem_verification.rca_infeasible = True
        case.problem_verification.rca_infeasible_rationale = "black-box vendor API"
        await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
                solutions_to_add=[
                    SolutionToAdd(
                        solution_type="workaround",
                        description="route checkout through the backup region",
                        estimated_impact="restores checkout",
                        risks="higher latency",
                    )
                ],
                milestones=MilestoneUpdates(
                    mitigation_accepted=True, mitigation_verified=True
                ),
            ),
        )
        case.current_turn += 1

        result = await engine.process_turn(case=case, user_message="yes")

        updated = result["case_updated"]
        assert updated.state == CaseState.INVESTIGATING
        assert updated.pending_transition["to_state"] == "closed"
        keys = {
            (f.get("intent") or {}).get("proposal_id")
            for f in result["suggested_follow_ups"]
        }
        assert keys == {terminal_offer_key(updated.pending_transition)}

    async def test_a_decline_note_survives_the_live_turns_own_feedback(self):
        """Feedback written before the LLM call (the decline note) is merged
        with the live turn's own, never replaced by it (#1871 review)."""
        engine, case = _engine(), _case()
        _propose(case)
        engine.generator.generate_structured_output = AsyncMock(
            return_value=InvestigationResponse_Diagnosis(
                agent_response="Understood.",
                state_updates={
                    "hypotheses_to_add": [
                        {
                            "statement": CAUSE,
                            "category": "database",
                            "likelihood": 0.5,
                            "rationale": "r",
                        }
                    ]
                },
            )
        )

        await engine.process_turn(case=case, user_message="no")

        feedback = case.turn_history[-1].system_feedback or ""
        assert "declined the revised problem statement" in feedback
        assert "CAUSE WORK NOT ACCEPTED" in feedback

    async def test_a_decline_discards_the_stage(self):
        engine, case = _engine(), _case()
        await _apply(engine, case, _revision_turn_with_cause_work())
        case.current_turn += 1

        result = await engine.process_turn(case=case, user_message="no")

        updated = result["case_updated"]
        assert updated.progress.problem_status == ProblemStatus.UNVERIFIED
        assert updated.description == STATEMENT
        assert updated.hypotheses == {}
        assert updated.problem_verification.pending_revision is None
        feedback = updated.turn_history[-1].system_feedback or ""
        assert "(1 hypothesis(es), 1 causal node(s)) was discarded" in feedback


# ---------------------------------------------------------------------------
# The handshake's other doors
# ---------------------------------------------------------------------------


class TestHandshakeDoors:
    async def test_the_card_is_composed_with_the_reply_and_names_its_offer(self):
        engine, case = _engine("Here is what the log shows."), _case()
        _propose(case)
        result = await engine.process_turn(
            case=case, user_message="what made you think that?"
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert REVISED in result["agent_response"]
        assert "Here is what the log shows." in result["agent_response"]
        keys = {
            (f.get("intent") or {}).get("proposal_id")
            for f in result["suggested_follow_ups"]
        }
        assert keys == {revision_offer_key(REVISED)}

    async def test_a_stale_click_commits_nothing(self):
        engine, case = _engine(), _case()
        _propose(case)
        await engine.process_turn(
            case=case,
            user_message="Yes, the revised problem statement is right.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": revision_offer_key("other")},
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert case.description == STATEMENT

    async def test_a_click_on_the_offer_commits(self):
        engine, case = _engine(), _case()
        _propose(case)
        await engine.process_turn(
            case=case,
            user_message="Yes, the revised problem statement is right.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": revision_offer_key(REVISED)},
        )
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.description == REVISED

    @pytest.mark.parametrize(
        "message, swallowed",
        [
            ("yes", False),
            ("Yes, use the revised statement", True),
            ("yes but it is only the EU region, isn't it?", True),
        ],
    )
    def test_a_minted_confirmation_commits_only_on_a_bare_consent(
        self, message, swallowed
    ):
        """The resolver may mint a confirmation from typed text matched against
        the card; like Gate 1, only one bare consent token may commit."""
        from faultmaven.models.api_models import IntentType, QueryIntent
        from faultmaven.modules.agent.domain.services.investigation_service.intent_gates import (
            _minted_intent_swallows_gate_consent,
        )

        case = _case()
        _propose(case)
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert _minted_intent_swallows_gate_consent(case, minted, message) is swallowed

    async def test_a_minted_decline_declines(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        _propose(case)
        await engine.process_turn(
            case=case,
            user_message="No, the original statement is correct",
            intent_type="confirmation",
            intent_data={"value": False},
            typed=True,
        )
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.problem_verification.pending_revision is None
        assert case.description == STATEMENT

    async def test_the_model_cannot_propose_a_transition_meanwhile(self):
        engine, case = _engine(), _case()
        _propose(case)
        response = MagicMock()
        response.state_updates.proposed_transition = ProposedTransition(
            to_state="closed"
        )
        metadata: dict = {"response_obj": response}
        await engine.transitions.check_automatic_transitions(case, metadata, "close")
        assert case.pending_transition is None
        assert "awaiting the user's confirmation" in metadata["system_feedback"]

    async def test_the_users_own_close_cancels_the_revision(self):
        case = _case(ProblemStatus.VERIFIED)
        _propose(case)
        reason = execute_user_closure(case, "u")
        assert case.state == CaseState.CLOSED
        assert reason == "closed_insufficient_evidence"
        assert case.description == STATEMENT

    async def test_the_status_menu_close_cancels_the_revision(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        _propose(case)
        await engine.process_turn(
            case=case,
            user_message="",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.pending_transition["to_state"] == "closed"


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


class TestHolds:
    async def test_a_hold_turn_is_not_a_stall_and_ages_nothing(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        case.hypotheses["hyp_0000000000bb"] = Hypothesis(
            hypothesis_id="hyp_0000000000bb",
            statement=CAUSE,
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            generated_at_turn=1,
            likelihood=0.5,
            iterations_without_progress=2,
            last_progress_at_turn=1,
            last_updated_turn=3,
        )
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        case.progress.problem_status = ProblemStatus.UNVERIFIED
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        case.turns_without_progress = 6

        await engine.process_turn(case=case, user_message="ok, noted")

        hyp = case.hypotheses["hyp_0000000000bb"]
        assert case.turns_without_progress == 6
        assert hyp.likelihood == 0.5
        assert hyp.state == HypothesisState.ACTIVE
        assert engine_owned_affordances(case) is None

    async def test_a_false_alarm_refuses_fixes_and_hypothesis_updates(self):
        from faultmaven.core.investigation.schemas import (
            HypothesisUpdate,
            SolutionToAdd,
        )

        engine, case = _engine(), _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        meta = await _apply(
            engine,
            case,
            _DSU(
                solutions_to_add=[
                    SolutionToAdd(
                        solution_type="workaround",
                        description="restart the pool",
                        estimated_impact="clears the queue",
                        risks="drops in-flight requests",
                    )
                ],
                hypotheses_to_update=[
                    HypothesisUpdate(hypothesis_id="hyp_0000000000bb", likelihood=0.1)
                ],
            ),
        )
        assert case.solutions == []
        assert case.proposed_actions == []
        assert "SOLUTIONS NOT ACCEPTED" in meta["system_feedback"]
        assert "HYPOTHESIS UPDATES NOT ACCEPTED" in meta["system_feedback"]

    async def test_a_false_alarm_refuses_evidence_links_on_standing_hypotheses(self):
        from faultmaven.core.investigation.schemas import HypothesisEvidenceLinkToAdd

        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        case.hypotheses["hyp_0000000000bb"] = Hypothesis(
            hypothesis_id="hyp_0000000000bb",
            statement=CAUSE,
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            generated_at_turn=1,
            likelihood=0.5,
        )
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        refuting = _with(case, _evidence(EvidenceCategory.CAUSAL_EVIDENCE, "r"))
        case.progress.problem_status = ProblemStatus.UNVERIFIED
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")

        meta = await _apply(
            engine,
            case,
            _DSU(
                hypothesis_evidence_links=[
                    HypothesisEvidenceLinkToAdd(
                        hypothesis_id_ref="hyp_0000000000bb",
                        evidence_id_ref=refuting[0],
                        stance=EvidenceStance.REFUTES,
                        reasoning="pool metrics were flat",
                    )
                ]
            ),
        )
        hyp = case.hypotheses["hyp_0000000000bb"]
        assert hyp.likelihood == 0.5
        assert hyp.evidence_links == []
        assert "HYPOTHESIS EVIDENCE LINKS NOT ACCEPTED" in meta["system_feedback"]

    def test_both_holds_have_their_own_status(self):
        case = _case()
        _propose(case)
        assert assess_verification_status(case) == VerificationStatus.REVISION_PENDING


# ---------------------------------------------------------------------------
# Closure, reports and the UI
# ---------------------------------------------------------------------------


class TestClosure:
    def test_a_false_alarm_closes_as_one_and_never_pivots_to_resolve(self):
        case = _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        assert derive_closure_reason(case) == "closed_false_alarm"
        assert assess_closure_readiness(case).verdict != "suggest_resolve"

    async def test_the_closure_summary_states_the_finding(self):
        case = _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        execute_user_closure(case, "u")
        summary = await ReportGenerationService.__new__(
            ReportGenerationService
        )._generate_closure_summary(case, {"duration": "1h"})
        assert "false alarm" in summary
        assert "the alert misfired" in summary
        assert absent[0] in summary
        assert "Review the signal" in summary

    def test_a_revised_case_reports_what_was_originally_reported(self):
        case = _case()
        _propose(case)
        commit_revision(case)
        lines = ReportGenerationService._problem_statement_section(case)
        assert REVISED in lines[1]
        assert f"Originally reported as: {STATEMENT}" in "".join(lines)

    def test_the_ui_says_where_the_problem_stands(self):
        case = _case()
        _propose(case)
        data = _extract_problem_verification(case)
        assert data.problem_status == "revision_pending"
        assert data.pending_revision == REVISED
        commit_revision(case)
        data = _extract_problem_verification(case)
        assert data.original_problem_statement == STATEMENT
        assert data.pending_revision is None


class TestTerminalCaseRead:
    """#1874 (contract 11.3.0): a resolved or closed case's read still says
    where its problem statement stood, so the header stops stating a false
    alarm's problem as fact once the case ends."""

    def test_a_closed_false_alarm_carries_the_finding(self):
        case = _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        assert execute_user_closure(case, "u") == FALSE_ALARM_CLOSURE_REASON

        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Resolved)
        assert result.state == CaseState.CLOSED
        assert result.problem_statement == STATEMENT
        pv = result.model_dump(mode="json")["problem_verification"]
        assert pv["problem_status"] == "invalidated"
        assert pv["invalidation_finding"] == "the alert misfired"
        assert pv["original_problem_statement"] is None
        assert pv["pending_revision"] is None

    def test_a_resolved_revised_case_says_what_was_originally_reported(self):
        case = _case(ProblemStatus.VERIFIED)
        _propose(case)
        commit_revision(case)
        _execute_resolved_transition(case, "u")

        result = transform_case_for_ui(case)

        assert result.state == CaseState.RESOLVED
        assert result.problem_statement == REVISED
        pv = result.problem_verification
        assert pv.problem_status == ProblemStatus.VERIFIED
        assert pv.original_problem_statement == STATEMENT
        assert pv.invalidation_finding is None

    def test_a_close_cancels_a_pending_revision_rather_than_carrying_it(self):
        # No one can confirm a revision on a closed case: the user's close
        # cancels it, so the terminal read never offers one.
        case = _case(ProblemStatus.VERIFIED)
        _propose(case)
        execute_user_closure(case, "u")

        pv = transform_case_for_ui(case).problem_verification

        assert pv.problem_status == ProblemStatus.VERIFIED
        assert pv.pending_revision is None
        assert pv.original_problem_statement is None

    def test_a_case_closed_unverified_carries_no_status_a_client_renders(self):
        # Closed without the evidence ever bearing on the statement: the
        # status is `unverified`, which both clients render as before.
        case = _case()
        execute_user_closure(case, "u")

        pv = transform_case_for_ui(case).problem_verification

        assert pv.problem_status == ProblemStatus.UNVERIFIED
        assert pv.invalidation_finding is None
        assert pv.original_problem_statement is None

    def test_a_case_closed_from_inquiry_carries_none(self):
        # Gate 1 never ran: no statement was confirmed, so none is judged.
        case = Case(
            case_id="case_0000000000bb",
            user_id="u",
            enterprise_id="e",
            title="t",
            description="maybe the checkout database is slow",
        )
        assert execute_user_closure(case, "u") == "inquiry_only"

        result = transform_case_for_ui(case)

        assert result.state == CaseState.CLOSED
        assert result.problem_verification is None


def test_a_node_is_retexted_in_place_so_chains_keep_their_anchor():
    case = _case(ProblemStatus.VERIFIED)
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    root = CausalNode(
        node_id="cn_0000000000cc",
        statement=CAUSE,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=1,
    )
    case.causal_nodes[root.node_id] = root
    _propose(case)
    commit_revision(case)
    assert problem.node_id in case.causal_nodes
    assert case.causal_nodes[problem.node_id].statement == REVISED


# ---------------------------------------------------------------------------
# A false-alarm close never outlives its finding, whoever proposed it (#1876)
# ---------------------------------------------------------------------------

_ENGINE_WITHDRAWN_KEY = "engine_disposition_withdrawn_this_turn"


def _false_alarm_case(*, model_close: bool) -> Case:
    """INVALIDATED, with the close either the model's (no signature) or the
    engine's (signed, as ``_maybe_propose_false_alarm_close`` writes it)."""
    case = _case()
    absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
    invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
    propose_transition(case, to_state="closed", summary="close?")
    if not model_close:
        case.pending_transition["justifying_signature"] = "sig"
    assert case.pending_transition["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
    return case


def _withdraw_update() -> _DSU:
    return _DSU(
        verification_updates=ProblemVerificationUpdate(
            invalidation_withdrawn=True,
            withdrawal_basis="the user has customer tickets from that hour",
        )
    )


class TestFalseAlarmCloseFollowsItsFinding:
    async def test_a_withdrawal_takes_back_the_models_close(self):
        engine, case = _engine(), _false_alarm_case(model_close=True)
        await _apply(engine, case, _withdraw_update())
        assert case.pending_transition is None
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert confirm_pending_transition(case, "u") is False
        assert case.state == CaseState.INVESTIGATING
        result = await engine.process_turn(case=case, user_message="yes")
        assert result["case_updated"].state == CaseState.INVESTIGATING

    async def test_an_edit_takes_back_the_models_close(self):
        case = _false_alarm_case(model_close=True)
        edit_statement(case, "The checkout page returns 502 at peak")
        assert case.pending_transition is None
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED

    async def test_a_withdrawal_still_notes_the_engines_own_disposition(self):
        engine, case = _engine(), _false_alarm_case(model_close=False)
        meta = await _apply(engine, case, _withdraw_update())
        assert case.pending_transition is None
        assert meta[_ENGINE_WITHDRAWN_KEY] is True

    async def test_a_withdrawn_models_close_records_no_engine_disposition(self):
        engine, case = _engine(), _false_alarm_case(model_close=True)
        meta = await _apply(engine, case, _withdraw_update())
        assert case.pending_transition is None
        assert _ENGINE_WITHDRAWN_KEY not in meta

    def test_an_edit_leaves_a_close_that_is_not_a_false_alarm(self):
        case = _case(ProblemStatus.VERIFIED)
        propose_transition(case, to_state="closed", summary="close?")
        case.pending_transition["closure_reason"] = "closed_insufficient_evidence"
        before = dict(case.pending_transition)
        edit_statement(case, "The checkout page returns 502 at peak")
        assert case.pending_transition == before
        assert case.progress.problem_status == ProblemStatus.VERIFIED

    async def test_a_revision_still_cannot_cut_in_on_the_models_false_alarm_close(
        self,
    ):
        """The revision gate keys on who proposed the close: unchanged."""
        engine, case = _engine(), _false_alarm_case(model_close=True)
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert case.pending_transition is not None
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        assert "awaiting the user's answer" in meta["system_feedback"]
        # The engine's own close is still withdrawn by a revision:
        # TestApplyStep.test_a_revision_from_a_false_alarm_withdraws_the_engine_close

    async def test_the_status_menu_close_is_the_third_proposer(self):
        """The user's own close from the status menu on a false alarm derives
        the same reason, unsigned, and is taken back the same way."""
        engine, case = _engine(), _case()
        absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
        invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
        await engine.process_turn(
            case=case,
            user_message="",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        pending = case.pending_transition
        assert pending["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        assert "justifying_signature" not in pending
        edit_statement(case, "The checkout page returns 502 at peak")
        assert case.pending_transition is None

    async def test_dispute_then_yes_never_closes_on_a_withdrawn_finding(self):
        """The issue's repro: invalidate, the model proposes the close, the
        user disputes the finding, then answers yes."""
        engine, case = _engine(), _false_alarm_case(model_close=True)
        await _apply(engine, case, _withdraw_update())
        case.current_turn += 1
        result = await engine.process_turn(case=case, user_message="yes")
        updated = result["case_updated"]
        assert updated.state != CaseState.CLOSED
        reason = (updated.pending_transition or {}).get("closure_reason")
        assert not (
            reason == FALSE_ALARM_CLOSURE_REASON
            and updated.progress.problem_status != ProblemStatus.INVALIDATED
        )


# ---------------------------------------------------------------------------
# The engine's same-turn offer stands against the model's proposal (#1885)
# ---------------------------------------------------------------------------

_SIGNED = "justifying_signature"


def _respond(engine: MilestoneEngine, dsu: _DSU) -> None:
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=dsu
        )
    )


def _finding(*, model_proposes: str | None) -> _DSU:
    """The false-alarm finding, with the model's own transition beside it."""
    return _DSU(
        evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")],
        verification_updates=ProblemVerificationUpdate(
            problem_invalidated=True,
            invalidation_evidence_ids=["new_index_0"],
            invalidation_basis="no 5xx in the gateway log for the window",
        ),
        proposed_transition=(
            ProposedTransition(to_state=model_proposes) if model_proposes else None
        ),
    )


async def _finding_turn(*, model_proposes: str | None) -> tuple:
    engine, case = _engine(), _case()
    _respond(engine, _finding(model_proposes=model_proposes))
    result = await engine.process_turn(
        case=case, user_message="the gateway log shows no errors at 14:00"
    )
    case = result["case_updated"]
    assert case.progress.problem_status == ProblemStatus.INVALIDATED
    return engine, case, result


class TestTheEnginesFalseAlarmOfferStands:
    """On the turn the finding is made, the engine's signed close is what the
    user answers, whatever the model proposed beside it. Real ``process_turn``,
    stubbed generator."""

    @pytest.mark.parametrize("model_proposes", ["closed", "resolved"])
    async def test_the_models_same_turn_proposal_leaves_the_signed_offer(
        self, model_proposes
    ):
        _, case, result = await _finding_turn(model_proposes=model_proposes)
        pending = case.pending_transition
        assert pending["to_state"] == "closed"
        assert pending["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        assert pending[_SIGNED] == f"{FALSE_ALARM_CLOSURE_REASON}|4"
        # The card the user is shown names the offer that is standing.
        keys = {
            (f.get("intent") or {}).get("proposal_id")
            for f in result["suggested_follow_ups"]
        }
        assert keys == {pending["proposed_at"]}
        assert "false alarm" in result["agent_response"]

    async def test_the_dropped_proposal_is_reported_superseded_not_pivoted(self):
        engine, case = _engine(), _case()
        _respond(engine, _finding(model_proposes="resolved"))
        with patch.object(turn_completion.logger, "info") as info:
            result = await engine.process_turn(
                case=case, user_message="the gateway log shows no errors at 14:00"
            )
        (extra,) = [
            c.kwargs["extra"]
            for c in info.call_args_list
            if c.args == ("transition_compliance",)
        ]
        assert extra["llm_proposed_to_status"] == "resolved"
        assert extra["engine_effective_to_status"] == "closed"
        assert extra["transition_superseded_by_engine"] is True
        assert extra["transition_pivoted"] is False
        feedback = result["case_updated"].turn_history[-1].system_feedback or ""
        assert "TRANSITION NOT PROPOSED" in feedback

    async def test_a_bare_no_then_records_the_decline_on_the_finding(self):
        """The decline is a fact about the finding (#1889): recorded there, and
        never in the deferred-disposition signature list, where no reader
        matched it and it could only evict a live deferred refusal."""
        engine, case, _ = await _finding_turn(model_proposes="closed")
        case.current_turn += 1
        _respond(engine, _DSU())
        await engine.process_turn(case=case, user_message="no")
        assert case.pending_transition is None
        assert case.problem_verification.invalidation.close_declined_at_turn == 5
        assert case.progress.deferred_disposition_declined_signatures == []

    async def test_a_revision_then_withdraws_the_close(self):
        """INV-45: a revision may withdraw the ENGINE's false-alarm close.

        On a chat turn section 0b withdraws a pending close before the model is
        called, so the apply step's gate is the backstop for any path that
        reaches it with the close standing; it is driven directly here. On
        main the model's same-turn close had replaced the signed offer, so the
        gate read it as the model's and refused the revision."""
        engine, case, _ = await _finding_turn(model_proposes="closed")
        case.current_turn += 1
        meta = await _apply(
            engine,
            case,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert "STATEMENT REVISION NOT ACCEPTED" not in (
            meta.get("system_feedback") or ""
        )
        assert case.pending_transition is None
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        assert case.problem_verification.pending_revision.prior_status == (
            ProblemStatus.INVALIDATED
        )

    async def test_a_revision_on_the_chat_turn_is_accepted(self):
        """The reachable path: 0b withdraws the signed close (the substantive
        reply is a refusal, recorded), and the model's revision is accepted."""
        engine, case, _ = await _finding_turn(model_proposes="closed")
        case.current_turn += 1
        _respond(
            engine,
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        await engine.process_turn(
            case=case,
            user_message="The /orders API times out after 30s; the database is fine",
        )
        assert case.pending_transition is None
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING

    async def test_without_a_model_proposal_the_offer_is_signed(self):
        _, case, _ = await _finding_turn(model_proposes=None)
        assert case.pending_transition[_SIGNED] == f"{FALSE_ALARM_CLOSURE_REASON}|4"

    async def test_with_no_engine_offer_the_models_proposal_lands(self):
        """Negative control: the rule is about the engine's SAME-turn offer,
        not about the model's proposals in general."""
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        _respond(
            engine, _DSU(proposed_transition=ProposedTransition(to_state="closed"))
        )
        result = await engine.process_turn(case=case, user_message="let's stop here")
        pending = result["case_updated"].pending_transition
        assert pending["to_state"] == "closed"
        assert _SIGNED not in pending
