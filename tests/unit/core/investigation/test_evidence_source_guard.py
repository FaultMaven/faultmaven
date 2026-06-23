"""A hallucinated/stale ``source_file_id`` must not abort the turn at save.

The schema enforces ``source_file_id`` is PRESENT (unless USER_DESCRIPTION) but
not that it resolves to a real uploaded file; an unresolvable id fails the
``evidence.source_file_id`` FK at save and kills the whole turn. The engine now
drops the file anchor and records the slice as USER_DESCRIPTION instead.
"""

import pytest

from faultmaven.core.investigation.milestone_engine import _resolve_evidence_source
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
    UploadedFile,
)

pytestmark = pytest.mark.unit


def _case(uploaded=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
    )
    case.uploaded_files = uploaded or []
    return case


def _file() -> UploadedFile:
    return UploadedFile(
        file_id="file_0123456789ab",
        filename="app.log",
        size_bytes=10,
        uploaded_at_turn=3,
    )


def test_resolvable_source_file_id_is_preserved():
    f = _file()
    case = _case([f])
    sid, stype = _resolve_evidence_source(case, f.file_id, EvidenceSourceType.LOGS)
    assert sid == f.file_id
    assert stype == EvidenceSourceType.LOGS


def test_unresolvable_source_file_id_falls_back_to_user_description():
    case = _case([_file()])
    sid, stype = _resolve_evidence_source(
        case, "file_deadbeef0000", EvidenceSourceType.LOGS  # no such upload
    )
    assert sid is None  # dropped — no dangling FK
    assert stype == EvidenceSourceType.USER_DESCRIPTION  # content preserved, fileless


def test_null_source_file_id_is_unchanged():
    case = _case([])
    sid, stype = _resolve_evidence_source(
        case, None, EvidenceSourceType.USER_DESCRIPTION
    )
    assert sid is None
    assert stype == EvidenceSourceType.USER_DESCRIPTION
