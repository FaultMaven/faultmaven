"""#2487: the report's Confirming Evidence cites the row the confirm-stamp
recorded.

The report qualified a causal_absence row by the cause leg, re-derived after
the stamp ran. The stamp's count-held re-derive can narrow the pool (R1
validates, its same-cause sibling R2 stays INCONCLUSIVE), so the row it had
just recorded on R1 no longer bound and the section showed no causal_absence
row on a CONFIRMED case. The report now also admits the stamp's record
(``confirmation_stamp_rows``), through the polarity bars (INV-30).

Rows enter through the real turn application, as in
``test_fix_verification_subjects_1906``.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    cause_elimination_rows,
    confirmation_stamp_rows,
    confirming_polarity_rows,
    fix_windows,
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
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, bound_evidence
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _confirming_section,
    _fixed,
    _obs,
    _record,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _eid,
    _identified_case,
    _row,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_resolution_backstop_turn import _case

pytestmark = pytest.mark.unit

_C_B = "pg_stat_activity: 12 of 100 slots used"


def _link(node, evidence_id, stance=EvidenceStance.SUPPORTS) -> None:
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=evidence_id, stance=stance, reasoning="r", linked_at_turn=7
        )
    )


@pytest.mark.parametrize("same_line", [True, False], ids=["same-line", "duplicate"])
async def test_the_report_renders_the_row_the_stamp_recorded(same_line):
    """R1 (the hypothesis's root) and R2 (on its line, or a duplicate of it),
    both count-held terminal on one support each. Fixed at 6; c_a re-checks
    R1's support at 7, c_b R2's at 8 beside a bound relief. The stamp records
    c_b on R1, whose validation narrows the pool to R1."""
    case, r1, _hyp = _identified_case()
    r2_links, r1.evidence_links = r1.evidence_links[1:], r1.evidence_links[:1]
    r2 = CausalNode(
        metadata={"root_terminal": True},
        node_id="cn_00000000d00b",
        statement=(
            "every checkout worker blocks waiting on a postgres slot"
            if same_line
            else r1.statement
        ),
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=r2_links,
        generated_at_turn=1,
    )
    case.causal_nodes[r2.node_id] = r2
    problem_id = next(
        n.node_id for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=r2.node_id, effect_node_id=problem_id)
    )
    if same_line:
        case.causal_edges.append(
            CausalEdge(cause_node_id=r1.node_id, effect_node_id=r2.node_id)
        )
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
    rung = CausalNode(
        node_id="cn_00000000a11e",
        statement="the checkout pool waits on a slot",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        generated_at_turn=1,
    )
    case.causal_nodes[rung.node_id] = rung
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
    _link(root, rows["stamped"].evidence_id)
    _link(root, rows["engine"].evidence_id)
    _link(rung, rows["rung"].evidence_id)
    _link(root, rows["refutes"].evidence_id, EvidenceStance.REFUTES)
    assert confirmation_stamp_rows(case) == [rows["stamped"]]


async def test_a_stamp_style_link_on_a_failed_windows_row_does_not_render():
    """A model-authored SUPPORTS link on a causal_absence row, as a case
    stored before #663's refusal can carry, on a row whose fix window FAILED:
    the stamp's record holds the row, the polarity bars refuse it, and the
    section does not render it."""
    eng, case, root, _hyp = _fixed(6)
    (row, _present), _ = await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100 (legacy)", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert fix_windows(case)[-1].state == FIX_FAILED, "premise"
    _link(root, row.evidence_id)
    assert confirmation_stamp_rows(case) == [row], "premise"
    assert row not in confirming_polarity_rows(case), "premise"

    section = _confirming_section(case)
    assert section.startswith("## Confirming Evidence"), "premise"
    assert "(legacy)" not in section


@pytest.mark.parametrize("leg", [True, False], ids=["cause-leg", "polarity"])
async def test_with_no_stamp_the_section_admits_the_leg_or_the_polarity_set(leg):
    """No stamp on record, and an unnamed causal_absence row that passes the
    polarity bars but binds to nothing. With a cause leg the section admits
    the leg alone, on the resolution summary and on the summary of a close
    the user chose over the resolution (#2360); with none, the polarity set."""
    case = _case(absence=_CAUSE_GONE if leg else None, problem_gone=True)
    case.evidence.append(
        bound_evidence(
            _CAUSE_GONE,
            turn=1,
            evidence_id=_eid("unnamed"),
            rechecks=None,
            summary="the pods' token audience now reads sts.amazonaws.com",
        )
    )
    absence = [e for e in case.evidence if e.category == _CAUSE_GONE]
    assert confirmation_stamp_rows(case) == []
    assert confirming_polarity_rows(case) == absence, "premise"
    expected = cause_elimination_rows(case) or absence
    assert len(expected) == 1, "premise: the leg, or the unnamed row alone"

    svc = object.__new__(ReportGenerationService)
    resolved = case.model_copy(update={"state": CaseState.RESOLVED})
    summaries = [await svc._generate_resolution_summary(resolved, {})]
    if leg:
        with_declined_offer(case, kind="resolve", basis="backstop")
        execute_user_closure(case, "user_test")
        assert case.closure_reason == "resolution_declined", "premise"
        summaries.append(await svc._generate_closure_summary(case, {}))
    for summary in summaries:
        section = summary.split("## Confirming Evidence")[1].split("\n## ")[0]
        assert [e for e in absence if e.summary in section] == expected
