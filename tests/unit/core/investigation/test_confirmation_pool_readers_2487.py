"""The confirmation pool's two leftovers of #1906 2a.

#2487: the report's Confirming Evidence re-derived the cause leg after the
confirm-stamp ran, and the stamp's count-held re-derive can narrow the pool, so
the row the stamp had just recorded was dropped. The report now also reads the
stamp's record (``confirmation_stamp_rows``).

#2485: the fallen-cause leg stood aside only for another standing hypothesis
rooted elsewhere, so a fallen cause confirmed past a root no hypothesis names.
Such an orphan now counts when it is not fallen, not REFUTED, on no
hypothesis's path or root, and not one cause with any fallen root, pairwise.

Rows enter through the real turn application and the real gate judges them, as
in ``test_fix_verification_subjects_1906`` (#1927's re-fix is pinned there).
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_SUCCEEDED,
    SUBJECT_UNBOUND,
    _cause_pool,
    cause_elimination_rows,
    confirmation_stamp_rows,
    fix_windows,
    observation_subject,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    fix_confirmed,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    execute_user_closure,
    finalize_resolution_truth_surface,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    CausalEdge,
    CausalNode,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.case_builders import with_declined_offer
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _confirming_section,
    _fix_b_and_recheck,
    _fixed,
    _obs,
    _record,
    _support,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _add_sibling_chain,
    _identified_case,
    _row,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

_LEAK = ["worker RSS grows 40MB/h", "OOM killer log for gunicorn workers"]
_C_B = "pg_stat_activity: 12 of 100 slots used"


def _links(case, *labels, stance=EvidenceStance.SUPPORTS) -> list:
    """A causal row per label, recorded at turn 10, each linked by ``stance``."""
    return [
        NodeEvidenceLink(
            evidence_id=_support(case, label, 10),
            stance=stance,
            reasoning="bears on it",
            linked_at_turn=10,
        )
        for label in labels
    ]


def _root(case, node_id, statement, links=(), terminal=False, to=None) -> CausalNode:
    """A ROOT node, CANDIDATE, edged into ``to`` when given. Without
    ``terminal`` it has no why-stopping verdict, as a root no hypothesis
    names has none."""
    node = CausalNode(
        metadata={"root_terminal": True} if terminal else {},
        node_id=node_id,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.3,
        actionable=True,
        evidence_links=list(links),
        generated_at_turn=1,
    )
    case.causal_nodes[node_id] = node
    if to is not None:
        _edge(case, node_id, to)
    return node


def _edge(case, cause_id, effect_id) -> None:
    case.causal_edges.append(
        CausalEdge(cause_node_id=cause_id, effect_node_id=effect_id)
    )


def _problem_id(case) -> str:
    return next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )


async def _fail(eng, case, turn, support=_SUPPORT) -> None:
    """The cause ``support`` backs re-checked gone, the problem still there:
    M6 demotes it."""
    await _record(
        eng,
        case,
        turn,
        _obs(_CAUSE_GONE, "the cause re-checked gone", support),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )


async def _refixed(eng, case, fix, support=_SUPPORT) -> None:
    _accept(case, InvestigationActionType.SOLUTION, fix)
    await _record(
        eng,
        case,
        fix + 1,
        _obs(_CAUSE_GONE, "the cause re-checked gone again", support),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )


# ---------------------------------------------------------------------------
# #2487: the report reads the stamp's record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("same_line", [True, False], ids=["same-line", "duplicate"])
async def test_the_report_renders_the_row_the_stamp_recorded(same_line):
    """R1 (the hypothesis's root) and R2 (on its line, or a duplicate of it),
    both count-held terminal on one support each. Fixed at 6; c_a re-checks
    R1's support at 7, c_b R2's at 8 beside a bound relief. The stamp records
    c_b on R1, whose validation narrows the pool to R1."""
    case, r1, _hyp = _identified_case()
    r2_links, r1.evidence_links = r1.evidence_links[1:], r1.evidence_links[:1]
    statement = "every checkout worker blocks waiting on a postgres slot"
    r2 = _root(
        case,
        "cn_00000000d00b",
        statement if same_line else r1.statement,
        r2_links,
        terminal=True,
        to=_problem_id(case),
    )
    if same_line:
        _edge(case, r1.node_id, r2.node_id)
    eng, r1_support = _engine(), r1.evidence_links[0].evidence_id
    await _record(eng, case, 5)
    _accept(case, InvestigationActionType.SOLUTION, 6)
    (c_a,), _ = await _record(eng, case, 7, _obs(_CAUSE_GONE, "reads 100", r1_support))
    (c_b, _gone), _ = await _record(
        eng,
        case,
        8,
        _obs(_CAUSE_GONE, _C_B, r2_links[0].evidence_id),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert cause_elimination_rows(case) == [c_a, c_b], "premise"

    assert finalize_resolution_truth_surface(case)
    assert confirmation_stamp_rows(case) == [c_b]
    assert (r1.node_state, r2.node_state) == (
        NodeState.VALIDATED,
        NodeState.INCONCLUSIVE,
    )
    assert cause_elimination_rows(case) == [c_a], "the issue's narrowing"
    assert c_b.evidence_id in case.root_cause_conclusion.evidence_basis
    assert _C_B in _confirming_section(case)


def test_the_stamp_record_is_a_user_absence_row_supporting_a_root():
    """Not an engine row, not a rung's link, not a REFUTES link, not a row
    linked nowhere, and not the root's own causal supports."""
    case, root, _hyp = _identified_case()
    rung = _root(case, "cn_00000000a11e", "the checkout pool waits on a slot")
    rung.node_type = NodeType.INTERMEDIATE
    rows = {
        label: _row(label, _CAUSE_GONE, 6, label, collected_by=author)
        for label, author in [
            ("stamped", "llm"),
            ("engine", "engine"),
            ("rung", "llm"),
            ("refutes", "llm"),
            ("unlinked", "llm"),
        ]
    }
    case.evidence += rows.values()
    for node, label, stance in [
        (root, "stamped", EvidenceStance.SUPPORTS),
        (root, "engine", EvidenceStance.SUPPORTS),
        (rung, "rung", EvidenceStance.SUPPORTS),
        (root, "refutes", EvidenceStance.REFUTES),
    ]:
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=rows[label].evidence_id,
                stance=stance,
                reasoning="r",
                linked_at_turn=6,
            )
        )
    assert confirmation_stamp_rows(case) == [rows["stamped"]]


async def test_a_report_with_no_stamp_is_unchanged():
    """No stamp on record: the resolution summary, and the summary of a close
    the user chose over the resolution (#2360), are the cause leg's alone."""
    case = _confirmed_case()
    assert confirmation_stamp_rows(case) == []
    absence = [e for e in case.evidence if e.category == _CAUSE_GONE]
    assert absence and cause_elimination_rows(case) == absence, "premise"
    resolved = case.model_copy(update={"state": CaseState.RESOLVED})
    declined = case.model_copy(deep=True)
    with_declined_offer(declined, kind="resolve", basis="backstop")
    execute_user_closure(declined, "user_test")
    assert declined.closure_reason == "resolution_declined", "premise"
    svc = object.__new__(ReportGenerationService)

    async def summaries():
        return (
            await svc._generate_resolution_summary(resolved, {}),
            await svc._generate_closure_summary(declined, {}),
        )

    rendered = await summaries()
    with patch(
        "faultmaven.core.investigation.cause_assurance.confirmation_stamp_rows",
        return_value=[],
    ):
        assert await summaries() == rendered
    for summary in rendered:
        assert absence[0].summary in summary.split("## Confirming Evidence")[1]


# ---------------------------------------------------------------------------
# #2485: an orphan candidate takes the fallen-cause leg away
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("supports", [0, 1, 2])
async def test_a_fallen_cause_is_not_confirmed_past_an_orphan_candidate(supports):
    """A fell; B, a root no hypothesis names, is fixed at 12; at 13 A's support
    is re-checked beside a bound relief. On main B's window SUCCEEDED and the
    case read READY through A. Now nothing confirms, and the asks are today's
    for a case with no cause (the stated residual)."""
    eng, case, root_a, hyp_a = _fixed(8)
    await _fail(eng, case, 9)
    assert hyp_a.state == HypothesisState.REFUTED
    b_links, statement = _links(case, *_LEAK[:supports]), "a leak in the workers"
    _root(case, "cn_00000000eeee", statement, b_links, to=_problem_id(case))
    await _record(eng, case, 10)

    a_recheck, _meta = await _fix_b_and_recheck(eng, case, _SUPPORT)

    assert _cause_pool(case)[:2] == (frozenset(), {root_a.node_id})
    assert observation_subject(case, a_recheck) == SUBJECT_UNBOUND
    assert fix_windows(case)[-1].state != FIX_SUCCEEDED and not fix_confirmed(case)
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict != ResolutionReadiness.READY
    assert "- **Root cause** — what caused the problem?" in readiness.message


@pytest.mark.parametrize(
    "shape, resolves",
    [
        ("distinct", False),
        ("above", False),
        ("below", True),
        ("duplicate", True),
        ("refuted", True),
    ],
)
async def test_an_orphan_beside_the_only_fallen_cause(shape, resolves):
    """#1927's sole cause A fell, then re-fixed and re-checked. An orphan
    distinct from A blocks it, and so does one above A (D→A): the cluster walk
    does not expand the REFUTED A, so D is another cause, as a standing
    hypothesis rooted at D is. One below A, a duplicate of A, or a REFUTED
    one is no other candidate."""
    eng, case, root, _hyp = _fixed(6)
    await _fail(eng, case, 7)
    statement = {
        "duplicate": root.statement,
        "above": "a deploy script resets max_connections to its default of 20",
    }.get(shape, "a DNS flap between the app and the database")
    stance = EvidenceStance.REFUTES if shape == "refuted" else EvidenceStance.SUPPORTS
    orphan = _root(
        case,
        "cn_00000000ab1e",
        statement,
        _links(case, "dig log", stance=stance),
        to=root.node_id if shape == "above" else None,
    )
    if shape == "below":
        _edge(case, root.node_id, orphan.node_id)
    await _refixed(eng, case, 10)
    assert (orphan.node_state == NodeState.REFUTED) is (shape == "refuted")
    verdict = assess_resolution_readiness(case).verdict
    assert (verdict == ResolutionReadiness.READY) is resolves


async def test_two_fallen_causes_and_a_duplicate_orphan_of_the_second():
    """A fell at 9; B, identified, fell to its own fix at 13; D, a root no
    hypothesis names, duplicates B. Pairwise, D is one cause with B, so it is
    no other candidate and B's re-fix resolves."""
    eng, case, root_a, hyp_a = _fixed(8)
    await _fail(eng, case, 9)
    b = _add_sibling_chain(case)
    _edge(case, b.node_id, _problem_id(case))
    b.evidence_links += _links(case, *_LEAK)
    await _record(eng, case, 10)
    assert b.is_identified_root, "premise"
    b_support = b.evidence_links[0].evidence_id
    _accept(case, InvestigationActionType.SOLUTION, 12)
    await _fail(eng, case, 13, b_support)
    hyp_b = case.hypotheses["hyp_00000000eeee"]
    assert hyp_a.state == hyp_b.state == HypothesisState.REFUTED, "premise"
    _root(case, "cn_00000000dddd", b.statement)

    await _refixed(eng, case, 15, b_support)
    assert _cause_pool(case)[0] == {root_a.node_id, b.node_id}
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
