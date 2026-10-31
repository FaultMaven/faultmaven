"""An evidence row must name a file of the case, and a refused row keeps its place (#1930).

The schema requires ``source_file_id`` but cannot check that it names a real
upload; an id that names none would fail the ``evidence.source_file_id`` foreign
key at save and abort the whole turn. The engine refuses that ONE row instead —
and never keeps it with no file, because evidence is submitted data.

A refused row must keep its position: ``new_index_N`` evidence refs count the
rows in the order the model wrote them, so dropping one would land every later
ref on the wrong row. The positions live in ``metadata["evidence_slots"]``, where
a refused row holds ``REFUSED_EVIDENCE_SLOT``.
"""

import asyncio
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.milestone_inference import (
    _source_file_resolves,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    ResponseApplier,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    REFUSED_EVIDENCE_SLOT,
)
from faultmaven.core.investigation.problem_status import pending_revision
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.modules.case.contracts import (
    Case,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    ProblemStatus,
    UploadedFile,
)
from tests.case_builders import investigating_case

pytestmark = pytest.mark.unit

_FILE_ID = "file_0123456789ab"
_MISSING_FILE_ID = "file_deadbeef0000"


def _case(uploaded=None) -> Case:
    return investigating_case(
        case_id="case_000000000001",
        current_turn=5,
        statement="X fails",
        symptom="X fails",
        uploaded_files=uploaded or [],
    )


def _file() -> UploadedFile:
    return UploadedFile(
        file_id=_FILE_ID,
        filename="app.log",
        size_bytes=10,
        uploaded_at_turn=3,
    )


def test_a_file_of_the_case_resolves():
    assert _source_file_resolves(_case([_file()]), _FILE_ID) is True


@pytest.mark.parametrize("file_id", [_MISSING_FILE_ID, ""])
def test_an_unknown_or_missing_file_does_not(file_id):
    assert _source_file_resolves(_case([_file()]), file_id) is False


@pytest.fixture
def engine():
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.deps = EngineDeps()
    eng.deps.llm_provider = None
    eng.deps.team_service = None
    eng.deps.share_repository = None
    eng.deps.conversion_service = None
    eng.responses = ResponseApplier(deps=eng.deps, kb_prefetcher=None)
    return eng


def _row(file_id, summary):
    return {
        "summary": summary,
        "category": "symptom_evidence",
        "source_type": "logs",
        "source_file_id": file_id,
    }


def _apply(engine, case, updates):
    metadata = {
        "milestones_completed": [],
        "evidence_added": [],
        "evidence_slots": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }
    asyncio.run(
        engine.responses._apply_investigation_updates(
            case,
            updates,
            metadata,
            SimpleNamespace(evidence_trail=None, agent_response=""),
            "any message",
        )
    )
    return metadata


def test_a_refused_row_creates_nothing_and_keeps_its_slot(engine):
    case = _case([_file()])
    updates = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
        {
            "evidence_to_add": [
                _row(_MISSING_FILE_ID, "refused: names no file of the case"),
                _row(None, "refused: names no file at all"),
                _row(_FILE_ID, "kept"),
            ]
        }
    )

    metadata = _apply(engine, case, updates)

    assert [ev.summary for ev in case.evidence] == ["kept"]
    kept = case.evidence[0].evidence_id
    assert case.evidence[0].source_file_id == _FILE_ID
    assert metadata["evidence_added"] == [kept]
    assert metadata["evidence_slots"] == [
        REFUSED_EVIDENCE_SLOT,
        REFUSED_EVIDENCE_SLOT,
        kept,
    ]


def test_a_ref_past_a_refused_row_lands_on_its_own_row(engine):
    """``new_index_2`` names the third row the model wrote. With the first one
    refused it must still reach that row, not the second, and a ref to the
    refused row must link nothing rather than shift onto its neighbour."""
    case = _case([_file()])
    case.progress.problem_status = ProblemStatus.VERIFIED
    engine.deps.hypothesis_manager = HypothesisManager()
    hypothesis = Hypothesis(
        hypothesis_id="hyp_0123456789ab",
        statement="The pool is exhausted",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        likelihood=0.6,
        initial_likelihood=0.6,
        generated_at_turn=case.current_turn,
        last_updated_turn=case.current_turn,
        last_progress_at_turn=case.current_turn,
        iterations_without_progress=0,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="test",
    )
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis
    updates = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
        {
            "evidence_to_add": [
                _row(_MISSING_FILE_ID, "refused"),
                _row(_FILE_ID, "second"),
                _row(_FILE_ID, "third"),
            ],
            "hypothesis_evidence_links": [
                {
                    "hypothesis_id_ref": hypothesis.hypothesis_id,
                    "evidence_id_ref": "new_index_0",
                    "stance": EvidenceStance.SUPPORTS.value,
                    "reasoning": "names a refused row",
                },
                {
                    "hypothesis_id_ref": hypothesis.hypothesis_id,
                    "evidence_id_ref": "new_index_2",
                    "stance": EvidenceStance.SUPPORTS.value,
                    "reasoning": "names the third row",
                },
            ],
        }
    )

    _apply(engine, case, updates)

    third = next(ev.evidence_id for ev in case.evidence if ev.summary == "third")
    assert [link.evidence_id for link in hypothesis.evidence_links] == [third]


def test_a_revision_citing_a_row_past_a_refused_one_is_proposed(engine):
    """``verification_updates`` evidence refs resolve against the same positions:
    a revised statement citing the second row the model wrote is proposed, not
    refused for citing nothing because the first row was refused (#1930)."""
    case = _case([_file()])
    case.progress.problem_status = ProblemStatus.VERIFIED
    updates = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
        {
            "evidence_to_add": [
                _row(_MISSING_FILE_ID, "refused"),
                _row(_FILE_ID, "checkout returns 500 only for EU card payments"),
            ],
            "verification_updates": {
                "revised_problem_statement": (
                    "Checkout returns HTTP 500 for card payments in the EU region"
                ),
                "revision_basis": "the log shows the 500s only on EU card payments",
                "revision_evidence_ids": ["new_index_1"],
            },
        }
    )

    _apply(engine, case, updates)

    pending = pending_revision(case)
    assert pending is not None, "the revision was refused for citing nothing"
    (row,) = case.evidence
    assert row.evidence_id in pending.evidence_ids
