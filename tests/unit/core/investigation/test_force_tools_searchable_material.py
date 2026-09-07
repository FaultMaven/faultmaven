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

from faultmaven.core.investigation.milestone_engine import (
    _has_searchable_material,
    _should_force_tools,
)
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
        enterprise_id="o1",
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


class TestShouldForceTools:
    """The force_tools linchpin (#708). ``directed_analysis`` AND searchable
    material AND not mid-confirmation must all hold. Pins the exact flip the
    fix depends on: a fresh evidence-bearing upload turn (rerouted to
    directed_analysis, with a searchable UploadedFile and no prior Evidence)
    forces tools instead of leaving the turn on tool_choice=auto."""

    def _case_with_fresh_upload(self) -> Case:
        """The #708 delivering-turn shape: a searchable UploadedFile, no
        Evidence row yet, no pending transition."""
        case = _make_case()
        assert case.evidence == []
        case.uploaded_files.append(
            _make_uploaded_file("timerange: 07:40-07:41\n503 x12\nSSLException x3")
        )
        return case

    def test_da_with_fresh_upload_forces_tools(self):
        """The exact bug turn: rerouted to DA, only an UploadedFile present."""
        case = self._case_with_fresh_upload()
        assert _should_force_tools("directed_analysis", case, has_pending=False) is True

    def test_triage_never_forces_tools(self):
        """The pre-fix path: a TRIAGE turn does not force tools even with a
        searchable upload."""
        case = self._case_with_fresh_upload()
        assert _should_force_tools("triage", case, has_pending=False) is False

    def test_da_without_searchable_material_does_not_force(self):
        """DA classification alone is not enough — nothing to search means no
        forced tools (avoids crashing the empty tool loop)."""
        case = _make_case()
        assert (
            _should_force_tools("directed_analysis", case, has_pending=False) is False
        )

    def test_da_with_pending_transition_does_not_force(self):
        """Mid-confirmation: a typed confirm/decline must not be forced into
        tools even when classified DA with searchable material."""
        case = self._case_with_fresh_upload()
        assert _should_force_tools("directed_analysis", case, has_pending=True) is False

    def test_none_mode_does_not_force(self):
        """Engine entry points that thread no query_mode and fall back to a
        None mode must not force tools."""
        case = self._case_with_fresh_upload()
        assert _should_force_tools(None, case, has_pending=False) is False
