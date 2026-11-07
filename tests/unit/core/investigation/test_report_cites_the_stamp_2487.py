"""#2487: the report's Confirming Evidence cites the row the confirm-stamp
recorded.

The report qualified a causal_absence row by the cause leg, re-derived after
the stamp ran. The stamp's count-held re-derive can narrow the pool (R1
validates, its same-cause sibling R2 stays INCONCLUSIVE), so the row it had
just recorded on R1 no longer bound and the section showed no causal_absence
row on a CONFIRMED case. The report now admits the stamp's record as
recorded (``confirmation_stamp_rows``) beside the cause leg or the polarity set,
a plain union (INV-30, ruled 2026-11-04).

Rows enter through the real turn application, as in
``test_fix_verification_subjects_1906``.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    cause_elimination_rows,
    confirmation_stamp_rows,
    confirming_polarity_rows,
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
    EvidenceCategory,
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
    _confirming_section,
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
from tests.utils import terminal_verdict

pytestmark = pytest.mark.unit

_C_B = "pg_stat_activity: 12 of 100 slots used"
_UNNAMED = "the old cron entry is gone"
#: shape: (fix turns, c_a's turn, c_b's turn, a newer fix's turn)
_SHAPES = {
    "one-fix": ((6,), 7, 8, None),
    "second-fix": ((6,), 7, 8, 9),
    "stamp-lag": ((), 7, 8, 9),
    "ca-older-window": ((6, 8), 7, 9, None),
    "cb-only+newer-unnamed": ((6,), None, 7, 9),
    "ca-older+newer-unnamed": ((6, 8), 7, 9, 11),
}


def _link(node, evidence_id, stance=EvidenceStance.SUPPORTS) -> None:
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=evidence_id, stance=stance, reasoning="r", linked_at_turn=7
        )
    )


@pytest.mark.parametrize("shape", list(_SHAPES))
@pytest.mark.parametrize("same_line", [True, False], ids=["same-line", "duplicate"])
async def test_the_report_renders_the_row_the_stamp_recorded(same_line, shape):
    """R1 (the hypothesis's root) and R2 (on its line, or a duplicate of it),
    both count-held terminal on one support each. c_a re-checks R1's support,
    and c_b re-checks R2's beside a bound relief, in the fix windows the
    shape gives (``stamp-lag``: the only fix registered after the re-checks;
    ``ca-older``: c_a in an older fix's window). A newer fix with only an
    unnamed causal_absence row follows where the shape says so. The stamp
    records c_b on R1, whose validation narrows the pool to R1, and every
    window re-derived after it sees that pool: the re-derived cause leg and
    polarity set omit c_b, and the section still renders it."""
    case, r1, _hyp = _identified_case()
    r2_links, r1.evidence_links = r1.evidence_links[1:], r1.evidence_links[:1]
    r1.metadata.update(terminal_verdict(r1.evidence_links[0].evidence_id))
    r2 = CausalNode(
        metadata=terminal_verdict(r2_links[0].evidence_id),
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
    fixes, a_turn, b_turn, newer = _SHAPES[shape]
    await _record(eng, case, 5)
    for turn in range(6, b_turn):
        if turn in fixes:
            _accept(case, InvestigationActionType.SOLUTION, turn)
        elif turn == a_turn:
            await _record(eng, case, turn, _obs(_CAUSE_GONE, "reads 100", r1_support))
    (c_b, _gone), _ = await _record(
        eng,
        case,
        b_turn,
        _obs(_CAUSE_GONE, _C_B, r2_links[0].evidence_id),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    if newer:
        _accept(case, InvestigationActionType.SOLUTION, newer)
        await _record(eng, case, newer + 1, _obs(_CAUSE_GONE, _UNNAMED))
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert c_b in cause_elimination_rows(case), "premise: the gate confirmed on it"

    assert finalize_resolution_truth_surface(case)
    assert any(link.evidence_id == c_b.evidence_id for link in r1.evidence_links)
    assert (r1.node_state, r2.node_state) == (
        NodeState.VALIDATED,
        NodeState.INCONCLUSIVE,
    )
    rederived = cause_elimination_rows(case) or confirming_polarity_rows(case)
    assert c_b not in rederived, "the issue's narrowing"
    assert confirmation_stamp_rows(case) == [c_b]
    assert c_b.evidence_id in case.root_cause_conclusion.evidence_basis
    section = _confirming_section(case)
    assert _C_B in section and _UNNAMED not in section


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
        label: _row(label, category, 6, label, collected_by=author)
        for label, category, author in [
            ("stamped", _CAUSE_GONE, "llm"),
            ("engine", _CAUSE_GONE, "engine"),
            ("rung", _CAUSE_GONE, "llm"),
            ("refutes", _CAUSE_GONE, "llm"),
            ("unlinked", _CAUSE_GONE, "llm"),
            ("support", EvidenceCategory.CAUSAL_EVIDENCE, "llm"),
        ]
    }
    case.evidence += rows.values()
    _link(root, rows["stamped"].evidence_id)
    _link(root, rows["engine"].evidence_id)
    _link(rung, rows["rung"].evidence_id)
    _link(root, rows["refutes"].evidence_id, EvidenceStance.REFUTES)
    _link(root, rows["support"].evidence_id)
    assert confirmation_stamp_rows(case) == [rows["stamped"]]


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
