"""#1906 Phase 2a (design D1) and #2145: a post-fix observation counts only for
the subject it names, and only once its data was submitted after the fix.

Every row here enters through the real turn application
(``ResponseApplier._apply_investigation_updates``, which ends in the real
recompute) and is judged by the real gate (``assess_resolution_readiness``,
``fix_windows``, M6). Numbered as the round-57 plan's tests.
"""

from functools import partial

import pytest

from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    FIX_FAILED,
    FIX_SUCCEEDED,
    FIX_UNKNOWN,
    SUBJECT_CAUSE,
    SUBJECT_PROBLEM,
    SUBJECT_UNBOUND,
    _cause_pool,
    cause_elimination_rows,
    confirm_root_from_resolution_absence,
    confirming_polarity_rows,
    fix_windows,
    has_resolution_confirmation,
    observation_subject,
    observed_turn,
    subject_index,
    symptom_elimination_rows,
)
from faultmaven.core.investigation.milestone_engine import response_application
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.problem_status import invalidation_refusal
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    fix_confirmed,
)
from faultmaven.core.investigation.prompts.context_builder.evidence_needs import (
    _build_evidence_needs_block,
)
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    KnowledgeResolution,
    MilestoneUpdates,
    ProblemVerificationUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    AnswerVia,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    InvestigationStage,
    MitigationRecord,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemStatus,
    ValidationMethod,
)
from faultmaven.modules.case.contracts import (
    KnowledgeResolution as KnowledgeResolutionRecord,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, upload
from tests.unit.core.investigation.fix_turns_2039 import _TSU, _engine_rows
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _accept,
    _add_sibling_chain,
    _eid,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _apply,
    _case,
    _engine,
    _Response,
)

pytestmark = pytest.mark.unit

_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_CAUSE_GONE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE
_CAUSAL = EvidenceCategory.CAUSAL_EVIDENCE
_SUPPORT = _eid("pool_log")  # the identified root's own support
_ASK = "The check that showed the problem"
_RECHECK_ASK = "The cause re-checked after the fix"


class _KRUpdate(_TSU):
    """A turn carrying a knowledge resolution: the investigating apply step
    reads it when present."""

    knowledge_resolution: KnowledgeResolution | None = None


def _obs(category, summary, rechecks=None, file_id="") -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=summary,
        extract=summary,
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        rechecks=rechecks,
    )


async def _record(eng, case, turn, *rows, update=_TSU, response=None, **fields):
    """One turn through the real apply, the rows' data uploaded on that turn
    unless a row names its file. Returns (the rows created, in order; meta)."""
    case.current_turn = turn
    fresh = upload(case, turn)
    rows = [
        r if r.source_file_id else r.model_copy(update={"source_file_id": fresh})
        for r in rows
    ]
    meta = await _apply(eng, case, update(evidence_to_add=rows, **fields), response)
    by_id = {e.evidence_id: e for e in case.evidence}
    return [by_id.get(slot) for slot in meta["evidence_slots"]], meta


def _fixed(turn=6):
    """The #1927 identified case, its fix executed at ``turn``."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, turn)
    return _engine(), case, root, hyp


def _root(node_id, statement, *supports, node_type=NodeType.ROOT, terminal=True):
    return CausalNode(
        metadata={"root_terminal": True} if terminal else {},
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        belief=0.9,
        actionable=node_type == NodeType.ROOT,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=s,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on it",
                linked_at_turn=2,
            )
            for s in supports
        ],
        generated_at_turn=1,
    )


def _support(case, label, turn=2) -> str:
    row = Evidence(
        evidence_id=_eid(label),
        summary=label,
        primary_purpose="diagnosis",
        category=_CAUSAL,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=upload(case, turn, label),
        collected_by="llm",
        collected_at_turn=turn,
    )
    case.evidence.append(row)
    return row.evidence_id


# ---------------------------------------------------------------------------
# 1. The resolver
# ---------------------------------------------------------------------------


async def test_1_each_subject_resolves_by_the_row_it_names():
    eng, case, _root_node, _hyp = _fixed()
    (gone, cause, unnamed, unnamed_cause), _ = await _record(
        eng,
        case,
        7,
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout fine now"),
        _obs(_CAUSE_GONE, "the setting is fixed"),
    )
    assert observation_subject(case, gone) == SUBJECT_PROBLEM
    assert observation_subject(case, cause) == SUBJECT_CAUSE
    assert observation_subject(case, unnamed) == SUBJECT_UNBOUND
    assert observation_subject(case, unnamed_cause) == SUBJECT_UNBOUND
    assert "fix_verification" not in (unnamed.metadata or {})


async def test_1_a_forward_new_index_ref_resolves_once_every_row_has_its_slot():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    (gone, shown), _ = await _record(
        eng,
        case,
        5,
        _obs(_GONE, "the 401s stop after the token refresh", "new_index_1"),
        _obs(_PRESENT, "a third of logins fail with 401"),
        milestones=MilestoneUpdates(
            symptom_verified=True, symptom_verification_evidence_ids=["new_index_1"]
        ),
    )
    assert gone.rechecks_evidence_id == shown.evidence_id
    assert case.problem_verification.verifying_evidence_ids == [shown.evidence_id]
    assert observation_subject(case, gone) == SUBJECT_PROBLEM


async def test_1_an_absence_row_a_refused_slot_or_no_row_is_stored_unbound():
    eng, case, _root_node, _hyp = _fixed()
    (first,), _ = await _record(eng, case, 7, _obs(_GONE, "checkout 200", VERIFYING_ID))
    rows, _ = await _record(
        eng,
        case,
        8,
        _obs(_GONE, "still fine", first.evidence_id),
        _obs(_GONE, "fine again", "new_index_2"),
        _obs(_PRESENT, "from a file the case never had", file_id="file_000000000bad"),
        _obs(_CAUSE_GONE, "fixed", "ev_000000000bad"),
    )
    names_absence, names_refused, refused, names_nothing = rows
    assert refused is None  # the refused slot keeps its place
    for row, named in (
        (names_absence, first.evidence_id),
        (names_refused, "new_index_2"),
        (names_nothing, "ev_000000000bad"),
    ):
        assert row.metadata["fix_verification"] == {"rechecks": None, "named": named}
        assert observation_subject(case, row) == SUBJECT_UNBOUND


async def test_1_a_row_naming_itself_is_stored_unbound():
    """Refused when the turn records it (the one place: the read does not
    repeat it), even for a row the same turn cites as showing the problem."""
    eng, case, _root_node, _hyp = _fixed()
    case.problem_verification.verifying_evidence_ids = []
    (row,), _ = await _record(
        eng,
        case,
        7,
        _obs(_PRESENT, "checkout returned 503 (the original)", "new_index_0"),
        milestones=MilestoneUpdates(symptom_verification_evidence_ids=["new_index_0"]),
    )
    assert case.problem_verification.verifying_evidence_ids == [row.evidence_id]
    assert row.metadata["fix_verification"] == {
        "rechecks": None,
        "named": "new_index_0",
    }
    assert observation_subject(case, row) == SUBJECT_UNBOUND


async def test_1_a_subject_its_category_does_not_observe_is_unbound():
    eng, case, _root_node, _hyp = _fixed()
    rows, _ = await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "checkout 200 again", VERIFYING_ID),
        _obs(_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_CAUSAL, "the pool limit is the cause", _SUPPORT),
    )
    assert [observation_subject(case, r) for r in rows] == [SUBJECT_UNBOUND] * 3


def _gateway_case():
    """The stored link shape of case_ff3ed2d2df16 (#2036, c1021-1): a chain of
    three roots (only the deepest marked terminal) above two rungs, one row
    supporting the rung "certificate expired" AND the three roots, and one
    supporting the two upper roots alone."""
    case, _root_node, hyp = _identified_case()
    case.causal_nodes.clear()
    rung_and_roots = _support(case, "certificate expired; CertificateRequest blocked")
    roots_only = _support(case, "AppRole secret_id expired; nothing rotates it")
    problem = CausalNode(
        statement="api-gateway returns 503",
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )
    deepest = _root(
        "cn_00000000df7c",
        "nothing rotates the AppRole secret_id",
        rung_and_roots,
        roots_only,
    )
    secret = _root(
        "cn_00000000cbad",
        "the AppRole secret_id is expired",
        rung_and_roots,
        roots_only,
        terminal=False,
    )
    issuer = _root(
        "cn_000000000c1b", "the issuer is not Ready", rung_and_roots, terminal=False
    )
    expired = _root(
        "cn_000000003329",
        "the client certificate expired",
        rung_and_roots,
        node_type=NodeType.INTERMEDIATE,
        terminal=False,
    )
    for node in (problem, deepest, secret, issuer, expired):
        case.causal_nodes[node.node_id] = node
    case.causal_edges = [
        CausalEdge(cause_node_id=a.node_id, effect_node_id=b.node_id)
        for a, b in (
            (deepest, secret),
            (secret, issuer),
            (issuer, expired),
            (expired, problem),
        )
    ]
    hyp.root_node_id = deepest.node_id
    hyp.state = HypothesisState.VALIDATED
    return case, rung_and_roots, roots_only


async def test_1_roots_of_one_cluster_bind_a_rung_link_binds_none():
    case, rung_and_roots, roots_only = _gateway_case()
    rows, _ = await _record(
        _engine(),
        case,
        7,
        _obs(_CAUSE_GONE, "issuer on Kubernetes auth", roots_only),
        _obs(_CAUSE_GONE, "certificate renewed", rung_and_roots),
    )
    assert [observation_subject(case, r) for r in rows] == [
        SUBJECT_CAUSE,
        SUBJECT_UNBOUND,
    ]


async def test_1_roots_of_two_clusters_bind_none():
    case, root, _hyp = _identified_case()
    other = _root("cn_00000000e7e7", "the gunicorn worker leaks memory")
    case.causal_nodes[other.node_id] = other
    other.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_SUPPORT,
            stance=EvidenceStance.SUPPORTS,
            reasoning="r",
            linked_at_turn=2,
        )
    )
    (row,), _ = await _record(
        _engine(), case, 7, _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT)
    )
    assert observation_subject(case, row) == SUBJECT_UNBOUND


async def test_1_a_count_held_root_binds_and_with_no_cause_nothing_does():
    case, root, _hyp = _identified_case()
    root.evidence_links = root.evidence_links[:1]  # one support: count-held
    eng = _engine()
    (held,), _ = await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100", _SUPPORT))
    assert root.node_state == NodeState.INCONCLUSIVE
    assert observation_subject(case, held) == SUBJECT_CAUSE

    case.causal_nodes.pop(_ROOT_ID)
    case.progress.identified_cause_root_ids = []
    (orphan,), _ = await _record(eng, case, 8, _obs(_CAUSE_GONE, "reads 100", _SUPPORT))
    assert observation_subject(case, orphan) == SUBJECT_UNBOUND


# ---------------------------------------------------------------------------
# 2. #2036 (c1021-1): a renewal re-checking a rung-linked row is no cause leg
# ---------------------------------------------------------------------------


async def test_2_certificate_renewed_naming_the_rung_row_gives_no_cause_leg():
    case, rung_and_roots, roots_only = _gateway_case()
    eng = _engine()
    await _record(
        eng,
        case,
        7,
        _obs(
            _CAUSE_GONE,
            "api-gateway presents a renewed client certificate",
            rung_and_roots,
        ),
        _obs(_GONE, "503 rate 0.0/s", VERIFYING_ID),
    )
    assert cause_elimination_rows(case) == []
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY

    await _record(
        eng, case, 8, _obs(_CAUSE_GONE, "issuer on Kubernetes auth", roots_only)
    )
    assert len(cause_elimination_rows(case)) == 1


# ---------------------------------------------------------------------------
# 3. The #2039 residual: a probe's own failure is not "the problem is back"
# ---------------------------------------------------------------------------


async def test_3_a_probe_failure_naming_no_verifying_row_is_not_the_problem_back():
    eng, case, root, hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200, 503 rate 0.0/s", VERIFYING_ID),
    )
    await _record(
        eng,
        case,
        13,
        _obs(_PRESENT, "curl to checkout returned 503: no session cookie"),
    )
    assert fix_windows(case)[-1].state == FIX_SUCCEEDED
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert hyp.state != HypothesisState.REFUTED and _engine_rows(case) == []


# ---------------------------------------------------------------------------
# 4. #2145: a row is dated by its data's upload turn
# ---------------------------------------------------------------------------


async def test_4_a_row_from_data_uploaded_before_the_fix_does_not_confirm_it():
    eng, case, _root_node, _hyp = _fixed(9)
    early = upload(case, 7, "turn-7 relief")
    await _record(
        eng,
        case,
        10,
        _obs(_CAUSE_GONE, "issuer on Kubernetes auth", _SUPPORT),
        _obs(_GONE, "503 rate 0.0/s after the restart", VERIFYING_ID, file_id=early),
    )
    (carried,) = [e for e in case.evidence if e.source_file_id == early]
    assert (carried.collected_at_turn, observed_turn(case, carried)) == (10, 7)
    assert symptom_elimination_rows(case) == []
    assert not has_resolution_confirmation(case)

    await _record(eng, case, 10, _obs(_GONE, "503 rate 0.0/s now", VERIFYING_ID))
    assert has_resolution_confirmation(case)


def test_4_a_row_with_no_file_on_the_case_is_dated_by_its_turn():
    case, _root_node, _hyp = _identified_case()
    row = Evidence(
        summary="engine inference",
        primary_purpose="m6",
        category=_CAUSE_GONE,
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        collected_by="engine",
        collected_at_turn=9,
    )
    assert observed_turn(case, row) == 9
    row = row.model_copy(
        update={
            "source_file_id": "file_00000000gone",
            "source_type": EvidenceSourceType.LOGS,
        }
    )
    assert observed_turn(case, row) == 9


# ---------------------------------------------------------------------------
# 5. The rows that showed the problem
# ---------------------------------------------------------------------------


def _claim(*cited) -> MilestoneUpdates:
    return MilestoneUpdates(
        symptom_verified=True, symptom_verification_evidence_ids=list(cited)
    )


async def test_5_the_claim_records_its_cited_symptom_rows_only():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    (shown, cause), _ = await _record(
        eng,
        case,
        5,
        _obs(_PRESENT, "a third of logins fail with 401"),
        _obs(_CAUSAL, "the signing key rotated at 09:00"),
        milestones=_claim("new_index_0", "new_index_1"),
    )
    assert case.problem_verification.verifying_evidence_ids == [shown.evidence_id]


@pytest.mark.parametrize("rows, recorded", [(1, True), (2, False)])
async def test_5_with_no_citation_only_a_single_symptom_row_is_recorded(rows, recorded):
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    created, _ = await _record(
        eng,
        case,
        5,
        *[_obs(_PRESENT, f"logins fail with 401, host {i}") for i in range(rows)],
        milestones=_claim(),
    )
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    expected = [created[0].evidence_id] if recorded else []
    assert case.problem_verification.verifying_evidence_ids == expected


async def test_5_a_revision_commit_records_what_the_revision_cited():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    (shown,), _ = await _record(
        eng,
        case,
        5,
        _obs(_PRESENT, "only the batch pods' logins fail"),
        verification_updates=ProblemVerificationUpdate(
            revised_problem_statement="Only the batch pods' logins fail with 401",
            revision_evidence_ids=["new_index_0"],
            revision_basis="the API pods log in fine",
        ),
    )
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
    await confirm_revision(eng.responses, case, {}, via=AnswerVia.CLICK)
    assert case.problem_verification.verifying_evidence_ids == [shown.evidence_id]


async def test_5_any_unverify_clears_them():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    await _record(
        eng, case, 5, _obs(_PRESENT, "logins fail with 401"), milestones=_claim()
    )
    assert case.problem_verification.verifying_evidence_ids
    await _record(
        eng,
        case,
        6,
        milestones=MilestoneUpdates(symptom_verified=False),
        response=_Response("the 401s came from the load-test harness"),
    )
    assert case.progress.problem_status == ProblemStatus.UNVERIFIED
    assert case.problem_verification.verifying_evidence_ids == []


def _kr() -> KnowledgeResolution:
    return KnowledgeResolution(
        match_id="rb-oidc",
        match_type="runbook",
        solution_applied="fix",
        user_confirmation="it worked",
    )


@pytest.fixture
def _kr_record(monkeypatch):
    """The investigating apply step builds the case's resolution record
    without the ``resolution_turn`` the model requires, so the branch raises
    before it verifies anything; and no investigating schema carries
    ``knowledge_resolution``. Both reported, not fixed here: the record is
    built with the turn, so the branch's verifying-row write can be reached."""
    monkeypatch.setattr(
        response_application,
        "KnowledgeResolution",
        partial(KnowledgeResolutionRecord, resolution_turn=5),
    )


async def test_5_a_knowledge_resolution_records_rows_only_through_a_claim(_kr_record):
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    await _record(
        eng,
        case,
        5,
        _obs(_CAUSAL, "the runbook's indicator matched: audience mismatch"),
        update=_KRUpdate,
        knowledge_resolution=_kr(),
    )
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert case.problem_verification.verifying_evidence_ids == []
    assert _ASK in assess_resolution_readiness(case).message

    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    (shown,), _ = await _record(
        eng,
        case,
        5,
        _obs(_PRESENT, "logins fail with 401"),
        update=_KRUpdate,
        knowledge_resolution=_kr(),
        milestones=_claim("new_index_0"),
    )
    assert case.problem_verification.verifying_evidence_ids == [shown.evidence_id]


async def test_5_a_later_citation_fills_an_empty_list_never_a_full_one():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    a, b = (_obs(_PRESENT, f"logins fail with 401 on {h}") for h in ("api", "batch"))
    (first, second), _ = await _record(eng, case, 5, a, b, milestones=_claim())
    assert case.problem_verification.verifying_evidence_ids == []
    # No fallback after the verifying turn: a lone symptom row cited nowhere.
    await _record(eng, case, 6, _obs(_PRESENT, "still 401 on the batch pods"))
    assert case.problem_verification.verifying_evidence_ids == []
    cite = MilestoneUpdates(symptom_verification_evidence_ids=[second.evidence_id])
    await _record(eng, case, 7, milestones=cite)
    assert case.problem_verification.verifying_evidence_ids == [second.evidence_id]
    cite = MilestoneUpdates(symptom_verification_evidence_ids=[first.evidence_id])
    await _record(eng, case, 8, milestones=cite)
    assert case.problem_verification.verifying_evidence_ids == [second.evidence_id]


async def test_5_the_failing_output_re_pasted_and_cited_is_not_the_problem_back():
    eng, case, root, hyp = _fixed(6)
    case.problem_verification.verifying_evidence_ids = []
    await _record(
        eng,
        case,
        7,
        _obs(_PRESENT, "checkout returned 503 on every request (the original)"),
        milestones=MilestoneUpdates(symptom_verification_evidence_ids=["new_index_0"]),
    )
    (shown,) = [e for e in case.evidence if e.collected_at_turn == 7]
    assert case.problem_verification.verifying_evidence_ids == [shown.evidence_id]
    assert fix_windows(case)[-1].state != FIX_FAILED
    assert hyp.state != HypothesisState.REFUTED


# ---------------------------------------------------------------------------
# 6. Enforcement: a row naming nothing counts for nothing
# ---------------------------------------------------------------------------


async def test_6_only_the_named_bound_row_counts():
    eng, case, _root_node, _hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100"),
        _obs(_GONE, "checkout 200 again"),
    )
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY
    (bound_cause, bound_gone), _ = await _record(
        eng,
        case,
        8,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert cause_elimination_rows(case) == [bound_cause]
    assert symptom_elimination_rows(case) == [bound_gone]
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# 7. The re-verification checklist lists only the rows a re-check binds to
# ---------------------------------------------------------------------------


def test_7_the_checklist_narrows_to_the_rows_that_would_bind():
    case, _root_node, _hyp = _identified_case()
    case.progress.solution_accepted = True  # TREATMENT
    other = _support(case, "a row about nothing in the graph")
    block = _build_evidence_needs_block(case)
    assert f"[{VERIFYING_ID}]" in block and f"[{_SUPPORT}]" in block
    assert other not in block

    case.problem_verification.verifying_evidence_ids = []
    stray = Evidence(
        evidence_id=_eid("stray"),
        summary="a second symptom row",
        primary_purpose="diagnosis",
        category=_PRESENT,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=upload(case, 3, "stray"),
        collected_by="llm",
        collected_at_turn=3,
    )
    case.evidence.append(stray)
    block = _build_evidence_needs_block(case)
    assert f"[{VERIFYING_ID}]" in block and f"[{stray.evidence_id}]" in block


# ---------------------------------------------------------------------------
# 8. Feedback for an unbound post-fix row, named or not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rechecks", [None, "ev_000000000bad"], ids=["unnamed", "named"]
)
async def test_8_an_unbound_row_gets_one_line_listing_what_would_bind(rechecks):
    eng, case, _root_node, _hyp = _fixed(6)
    (row,), meta = await _record(eng, case, 7, _obs(_GONE, "checkout fine", rechecks))
    feedback = meta["system_feedback"]
    assert f"Post-fix row(s) {row.evidence_id}" in feedback
    assert VERIFYING_ID in feedback and _SUPPORT in feedback

    _, meta = await _record(eng, case, 8, _obs(_GONE, "checkout fine", VERIFYING_ID))
    assert "Post-fix row" not in (meta.get("system_feedback") or "")


# ---------------------------------------------------------------------------
# 10. Readiness names what is missing (ruled 2026-11-03)
# ---------------------------------------------------------------------------


def _no_symptom_rows(case) -> None:
    case.evidence = [e for e in case.evidence if e.category != _PRESENT]
    case.problem_verification.verifying_evidence_ids = []


async def test_10_with_no_symptom_row_on_record_the_ask_shows_until_one_is_cited():
    eng, case, _root_node, _hyp = _fixed(6)
    _no_symptom_rows(case)
    await _record(
        eng, case, 7, _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT)
    )
    message = assess_resolution_readiness(case).message
    assert (
        "- **The check that showed the problem** — the original failing output "
        "isn't on record as what showed the problem; submit it as data (pasted "
        "or attached) so the fix can be confirmed against it."
    ) in message

    await _record(
        eng,
        case,
        8,
        _obs(_PRESENT, "checkout returned 503 on every request (the original)"),
        milestones=MilestoneUpdates(symptom_verification_evidence_ids=["new_index_0"]),
    )
    assert case.problem_verification.verifying_evidence_ids
    assert _ASK not in assess_resolution_readiness(case).message


async def test_10_with_a_symptom_row_on_record_the_ask_is_absent():
    """Ruled 2026-11-03 (#2481 review, R3): with the row that showed the
    problem on record but not cited, the model cites it; the user is not asked
    to paste it again."""
    eng, case, _root_node, _hyp = _fixed(6)
    case.problem_verification.verifying_evidence_ids = []
    await _record(
        eng, case, 7, _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT)
    )
    assert _ASK not in assess_resolution_readiness(case).message


# ---------------------------------------------------------------------------
# 11. The ruled asymmetry: an unnamed "it's back" row disconfirms nothing
# ---------------------------------------------------------------------------


async def test_11_a_bound_relief_then_an_unnamed_return_keeps_ready_and_m6_quiet():
    eng, case, root, hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    await _record(eng, case, 9, _obs(_PRESENT, "checkout returns 503 again"))
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert fix_windows(case)[-1].state == FIX_SUCCEEDED
    assert hyp.state != HypothesisState.REFUTED and _engine_rows(case) == []


# ---------------------------------------------------------------------------
# The two cause pools (#2481 review, R1 + R2)
# ---------------------------------------------------------------------------


async def _a_fell_then_b(supports: int, terminal: bool = True):
    """A identified, its fix (8) seen failing at 9 — the cause re-checked gone,
    the problem still there — so M6 demotes A. Then B, another candidate,
    gets its own supports: two (validated, identified), one (count-held) or
    none; ``terminal=False`` leaves B with no why-stopping verdict."""
    eng, case, root_a, hyp_a = _fixed(8)
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert hyp_a.state == HypothesisState.REFUTED and _engine_rows(case)
    b = _add_sibling_chain(case)
    if not terminal:
        b.metadata.pop("root_terminal", None)
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=b.node_id, effect_node_id=problem.node_id)
    )
    labels = ["worker RSS grows 40MB/h", "OOM killer log for gunicorn workers"]
    rows = [_support(case, label, 10) for label in labels[:supports]]
    for evidence_id in rows:
        b.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on B",
                linked_at_turn=10,
            )
        )
    await _record(eng, case, 10)
    return eng, case, root_a, b, (rows[0] if rows else None)


async def _fix_b_and_recheck(eng, case, support):
    _accept(case, InvestigationActionType.SOLUTION, 12)
    rows, meta = await _record(
        eng,
        case,
        13,
        _obs(_CAUSE_GONE, "the re-check of the cause", support),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    return rows[0], meta


async def test_p1_a_refuted_causes_recheck_never_confirms_another_cause():
    eng, case, root_a, b, _b_support = await _a_fell_then_b(supports=2)
    assert b.is_identified_root
    _problem, cause = subject_index(case).bindable_rows()
    assert _SUPPORT not in [e.evidence_id for e in cause]
    a_recheck, meta = await _fix_b_and_recheck(eng, case, _SUPPORT)
    assert observation_subject(case, a_recheck) == SUBJECT_UNBOUND
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY
    # The window still sees it, so the model is not told it names nothing.
    assert "Post-fix row(s)" not in (meta.get("system_feedback") or "")


async def test_p2_a_count_held_cause_binds_after_another_cause_fell():
    eng, case, root_a, b, b_support = await _a_fell_then_b(supports=1)
    assert b.node_state == NodeState.INCONCLUSIVE  # count-held, not identified
    b_recheck, _meta = await _fix_b_and_recheck(eng, case, b_support)
    assert observation_subject(case, b_recheck) == SUBJECT_CAUSE
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


async def test_a_fallen_causes_removal_still_proves_the_problem_existed():
    """The false-alarm refusal reads the window pool: the cause A's failed fix
    showed it removed, so the problem existed, whatever cause stands now."""
    eng, case, _root_a, _b, _b_support = await _a_fell_then_b(supports=1)
    (quiet,), _ = await _record(eng, case, 11, _obs(_GONE, "no 503 in the window"))
    assert invalidation_refusal(case, [quiet.evidence_id], "nothing failed") == (
        "a cause was confirmed eliminated, so the problem existed"
    )


@pytest.mark.parametrize(
    "beside",
    [None, "ruled-out", "rootless"],
    ids=["alone", "beside-a-ruled-out-one", "beside-a-rootless-one"],
)
async def test_the_only_cause_refuted_then_re_fixed_still_resolves(beside):
    """#1927's shape: the sole cause fell to its failed fix; re-fixed and
    re-checked, it is the case's cause again (the pool's third leg). Only a
    standing hypothesis ROOTED elsewhere takes the leg away (F2): a candidate
    already ruled out does not stand, and a rootless one names no root."""
    eng, case, root, hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert hyp.state == HypothesisState.REFUTED
    if beside == "ruled-out":
        _add_sibling_chain(case)
        case.hypotheses["hyp_00000000eeee"].state = HypothesisState.REFUTED
    elif beside == "rootless":
        case.hypotheses["hyp_00000000f1a7"] = Hypothesis(
            hypothesis_id="hyp_00000000f1a7",
            statement="a DNS flap",
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="a flat guess",
            initial_likelihood=0.2,
            generated_at_turn=7,
        )
    _accept(case, InvestigationActionType.SOLUTION, 10)
    await _record(
        eng,
        case,
        11,
        _obs(_CAUSE_GONE, "max_connections reads 200 and pgbouncer pools", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


async def test_a_failed_fix_of_a_fallen_cause_is_not_the_identified_causes():
    """The window pool sees the fallen cause A's re-check after B's fix, and
    the window FAILS; but the fix it failed was not a fix of B, so M6 leaves
    B standing (``failed_fix_of``'s attribution)."""
    eng, case, root_a, b, _b_support = await _a_fell_then_b(supports=2)
    hyp_b = case.hypotheses["hyp_00000000eeee"]
    _accept(case, InvestigationActionType.SOLUTION, 12)
    await _record(
        eng,
        case,
        13,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert fix_windows(case)[-1].state == FIX_FAILED
    assert hyp_b.state != HypothesisState.REFUTED and b.is_identified_root


async def test_the_stamp_cites_only_a_row_about_the_cause_it_confirms():
    """A stays identified with its hypothesis retired, B is the standing cause:
    a row re-checking A's support is in the cause leg, but the stamp confirming
    B does not cite it."""
    case, root_a, hyp_a = _identified_case()
    b = _add_sibling_chain(case)
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=b.node_id, effect_node_id=problem.node_id)
    )
    for label in ("worker RSS grows 40MB/h", "OOM killer log for gunicorn workers"):
        b.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=_support(case, label, 3),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on B",
                linked_at_turn=3,
            )
        )
    eng = _engine()
    await _record(eng, case, 5)
    hyp_a.state = HypothesisState.RETIRED
    assert root_a.is_identified_root and b.is_identified_root
    await _record(
        eng,
        case,
        6,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert cause_elimination_rows(case)
    assert confirm_root_from_resolution_absence(case) is False


# ---------------------------------------------------------------------------
# Readings the windows, legs and asks take from the data's turn (#2145)
# ---------------------------------------------------------------------------


async def test_the_cause_leg_needs_data_from_after_the_cause_was_posited():
    case, root, _hyp = _identified_case()
    root.metadata["root_attached_turn"] = 7
    eng = _engine()
    early = upload(case, 5, "turn-5 reading")
    await _record(eng, case, 8, _obs(_CAUSE_GONE, "reads 100", _SUPPORT, file_id=early))
    assert cause_elimination_rows(case) == []
    await _record(eng, case, 8, _obs(_CAUSE_GONE, "reads 100 now", _SUPPORT))
    assert len(cause_elimination_rows(case)) == 1


async def test_a_persistence_after_the_causes_data_fails_the_window():
    """The cause row is recorded after the persistence, from data uploaded
    before it: by the data, the cause went first and the problem stayed."""
    eng, case, _root_node, _hyp = _fixed(6)
    await _record(eng, case, 8, _obs(_PRESENT, "checkout still 503", VERIFYING_ID))
    early = upload(case, 7, "turn-7 config")
    await _record(eng, case, 9, _obs(_CAUSE_GONE, "reads 100", _SUPPORT, file_id=early))
    assert fix_windows(case)[-1].state == FIX_FAILED


async def test_an_unnamed_relief_is_no_relief():
    eng, case, _root_node, _hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "reads 100", _SUPPORT),
        _obs(_GONE, "checkout fine now"),
    )
    assert fix_windows(case)[-1].state == FIX_UNKNOWN


async def test_the_cause_recheck_ask_reads_a_bound_relief_from_after_the_fix():
    eng, case, _root_node, _hyp = _fixed(9)
    await _record(eng, case, 10, _obs(_GONE, "checkout fine now"))
    assert _RECHECK_ASK not in assess_resolution_readiness(case).message
    early = upload(case, 7, "turn-7 relief")
    await _record(
        eng, case, 10, _obs(_GONE, "503 rate 0.0/s", VERIFYING_ID, file_id=early)
    )
    assert _RECHECK_ASK not in assess_resolution_readiness(case).message
    await _record(eng, case, 11, _obs(_GONE, "checkout 200 again", VERIFYING_ID))
    assert _RECHECK_ASK in assess_resolution_readiness(case).message


def test_the_stamp_never_cites_data_from_before_a_refutation_of_its_root():
    """The per-root refutation window reads the data's turn: a confirmation
    recorded after a (hedged) refutation of the root, but read from data
    uploaded before it, does not mint the top grade."""
    case, root, _hyp = _identified_case()
    refute = Evidence(
        evidence_id=_eid("hedged refute"),
        summary="not sure the fix helped",
        primary_purpose="post-fix check",
        category=_CAUSE_GONE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=upload(case, 8, "turn-8 refute"),
        collected_by="llm",
        collected_at_turn=8,
    )
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=refute.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="uncertain failed fix",
            stance_confidence=0.4,
            linked_at_turn=8,
        )
    )
    early = Evidence(
        evidence_id=_eid("early confirm"),
        summary="reads 100",
        primary_purpose="post-fix check",
        category=_CAUSE_GONE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=upload(case, 7, "turn-7 confirm"),
        collected_by="llm",
        collected_at_turn=9,
    )
    early.record_rechecks(_SUPPORT, named=_SUPPORT)
    case.evidence += [refute, early]
    assert early in cause_elimination_rows(case)
    assert confirm_root_from_resolution_absence(case) is False


async def test_a_count_held_root_with_no_terminal_verdict_is_no_cause():
    case, root, _hyp = _identified_case()
    root.evidence_links = root.evidence_links[:1]  # count-held
    root.record_root_terminal(False, "a job rewrites the limit", 4)
    case.progress.identified_cause_root_ids = []
    (row,), _ = await _record(
        _engine(), case, 7, _obs(_CAUSE_GONE, "reads 100", _SUPPORT)
    )
    assert root.node_state == NodeState.INCONCLUSIVE
    assert observation_subject(case, row) == SUBJECT_UNBOUND


# ---------------------------------------------------------------------------
# The unbound-row feedback reads only rows after a recorded fix (R4)
# ---------------------------------------------------------------------------


def _post_fix_line(meta) -> bool:
    return "Post-fix row(s)" in (meta.get("system_feedback") or "")


async def test_no_line_for_an_exclusion_before_any_fix():
    case, _root_node, _hyp = _identified_case()
    _, meta = await _record(
        _engine(), case, 6, _obs(_CAUSE_GONE, "DNS resolves fine: ruled out")
    )
    assert not _post_fix_line(meta)


async def test_no_line_for_data_from_before_the_recorded_fix():
    eng, case, _root_node, _hyp = _fixed(9)
    early = upload(case, 7, "turn-7 relief")
    _, meta = await _record(eng, case, 10, _obs(_GONE, "503 rate 0.0/s", file_id=early))
    assert not _post_fix_line(meta)
    _, meta = await _record(
        eng, case, 9, _obs(_PRESENT, "checkout 503 (the fix's turn)")
    )
    assert not _post_fix_line(meta)


async def test_no_line_for_the_failing_output_cited_as_having_shown_the_problem():
    eng, case, _root_node, _hyp = _fixed(6)
    case.problem_verification.verifying_evidence_ids = []
    _, meta = await _record(
        eng,
        case,
        7,
        _obs(_PRESENT, "checkout returned 503 (the original)"),
        milestones=MilestoneUpdates(symptom_verification_evidence_ids=["new_index_0"]),
    )
    assert not _post_fix_line(meta)


async def test_no_line_inside_a_failed_window():
    eng, case, _root_node, _hyp = _fixed(6)
    await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100", _SUPPORT))
    await _record(eng, case, 8, _obs(_PRESENT, "checkout still 503", VERIFYING_ID))
    assert fix_windows(case)[-1].state == FIX_FAILED
    _, meta = await _record(eng, case, 10, _obs(_GONE, "checkout fine now"))
    assert "did not hold" in meta["system_feedback"]
    assert not _post_fix_line(meta)


# ---------------------------------------------------------------------------
# The verifying rows render in their recorded order (R8)
# ---------------------------------------------------------------------------


def test_the_verifying_rows_render_in_the_order_recorded():
    case, _root_node, _hyp = _identified_case()
    case.progress.solution_accepted = True  # TREATMENT
    later = [_support(case, f"symptom {i}", 6 - i) for i in range(5)]
    for evidence_id in later:
        row = next(e for e in case.evidence if e.evidence_id == evidence_id)
        row.category = _PRESENT
    case.problem_verification.verifying_evidence_ids = list(later)
    block = _build_evidence_needs_block(case)
    assert sorted(later, key=block.index) == later
    problem, _cause = subject_index(case).bindable_rows()
    assert [e.evidence_id for e in problem] == later


async def test_the_window_pool_keeps_a_cause_that_lost_its_verdict_this_turn():
    """#2042 beside a count-held candidate B. The identified cause A loses its
    terminal verdict in the very turn its fix is seen failing, so B, not A,
    is the confirmation pool. The window pool keeps A, the cause the last
    recompute identified, so its failed fix still demotes it."""
    eng, case, root_a, hyp_a = _fixed(8)
    b = _add_sibling_chain(case)
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=b.node_id, effect_node_id=problem.node_id)
    )
    b.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_support(case, "worker RSS grows 40MB/h", 7),
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on B",
            linked_at_turn=7,
        )
    )
    await _record(eng, case, 8)
    assert root_a.is_identified_root and b.node_state == NodeState.INCONCLUSIVE
    root_a.record_root_terminal(False, "something upstream resets the limit", 9)
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert hyp_a.state == HypothesisState.REFUTED and _engine_rows(case)


async def test_an_out_of_band_fix_resolves_in_window_0_on_named_rows():
    """No fix recorded here (window 0): the checks the user submits after an
    out-of-band fix, each naming what it re-checks, confirm the resolution."""
    case, _root_node, _hyp = _identified_case()
    await _record(
        _engine(),
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert [w.fix_turn for w in fix_windows(case)] == [None]
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# The delta round: F1, F2, F3, F5
# ---------------------------------------------------------------------------


async def test_f1_a_fallen_causes_recheck_never_makes_anothers_fix_succeed():
    """A fell, B is identified and fixed at 12. At 13: the problem gone, and
    a re-check of A's old support. The re-check is in the window pool (it can
    still fail B's window) but not the confirmation pool: B's fix is not
    confirmed, and the user is still asked for B's cause re-check."""
    eng, case, _root_a, b, _b_support = await _a_fell_then_b(supports=2)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    await _record(
        eng,
        case,
        13,
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
        _obs(_CAUSE_GONE, "max_connections still reads 100", _SUPPORT),
    )
    window = fix_windows(case)[-1]
    assert window.cause_rows and not window.confirmed_cause_rows
    assert window.state != FIX_SUCCEEDED and not fix_confirmed(case)
    assert _RECHECK_ASK in assess_resolution_readiness(case).message


@pytest.mark.parametrize(
    "supports, terminal",
    [(1, False), (0, True), (2, False)],
    ids=["non-terminal-b", "bare-b", "validated-non-terminal-b"],
)
async def test_f2_a_fallen_cause_is_not_confirmed_past_a_newer_candidate(
    supports, terminal
):
    """A fell; B stands as a candidate that is neither identified nor
    count-held terminal. The fallen-cause leg does not serve confirmation
    while B stands: A's re-check confirms nothing and A's supports are not
    offered. The window pool still holds A (the pool's last leg too), so the
    window sees the re-check and the model is not told it names nothing."""
    eng, case, _root_a, b, _b_support = await _a_fell_then_b(supports, terminal)
    assert not b.is_identified_root
    _problem, cause = subject_index(case).bindable_rows()
    assert _SUPPORT not in [e.evidence_id for e in cause]
    a_recheck, meta = await _fix_b_and_recheck(eng, case, _SUPPORT)
    assert observation_subject(case, a_recheck) == SUBJECT_UNBOUND
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY
    assert a_recheck in fix_windows(case)[-1].cause_rows
    assert "Post-fix row(s)" not in (meta.get("system_feedback") or "")


async def test_f2_the_fallen_causes_own_hypothesis_is_not_another_candidate():
    """Only ANOTHER standing hypothesis takes the fallen leg away. In the turn
    A loses its verdict, before the recompute re-records the identified roots,
    A is fallen (recorded) while its own hypothesis still stands: A still
    serves confirmation, and its support is still offered for a re-check."""
    eng, case, root_a, hyp_a = _fixed(8)
    await _record(eng, case, 8)
    root_a.record_root_terminal(False, "something upstream resets the limit", 9)
    assert not root_a.is_identified_root and hyp_a.state == HypothesisState.VALIDATED
    assert _cause_pool(case)[0] == {root_a.node_id}
    _problem, cause = subject_index(case).bindable_rows()
    assert _SUPPORT in [e.evidence_id for e in cause]


def _polarity_ids(case) -> list:
    return [e.evidence_id for e in confirming_polarity_rows(case)]


async def test_f3_the_report_cites_rows_of_the_latest_window_holding_one():
    eng, case, _root_node, _hyp = _fixed(6)
    (first,), _ = await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100"))
    _accept(case, InvestigationActionType.SOLUTION, 9)
    assert _polarity_ids(case) == [first.evidence_id]  # the new window holds none
    (second,), _ = await _record(eng, case, 10, _obs(_CAUSE_GONE, "reads 200"))
    assert _polarity_ids(case) == [second.evidence_id]


@pytest.mark.parametrize("same_turn", [False, True], ids=["next-turn", "same-turn"])
async def test_f3_the_report_cites_nothing_from_a_failed_window(same_turn):
    """Seen failing a turn later, M6's disconfirmation also floors the row;
    seen failing in the row's own turn, only the FAILED-window bar keeps the
    row out (the floor is ``>=``)."""
    eng, case, _root_node, _hyp = _fixed(6)
    gone = _obs(_CAUSE_GONE, "reads 100", _SUPPORT)
    present = _obs(_PRESENT, "checkout still 503", VERIFYING_ID)
    if same_turn:
        await _record(eng, case, 7, gone, present)
    else:
        (row,), _ = await _record(eng, case, 7, gone)
        assert _polarity_ids(case) == [row.evidence_id]
        await _record(eng, case, 8, present)
    assert fix_windows(case)[-1].state == FIX_FAILED
    assert _polarity_ids(case) == []


async def test_f3_the_report_cites_nothing_from_before_the_cause_was_posited():
    case, root, _hyp = _identified_case()
    root.metadata["root_attached_turn"] = 7
    early = upload(case, 5, "turn-5 reading")
    eng = _engine()
    await _record(eng, case, 8, _obs(_CAUSE_GONE, "reads 100", file_id=early))
    assert _polarity_ids(case) == []
    (fresh,), _ = await _record(eng, case, 8, _obs(_CAUSE_GONE, "reads 100 now"))
    assert _polarity_ids(case) == [fresh.evidence_id]


async def test_f3_the_report_cites_no_failed_fix_disconfirmation():
    """M6 demoted A at 7 (its engine row and REFUTES link). After a new fix,
    a row the model REFUTES-linked to A is a disconfirmation, never cited; a
    plain re-check is."""
    eng, case, root, _hyp = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still 503", VERIFYING_ID),
    )
    assert _engine_rows(case)
    _accept(case, InvestigationActionType.SOLUTION, 9)
    (refuting, plain), _ = await _record(
        eng,
        case,
        10,
        _obs(_CAUSE_GONE, "the fix did not remove it"),
        _obs(_CAUSE_GONE, "pgbouncer pools connections"),
    )
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=refuting.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="failed treatment",
            linked_at_turn=10,
        )
    )
    assert _polarity_ids(case) == [plain.evidence_id]


async def test_f3_the_report_cites_nothing_older_than_a_disconfirmation():
    """No fix recorded (window 0); the engine recorded a failed-fix
    disconfirmation at 5. A row read from data older than it is premature."""
    case, _root_node, _hyp = _identified_case()
    case.evidence.append(
        Evidence(
            summary="Engine inference (M6): the fix did not hold",
            primary_purpose="engine inference",
            category=_CAUSE_GONE,
            source_type=EvidenceSourceType.ENGINE_INFERENCE,
            collected_by=ENGINE_EVIDENCE_AUTHOR,
            collected_at_turn=5,
        )
    )
    early = upload(case, 4, "turn-4 reading")
    eng = _engine()
    await _record(eng, case, 6, _obs(_CAUSE_GONE, "reads 100", file_id=early))
    assert _polarity_ids(case) == []
    (fresh,), _ = await _record(eng, case, 6, _obs(_CAUSE_GONE, "reads 100 now"))
    assert _polarity_ids(case) == [fresh.evidence_id]


async def test_f3_the_report_cites_nothing_while_identification_is_held_open():
    eng, case, _root_node, _hyp = _fixed(6)
    (gone,), _ = await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100"))
    assert _polarity_ids(case) == [gone.evidence_id]
    case.progress.cause_identification_held_open = True
    assert _polarity_ids(case) == []


async def test_f3_a_newer_window_holding_only_a_disconfirmation_cites_nothing():
    """The report's window is the latest one holding any causal_absence row,
    as main's confirming window was, not the latest holding a qualifying one:
    a newer fix's window whose only row is a failed-fix disconfirmation
    cites nothing, even though an older window holds a qualifying row."""
    case, root, _hyp = _identified_case()
    engine_row = Evidence(
        summary="Engine inference (M6): the fix did not hold",
        primary_purpose="engine inference",
        category=_CAUSE_GONE,
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        collected_by=ENGINE_EVIDENCE_AUTHOR,
        collected_at_turn=5,
    )
    case.evidence.append(engine_row)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=engine_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="failed treatment",
            linked_at_turn=5,
        )
    )
    eng = _engine()
    (older,), _ = await _record(eng, case, 6, _obs(_CAUSE_GONE, "reads 100"))
    assert _polarity_ids(case) == [older.evidence_id]
    _accept(case, InvestigationActionType.SOLUTION, 8)
    (refuting,), _ = await _record(
        eng, case, 9, _obs(_CAUSE_GONE, "the fix did not remove it")
    )
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=refuting.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="failed treatment",
            linked_at_turn=9,
        )
    )
    assert _polarity_ids(case) == []


async def test_f3_the_report_window_is_chosen_by_the_datas_turn():
    """A row recorded after a new fix, read from data uploaded before it,
    belongs to the older window: that window stays the report's."""
    eng, case, _root_node, _hyp = _fixed(6)
    (first,), _ = await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100"))
    early = case.uploaded_files[-1].file_id
    _accept(case, InvestigationActionType.SOLUTION, 9)
    (late,), _ = await _record(
        eng, case, 10, _obs(_CAUSE_GONE, "reads 100 (the same file)", file_id=early)
    )
    assert observed_turn(case, late) == 7
    assert _polarity_ids(case) == [first.evidence_id, late.evidence_id]


def _confirming_section(case) -> str:
    text = "\n".join(
        ReportGenerationService()._fix_and_confirming_evidence(case, ended="resolved")
    )
    return text[text.find("## Confirming Evidence") :]


@pytest.mark.parametrize("shape", ["second-fix", "stamp-lag"])
async def test_the_report_renders_the_row_the_resolution_was_stamped_on(shape):
    """Exception round, finding 1. The gate confirms on an older window while a
    newer one holds only an unnamed causal_absence row: a second fix after
    the confirmation, or the execution stamp lagging the user's report. The
    report cites the cause leg's row, the one the stamp stamped, not the
    unnamed one."""
    if shape == "second-fix":
        eng, case, _root_node, _hyp = _fixed(6)
        turn = 7
    else:
        case, _root_node, _hyp = _identified_case()
        eng, turn = _engine(), 6
    (stamped, _gone), _ = await _record(
        eng,
        case,
        turn,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    _accept(case, InvestigationActionType.SOLUTION, turn + 1)
    await _record(eng, case, turn + 2, _obs(_CAUSE_GONE, "the old cron entry is gone"))
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert [e.evidence_id for e in cause_elimination_rows(case)] == [
        stamped.evidence_id
    ]
    assert confirm_root_from_resolution_absence(case)
    section = _confirming_section(case)
    assert "max_connections reads 100" in section
    assert "the old cron entry is gone" not in section


async def test_a_newer_fix_observed_only_through_a_fallen_cause_still_answers():
    """A fell; B is identified, fixed at 11 and re-checked at 12. A second fix
    at 14, then a re-check of A's old support and the problem gone. Window 14
    was looked at (the re-check is in the window pool), so window 11's
    confirmation no longer answers, and window 14 holds no confirmation-bound
    cause row: nothing confirms."""
    eng, case, _root_a, b, b_support = await _a_fell_then_b(supports=2)
    assert b.is_identified_root
    _accept(case, InvestigationActionType.SOLUTION, 11)
    await _record(
        eng, case, 12, _obs(_CAUSE_GONE, "RSS flat after the patch", b_support)
    )
    _accept(case, InvestigationActionType.SOLUTION, 14)
    await _record(
        eng,
        case,
        15,
        _obs(_CAUSE_GONE, "max_connections still reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    latest = fix_windows(case)[-1]
    assert latest.fix_turn == 14 and latest.observed
    assert not latest.confirmed_cause_rows and not cause_elimination_rows(case)
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY


@pytest.mark.parametrize("fix_recorded", [True, False], ids=["fix", "no-fix"])
def test_f5_the_checklist_asks_for_rechecks_only_after_a_recorded_fix(fix_recorded):
    """Keyed on the fix record, as the unbound-row feedback is, not on the
    stage label: MITIGATION outranks TREATMENT while a workaround is pending,
    so a fix executed meanwhile still asks for ``rechecks``."""
    case, _root_node, _hyp = _identified_case()
    case.progress.mitigation = MitigationRecord(
        proposed_at_turn=case.current_turn, accepted=True
    )
    if fix_recorded:
        _accept(case, InvestigationActionType.SOLUTION, case.current_turn)
        case.progress.solution_accepted = True
    assert case.current_stage == InvestigationStage.MITIGATION
    block = _build_evidence_needs_block(case)
    assert "Re-verification checklist" in block
    assert ("rechecks" in block) is fix_recorded
