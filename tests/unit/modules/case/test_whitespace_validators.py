"""Cross-layer whitespace-rejection validators (Cat #10 audit follow-up).

Each test pins a Pydantic validator against the matching DB CHECK
constraint. Pydantic ``min_length=1`` accepts a single space; the DB
``LENGTH(TRIM(col)) > 0`` CHECK rejects it. Without the validator the
two layers disagree, and a domain object that Pydantic accepts
IntegrityError's at the persist boundary.

Mirrored pairs covered here:
    Evidence.summary       <-> evidence_summary_not_empty
    Hypothesis.statement   <-> hypotheses_statement_not_empty
    UploadedFile.filename  <-> uploaded_files_filename_not_empty
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    UploadedFile,
)


def _evidence_kwargs(**overrides):
    base = dict(
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="non-empty summary",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user-1",
        collected_at_turn=1,
    )
    base.update(overrides)
    return base


def _hypothesis_kwargs(**overrides):
    base = dict(
        statement="non-empty statement",
        category=HypothesisCategory.CODE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        generated_at_turn=1,
    )
    base.update(overrides)
    return base


def _uploaded_file_kwargs(**overrides):
    base = dict(
        filename="report.log",
        size_bytes=10,
        uploaded_at_turn=0,
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("bad_value", [" ", "  ", "\t", "\n", "  \t\n"])
def test_evidence_summary_rejects_whitespace_only(bad_value: str):
    with pytest.raises(ValidationError, match="whitespace"):
        Evidence(**_evidence_kwargs(summary=bad_value))


def test_evidence_summary_accepts_normal_value():
    ev = Evidence(**_evidence_kwargs(summary="real content"))
    assert ev.summary == "real content"


@pytest.mark.parametrize("bad_value", [" ", "  ", "\t", "\n", "  \t\n"])
def test_hypothesis_statement_rejects_whitespace_only(bad_value: str):
    with pytest.raises(ValidationError, match="whitespace"):
        Hypothesis(**_hypothesis_kwargs(statement=bad_value))


def test_hypothesis_statement_accepts_normal_value():
    hyp = Hypothesis(**_hypothesis_kwargs(statement="db pool exhausted"))
    assert hyp.statement == "db pool exhausted"


@pytest.mark.parametrize("bad_value", [" ", "  ", "\t", "\n", "  \t\n"])
def test_uploaded_file_filename_rejects_whitespace_only(bad_value: str):
    with pytest.raises(ValidationError, match="whitespace"):
        UploadedFile(**_uploaded_file_kwargs(filename=bad_value))


def test_uploaded_file_filename_accepts_normal_value():
    f = UploadedFile(**_uploaded_file_kwargs(filename="logs.tar.gz"))
    assert f.filename == "logs.tar.gz"
