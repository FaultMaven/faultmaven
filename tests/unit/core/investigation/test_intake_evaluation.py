"""Process-owned intake-evaluation orchestration (``run_intake_evaluation``).

The matcher-owned pieces (the evaluator + the root resolver) are injected here so
this exercises only the process orchestration: verdict → node-evidence link with
provenance, lazy promotion on a SUPPORTS, no instantiation on a REFUTES, dedup,
and skipping verdicts for unknown candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.differential_intake import StanceVerdict
from faultmaven.core.investigation.intake_evaluation import (
    run_differential_intake_turn,
    run_intake_evaluation,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NeedPurpose,
    NeedState,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

ROOT_ID = "cn_000000000001"


def _evidence(eid: str = "ev_000000000001") -> Evidence:
    return Evidence(
        evidence_id=eid,
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(UTC),
    )


def _root(node_id: str = ROOT_ID) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement="the root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=1,
    )


def _case(node: CausalNode | None = None) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="x",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="x", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 5
    if node is not None:
        case.causal_nodes = {node.node_id: node}
    return case


def _ac(candidate_id: str = "rb1:A"):
    # Stand-in for the matcher's ActiveCause (candidate_id + record). The record
    # is opaque here — only the injected resolver consumes it.
    return SimpleNamespace(candidate_id=candidate_id, record=object())


def _verdict(
    *,
    cause_id: str = "rb1:A",
    stance: EvidenceStance = EvidenceStance.SUPPORTS,
    provenance: str = "runbook",
) -> StanceVerdict:
    return StanceVerdict(
        cause_id=cause_id,
        stance=stance,
        provenance=provenance,  # type: ignore[arg-type]
        predicate={"predicate": "contains", "target": "NotFound"},
    )


def _always(root_id):
    """Resolver that always returns root_id (cause already instantiated)."""
    return lambda case, record, *, may_instantiate: root_id


def _promote_only(root_id):
    """Resolver that returns root_id only when allowed to instantiate (the lazy
    promotion path); None otherwise (un-promoted cause)."""
    return lambda case, record, *, may_instantiate: root_id if may_instantiate else None


def test_supports_verdict_promotes_and_links_with_provenance():
    node = _root()
    case = _case(node)
    ev = _evidence()
    recorded = run_intake_evaluation(
        case,
        [ev],
        [_ac()],
        case.current_turn,
        resolve_root=_promote_only(node.node_id),
        evaluate=lambda **_: [_verdict(provenance="runbook")],
    )
    assert len(node.evidence_links) == 1
    link = node.evidence_links[0]
    assert link.evidence_id == ev.evidence_id
    assert link.stance == EvidenceStance.SUPPORTS
    assert link.provenance == "runbook"  # carried from the verdict
    assert recorded and recorded[0].cause_id == "rb1:A"


def test_refutes_on_unpromoted_cause_is_skipped():
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_promote_only(node.node_id),  # returns None when not instantiating
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.REFUTES)],
    )
    assert node.evidence_links == []
    assert recorded == []


def test_supports_with_no_instantiable_root_is_skipped():
    """A degenerate / [Default] cause has no instantiable root, so resolve_root
    returns None even on a SUPPORTS (matcher's instantiate_cause_chain yields no
    root node). The verdict must be skipped — there is nothing to attach to —
    not error. (Pins the guarantee the matcher flagged: None-on-SUPPORTS.)"""
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=lambda c, r, *, may_instantiate: None,  # no instantiable root
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.SUPPORTS)],
    )
    assert node.evidence_links == []
    assert recorded == []


def test_refutes_on_already_instantiated_cause_links():
    node = _root()
    case = _case(node)
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),  # cause already in the graph
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.REFUTES)],
    )
    assert len(node.evidence_links) == 1
    assert node.evidence_links[0].stance == EvidenceStance.REFUTES


def test_duplicate_verdict_links_once():
    node = _root()
    case = _case(node)
    v = _verdict()
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [v, v],  # same datum fires the same predicate twice
    )
    assert len(node.evidence_links) == 1


def test_verdict_for_unknown_candidate_is_skipped():
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac(candidate_id="rb1:A")],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(cause_id="rb9:Z")],  # not in active_causes
    )
    assert node.evidence_links == []
    assert recorded == []


def test_fallback_provenance_is_carried_to_the_link():
    node = _root()
    case = _case(node)
    run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(provenance="llm_fallback")],
    )
    assert node.evidence_links[0].provenance == "llm_fallback"


async def _causes_for(_runbook_id):
    # Stand-in for knowledge_service.get_runbook_causes — a cheap DB read returning
    # raw v4 cause dicts.
    return [{"cause_letter": "A"}]


def _build(_rid, _causes_raw):
    # Stand-in for AnswerFromKB._build_cause_records (per-runbook, tolerant).
    # assemble_active_causes only reads .cause_letter / .is_fallback_cause.
    return [SimpleNamespace(cause_letter="A", is_fallback_cause=False)]


@pytest.mark.asyncio
async def test_differential_turn_inert_without_runbook_ids():
    # The matcher hasn't fired yet (no differential source) → no-op.
    node = _root()
    case = _case(node)
    recorded = await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=[],
        resolve_causes=_causes_for,
        build_records=_build,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict()],
    )
    assert recorded == []
    assert node.evidence_links == []


@pytest.mark.asyncio
async def test_differential_turn_inert_without_new_evidence():
    node = _root()
    case = _case(node)
    recorded = await run_differential_intake_turn(
        case,
        [],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict()],
    )
    assert recorded == []


@pytest.mark.asyncio
async def test_differential_turn_validates_resolved_candidates():
    # rb1's cause A is re-resolved, becomes an ActiveCause, and this turn's datum
    # fires a SUPPORTS verdict → a provenance-carrying link is attached.
    node = _root()
    case = _case(node)
    recorded = await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(cause_id="rb1:A", provenance="runbook")],
    )
    assert len(node.evidence_links) == 1
    assert node.evidence_links[0].provenance == "runbook"
    assert recorded and recorded[0].cause_id == "rb1:A"


@pytest.mark.asyncio
async def test_differential_turn_skips_runbook_with_no_causes():
    node = _root()
    case = _case(node)

    async def _no_causes(_rid):
        return None  # pre-v4 / unknown runbook

    recorded = await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb_missing"],
        resolve_causes=_no_causes,
        build_records=_build,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict()],
    )
    assert recorded == []


@pytest.mark.asyncio
async def test_differential_turn_skips_malformed_record_without_raising():
    node = _root()
    case = _case(node)

    def _bad_build(_rid, _causes_raw):
        raise ValueError("malformed cause records")

    recorded = await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_bad_build,  # parsing the runbook's causes fails
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict()],
    )
    assert recorded == []  # nothing parsed → no differential → no-op, not a crash


def _build_with_predicate(_rid, _causes_raw):
    return [
        SimpleNamespace(
            cause_letter="A",
            is_fallback_cause=False,
            match_predicates=[{"predicate": "contains", "target": "OOMKilled"}],
        )
    ]


@pytest.mark.asyncio
async def test_unsatisfied_predicate_creates_pending_causal_need():
    # The demand half: an unsatisfied differential predicate becomes a PENDING
    # causal Evidence Need describing what telemetry to collect.
    node = _root()
    case = _case(node)
    await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],  # predicate does not fire
    )
    assert len(case.evidence_needs) == 1
    need = case.evidence_needs[0]
    assert need.purpose == NeedPurpose.CAUSAL_VERIFICATION
    assert need.state == NeedState.PENDING
    assert need.motivating_hypothesis_ids == []  # safe from supersession rule
    assert "OOMKilled" in need.request_text


@pytest.mark.asyncio
async def test_need_regen_is_idempotent_across_turns():
    node = _root()
    case = _case(node)
    for _ in range(3):
        await run_differential_intake_turn(
            case,
            [_evidence()],
            case.current_turn,
            runbook_ids=["rb1"],
            resolve_causes=_causes_for,
            build_records=_build_with_predicate,
            resolve_root=_always(node.node_id),
            evaluate=lambda **_: [],
        )
    assert len(case.evidence_needs) == 1  # deterministic id → never duplicated


@pytest.mark.asyncio
async def test_firing_predicate_fulfills_its_need():
    node = _root()
    case = _case(node)
    # Turn 1: predicate unsatisfied → PENDING need.
    await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    assert case.evidence_needs[0].state == NeedState.PENDING

    # Turn 2: the same predicate fires → its need flips to FULFILLED (not a new one).
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000002")],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [
            StanceVerdict(
                cause_id="rb1:A",
                stance=EvidenceStance.SUPPORTS,
                provenance="runbook",
                predicate={"predicate": "contains", "target": "OOMKilled"},
            )
        ],
    )
    assert len(case.evidence_needs) == 1
    assert case.evidence_needs[0].state == NeedState.FULFILLED


@pytest.mark.asyncio
async def test_refuted_cause_need_is_superseded_not_left_pending():
    # The demand↔validate consistency fix: once a cause's root is REFUTED, its open
    # need is superseded — the refuted-root guard means its predicate can never
    # fulfill the need, so it must not keep asking for telemetry for a ruled-out cause.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    # Turn 1: cause still in play → a PENDING need is created.
    await run_differential_intake_turn(case, [_evidence()], case.current_turn, **common)
    assert case.evidence_needs[0].state == NeedState.PENDING

    # The cause's root is refuted; next turn the open need is superseded, not hung.
    node.node_state = NodeState.REFUTED
    node.refutation_reason = "refuted by rung evidence"
    await run_differential_intake_turn(
        case, [_evidence("ev_000000000002")], case.current_turn, **common
    )
    assert len(case.evidence_needs) == 1
    assert case.evidence_needs[0].state == NeedState.SUPERSEDED
    assert case.evidence_needs[0].superseded_reason


@pytest.mark.asyncio
async def test_fulfilled_need_stays_fulfilled_after_refutation():
    # Audit trail (the soundness claim): a need FULFILLED *before* the cause was
    # refuted must NOT be rewritten to SUPERSEDED — it records what was collected
    # before the cause was ruled out. Supersession only retires PENDING/PARTIALLY_MET.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
    )
    # Turn 1: predicate unsatisfied → a PENDING need is demanded.
    await run_differential_intake_turn(
        case, [_evidence()], case.current_turn, evaluate=lambda **_: [], **common
    )
    assert case.evidence_needs[0].state == NeedState.PENDING

    # Turn 2: the predicate fires → the need is FULFILLED.
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000002")],
        case.current_turn,
        evaluate=lambda **_: [
            StanceVerdict(
                cause_id="rb1:A",
                stance=EvidenceStance.SUPPORTS,
                provenance="runbook",
                predicate={"predicate": "contains", "target": "OOMKilled"},
            )
        ],
        **common,
    )
    assert case.evidence_needs[0].state == NeedState.FULFILLED

    # Turn 3: the cause's root is refuted — the FULFILLED need is PRESERVED.
    node.node_state = NodeState.REFUTED
    node.refutation_reason = "refuted by rung evidence"
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000003")],
        case.current_turn,
        evaluate=lambda **_: [],
        **common,
    )
    assert len(case.evidence_needs) == 1
    assert case.evidence_needs[0].state == NeedState.FULFILLED  # not SUPERSEDED


@pytest.mark.asyncio
async def test_uninstantiated_root_keeps_pending_need():
    # An untested cause (no instantiated root) is not settled: _cause_root_settled_state
    # resolves with may_instantiate=False, gets None, and returns None (neither refuted
    # nor validated) — so the cause's PENDING need is preserved, never superseded.
    case = _case()  # no root node in the graph
    await run_differential_intake_turn(
        case,
        [_evidence()],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_promote_only("cn_root"),  # None when may_instantiate=False
        evaluate=lambda **_: [],
    )
    assert len(case.evidence_needs) == 1
    assert case.evidence_needs[0].state == NeedState.PENDING


@pytest.mark.asyncio
async def test_partially_met_need_is_superseded_on_refutation():
    # The supersede branch also retires a PARTIALLY_MET need (lower-priority path,
    # completing the state matrix alongside PENDING→SUPERSEDED and FULFILLED-preserved).
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    # Turn 1: a PENDING need is created; drive it to PARTIALLY_MET out of band.
    await run_differential_intake_turn(case, [_evidence()], case.current_turn, **common)
    case.evidence_needs[0].state = NeedState.PARTIALLY_MET

    # Refute the root → the PARTIALLY_MET need is superseded.
    node.node_state = NodeState.REFUTED
    node.refutation_reason = "refuted by rung evidence"
    await run_differential_intake_turn(
        case, [_evidence("ev_000000000002")], case.current_turn, **common
    )
    assert case.evidence_needs[0].state == NeedState.SUPERSEDED


@pytest.mark.asyncio
async def test_validated_cause_unfired_need_is_superseded():
    # A3 (the symmetric twin of the refuted case): once a cause's root is VALIDATED,
    # its still-open predicate needs are retired — the cause is proven, so asking the
    # user to discriminate it further would hang PENDING forever.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    # Turn 1: a PENDING need is created.
    await run_differential_intake_turn(case, [_evidence()], case.current_turn, **common)
    assert case.evidence_needs[0].state == NeedState.PENDING

    # The root validates DEDUCTIVELY (durable) on other evidence without this
    # predicate firing → its un-fired need is superseded.
    node.node_state = NodeState.VALIDATED
    node.validation_method = ValidationMethod.DEDUCTIVE
    await run_differential_intake_turn(
        case, [_evidence("ev_000000000002")], case.current_turn, **common
    )
    assert case.evidence_needs[0].state == NeedState.SUPERSEDED
    assert case.evidence_needs[0].superseded_reason


@pytest.mark.asyncio
async def test_validated_cause_with_firing_predicate_fulfills_not_supersedes():
    # If the predicate DOES fire on the turn the cause is validated, the need is
    # FULFILLED — the fired branch runs before the validated supersession, so a genuine
    # hit is recorded (audit trail), not retired.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
    )
    # Turn 1: PENDING need.
    await run_differential_intake_turn(
        case, [_evidence()], case.current_turn, evaluate=lambda **_: [], **common
    )
    assert case.evidence_needs[0].state == NeedState.PENDING

    # Turn 2: predicate fires AND the root is deductively validated → FULFILLED wins.
    node.node_state = NodeState.VALIDATED
    node.validation_method = ValidationMethod.DEDUCTIVE
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000002")],
        case.current_turn,
        evaluate=lambda **_: [
            StanceVerdict(
                cause_id="rb1:A",
                stance=EvidenceStance.SUPPORTS,
                provenance="runbook",
                predicate={"predicate": "contains", "target": "OOMKilled"},
            )
        ],
        **common,
    )
    assert case.evidence_needs[0].state == NeedState.FULFILLED


@pytest.mark.asyncio
async def test_empirically_validated_cause_keeps_pending_need():
    # The correctness fix: an EMPIRICAL validation is NOT durable — derive_node_states
    # can demote it to INCONCLUSIVE/CANDIDATE on later evidence — so its need must STAY
    # OPEN (superseding it would strand the cause if it un-validates). Only DEDUCTIVE
    # validation retires the need.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    await run_differential_intake_turn(case, [_evidence()], case.current_turn, **common)
    assert case.evidence_needs[0].state == NeedState.PENDING

    # Empirically validated (the common, revertible kind) → need is NOT superseded.
    node.node_state = NodeState.VALIDATED
    node.validation_method = ValidationMethod.EMPIRICAL
    await run_differential_intake_turn(
        case, [_evidence("ev_000000000002")], case.current_turn, **common
    )
    assert case.evidence_needs[0].state == NeedState.PENDING


@pytest.mark.asyncio
async def test_fulfilled_need_stays_fulfilled_after_validation():
    # Audit trail (twin of the refuted-side test): a need FULFILLED before the cause
    # was deductively validated stays FULFILLED, not rewritten to SUPERSEDED.
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
    )
    await run_differential_intake_turn(
        case, [_evidence()], case.current_turn, evaluate=lambda **_: [], **common
    )
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000002")],
        case.current_turn,
        evaluate=lambda **_: [
            StanceVerdict(
                cause_id="rb1:A",
                stance=EvidenceStance.SUPPORTS,
                provenance="runbook",
                predicate={"predicate": "contains", "target": "OOMKilled"},
            )
        ],
        **common,
    )
    assert case.evidence_needs[0].state == NeedState.FULFILLED

    node.node_state = NodeState.VALIDATED
    node.validation_method = ValidationMethod.DEDUCTIVE
    await run_differential_intake_turn(
        case,
        [_evidence("ev_000000000003")],
        case.current_turn,
        evaluate=lambda **_: [],
        **common,
    )
    assert case.evidence_needs[0].state == NeedState.FULFILLED  # not SUPERSEDED


@pytest.mark.asyncio
async def test_partially_met_need_is_superseded_on_deductive_validation():
    # Completes the validated-side state matrix (twin of the refuted PARTIALLY_MET test).
    node = _root()
    case = _case(node)
    common = dict(
        runbook_ids=["rb1"],
        resolve_causes=_causes_for,
        build_records=_build_with_predicate,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [],
    )
    await run_differential_intake_turn(case, [_evidence()], case.current_turn, **common)
    case.evidence_needs[0].state = NeedState.PARTIALLY_MET

    node.node_state = NodeState.VALIDATED
    node.validation_method = ValidationMethod.DEDUCTIVE
    await run_differential_intake_turn(
        case, [_evidence("ev_000000000002")], case.current_turn, **common
    )
    assert case.evidence_needs[0].state == NeedState.SUPERSEDED


def test_supports_is_not_re_attached_to_a_refuted_root():
    # A REFUTED root is settled — a runbook predicate firing SUPPORTS on a later
    # turn must NOT re-support it (the standing-bias harm). The link is skipped.
    node = _root()
    node.node_state = NodeState.REFUTED
    node.refutation_reason = "refuted by rung evidence"
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
        evaluate=lambda **_: [_verdict(stance=EvidenceStance.SUPPORTS)],
    )
    assert node.evidence_links == []
    assert recorded == []


def test_refuted_root_does_not_block_a_live_sibling():
    # The guard is per-root: a refuted cause in a runbook must not stop the runbook's
    # OTHER (still-live) causes from being supported — that is why we skip the
    # refuted root rather than retiring the whole runbook.
    refuted = _root("cn_000000000001")
    refuted.node_state = NodeState.REFUTED
    refuted.refutation_reason = "refuted by rung evidence"
    live = _root("cn_000000000002")
    case = _case()
    case.causal_nodes = {refuted.node_id: refuted, live.node_id: live}
    active = [
        SimpleNamespace(candidate_id="rb1:A", record="A"),
        SimpleNamespace(candidate_id="rb1:B", record="B"),
    ]
    roots = {"A": refuted.node_id, "B": live.node_id}
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        active,
        case.current_turn,
        resolve_root=lambda c, record, *, may_instantiate: roots[record],
        evaluate=lambda **_: [
            _verdict(cause_id="rb1:A", stance=EvidenceStance.SUPPORTS),
            _verdict(cause_id="rb1:B", stance=EvidenceStance.SUPPORTS),
        ],
    )
    assert refuted.evidence_links == []  # refuted root skipped
    assert len(live.evidence_links) == 1  # live sibling still supported
    assert [v.cause_id for v in recorded] == ["rb1:B"]


def test_default_evaluator_stub_is_inert():
    # With the real (stubbed) evaluator, no verdicts → no links (loop inert until
    # the matcher body lands).
    node = _root()
    case = _case(node)
    recorded = run_intake_evaluation(
        case,
        [_evidence()],
        [_ac()],
        case.current_turn,
        resolve_root=_always(node.node_id),
    )
    assert recorded == []
    assert node.evidence_links == []
