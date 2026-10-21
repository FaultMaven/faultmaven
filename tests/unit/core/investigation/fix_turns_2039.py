"""A case driven through the real turn application (``_apply_investigation_updates``)
across a fix and what was seen after it.

The helpers of ``test_m6_relapse_through_the_turn_2039.py``, posted on #2039
(comment 6298472841) and reused by the reproductions of #2105 and #2106: the
cause identified with its support on the root node, the fix proposed and run at
turn 6, the cause and the problem seen gone at 7, the problem gone again at 12.
"""

from faultmaven.core.investigation.cause_assurance import ENGINE_EVIDENCE_AUTHOR
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    InvestigationResponse_Treatment,
    MilestoneUpdates,
    SolutionToAdd,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    EvidenceCategory,
    EvidenceSourceType,
    HypothesisState,
    NodeState,
    SolutionType,
    UploadedFile,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _FILE_ID,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _apply,
    _engine,
)

_TSU = InvestigationResponse_Treatment.TreatmentStateUpdate
_CAUSE_GONE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE
_NEW_FIX = "a new fix after the one that did not hold"


def _ev(category, summary) -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=summary,
        extract=summary,
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
    )


async def _turn(eng, case, turn, *rows, **updates) -> dict:
    case.current_turn = turn
    return await _apply(eng, case, _TSU(evidence_to_add=list(rows), **updates))


def _engine_rows(case):
    return [e for e in case.evidence if e.collected_by == ENGINE_EVIDENCE_AUTHOR]


async def _fixed_and_relieved():
    """Identified; the fix proposed and run at 6 (stamps accepted_in_turn);
    the cause and the problem seen gone at 7; the problem gone again at 12."""
    case, root, hyp = _identified_case()
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="c.log", size_bytes=1, uploaded_at_turn=1
        )
    )
    eng = _engine()
    fix = SolutionToAdd(
        description="raise max_connections to 100",
        solution_type=SolutionType.CONFIG_CHANGE,
        estimated_impact="checkout recovers",
        risks="none",
    )
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[fix],
        milestones=MilestoneUpdates(solution_accepted=True),
    )
    assert [a.accepted_in_turn for a in case.proposed_actions] == [6]
    await _turn(
        eng,
        case,
        7,
        _ev(_CAUSE_GONE, "max_connections now reads 100 after the config change"),
        _ev(_GONE, "checkout returns 200 again, 503 rate 0.0/s"),
    )
    await _turn(eng, case, 12, _ev(_GONE, "checkout log has no 503 lines in 1m"))
    assert case.progress.cause_state == CauseState.IDENTIFIED
    return eng, case, root, hyp


def _assert_standing(case, root, hyp):
    assert hyp.state == HypothesisState.VALIDATED
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.root_cause_conclusion is not None
    assert _engine_rows(case) == []
