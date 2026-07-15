"""Tests for the force_tools searchable-material predicate (#708).

Pins the rule that a Directed-Analysis turn forces ``tool_choice=required``
whenever the case holds content ``search_file`` can target — either an
existing Evidence row OR a fresh uploaded file with a non-trivial structural
index. Post-010, an evidence-*delivering* turn has only an ``UploadedFile``
(no Evidence row yet), so ``bool(case.evidence)`` alone left that turn on
``tool_choice=auto`` and let the agent skip analysis. ``_has_searchable_material``
closes that gap while guaranteeing a real search target (mirroring the
context builder's ``searchable="true"`` threshold) so forcing tools cannot
crash the tool loop.

Pure predicate over case state — no LLM, no model variance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultmaven.core.investigation.milestone_engine import _has_searchable_material
from faultmaven.modules.case.domain.models import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)


def _make_case() -> Case:
    return Case(
        user_id="u1",
        organization_id="o1",
        title="Test",
        description="Pods are crashing",
    )


def _make_evidence(idx: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{idx:012d}",
        summary=f"Test evidence {idx}",
        content_ref=f"test_{idx}.log",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(timezone.utc),
        collected_by="user_test",
        primary_purpose="Testing",
        preprocessed_content="content",
        content_size_bytes=100,
        preprocessing_method="manual",
        source_file_id=None,
        collected_at_turn=1,
    )


def _make_uploaded_file(structural_index: str | None, idx: int = 1) -> UploadedFile:
    uf = UploadedFile(
        filename=f"data_{idx}.log",
        size_bytes=1800,
        uploaded_at_turn=3,
    )
    uf.structural_index = structural_index
    return uf


class TestHasSearchableMaterial:
    def test_empty_case_returns_false(self):
        case = _make_case()
        assert case.evidence == []
        assert case.uploaded_files == []
        assert _has_searchable_material(case) is False

    def test_existing_evidence_returns_true(self):
        """Prior behavior preserved: any Evidence row satisfies the guard."""
        case = _make_case()
        case.evidence.append(_make_evidence())
        assert _has_searchable_material(case) is True

    def test_fresh_uploaded_file_with_index_returns_true(self):
        """#708: a delivering turn has an UploadedFile but no Evidence yet —
        the file is still a searchable target."""
        case = _make_case()
        assert case.evidence == []
        case.uploaded_files.append(
            _make_uploaded_file("timerange: 07:40-07:41\n503 x12\nSSLException x3")
        )
        assert _has_searchable_material(case) is True

    def test_uploaded_file_without_index_returns_false(self):
        """A placeholder/unanalyzable upload with no structural content is not
        a search target — do not force tools onto an empty loop."""
        case = _make_case()
        case.uploaded_files.append(_make_uploaded_file(None))
        assert _has_searchable_material(case) is False

    def test_uploaded_file_trivial_index_returns_false(self):
        """Below the >10-char threshold the context builder uses to mark a
        file searchable — treated as no target."""
        case = _make_case()
        case.uploaded_files.append(_make_uploaded_file("short"))
        assert _has_searchable_material(case) is False
