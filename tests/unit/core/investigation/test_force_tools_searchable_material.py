"""Tests for the force_tools material predicates (#708, #2375).

Pins the rule that a Directed-Analysis turn forces ``tool_choice=required``
only when the case holds a file a reading tool can open
(``_has_readable_material``): an Evidence row whose file is stored, OR an
upload with a stored raw file and a non-trivial structural index. Post-010, an
evidence-*delivering* turn has only an ``UploadedFile`` (no Evidence row yet),
so ``bool(case.evidence)`` alone left that turn on ``tool_choice=auto`` and let
the agent skip analysis (#708). Such an upload counts when it renders as an
``<uploaded_file>`` (the structural-index threshold) and is marked
``file_backed="true"`` (a stored file, #2375), so forcing tools cannot crash
the tool loop or force a call nothing can answer.

``_has_searchable_material`` decides single-shot routing only: any Evidence
row counts there, and an upload counts by the same stored-and-renders test.

Pure predicate over case state — no LLM, no model variance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _has_searchable_material,
    _route_toolless_turn_single_shot,
    _should_force_tools,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)
from tests.case_builders import make_case


def _make_case() -> Case:
    return make_case(
        user_id="u1",
        enterprise_id="o1",
        title="Test",
        description="Pods are crashing",
    )


def _make_evidence(
    idx: int = 1, source_file_id: str | None = "file_0123456789ab"
) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{idx:012d}",
        summary=f"Test evidence {idx}",
        content_ref=f"test_{idx}.log",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=(
            EvidenceSourceType.LOGS
            if source_file_id
            else EvidenceSourceType.ENGINE_INFERENCE
        ),
        collected_at=datetime.now(timezone.utc),
        collected_by="user_test",
        primary_purpose="Testing",
        preprocessed_content="content",
        content_size_bytes=100,
        preprocessing_method="manual",
        source_file_id=source_file_id,
        collected_at_turn=1,
    )


def _make_uploaded_file(
    structural_index: str | None, idx: int = 1, stored: bool = True
) -> UploadedFile:
    uf = UploadedFile(
        filename=f"data_{idx}.log",
        size_bytes=1800,
        uploaded_at_turn=3,
        storage_ref=f"org/case/data_{idx}.log" if stored else None,
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
        """Routing: any Evidence row is material, whether or not its file is
        stored. Forcing needs a stored file
        (``TestForcingNeedsAFileAToolCanRead``)."""
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

    def test_uploaded_file_with_no_stored_file_returns_false(self):
        """#2375: an upload with no stored file renders without
        ``file_backed`` and no reader can open it, so it forces nothing."""
        case = _make_case()
        case.uploaded_files.append(
            _make_uploaded_file(
                "timerange: 07:40-07:41\n503 x12\nSSLException x3", stored=False
            )
        )
        assert _has_searchable_material(case) is False
        assert (
            _should_force_tools("directed_analysis", case, has_pending=False) is False
        )

    def test_uploaded_file_without_index_returns_false(self):
        """A placeholder/unanalyzable upload with no structural content is not
        a search target — do not force tools onto an empty loop."""
        case = _make_case()
        case.uploaded_files.append(_make_uploaded_file(None))
        assert _has_searchable_material(case) is False

    def test_uploaded_file_trivial_index_returns_false(self):
        """Below the >10-char threshold under which the context builder
        renders no ``<uploaded_file>`` — treated as no target."""
        case = _make_case()
        case.uploaded_files.append(_make_uploaded_file("short"))
        assert _has_searchable_material(case) is False


class TestShouldForceTools:
    """The force_tools linchpin (#708). ``directed_analysis`` AND a file a
    reading tool can open (``_has_readable_material``: a row whose file is
    stored, or a stored upload that renders, #2375) AND not mid-confirmation
    must all hold. Pins the exact flip the fix depends on: a fresh
    evidence-bearing upload turn (rerouted to directed_analysis, with a stored,
    rendering UploadedFile and no prior Evidence) forces tools instead of
    leaving the turn on tool_choice=auto."""

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


class TestForcingNeedsAFileAToolCanRead:
    """#2375: a forced call needs a file a directed-analysis tool can read.
    A row counts only when its file is stored. Routing still counts any row
    (``_has_searchable_material``): ``case_evidence_search`` (given only with a
    case vector store) and the entity tools (given only under
    ``FAULTMAVEN_ENTITY_REGISTRY``) work over rows, so a case with rows stays
    on the tool loop."""

    @staticmethod
    def _case(rows, uploads=()) -> Case:
        case = _make_case()
        case.evidence.extend(rows)
        case.uploaded_files.extend(uploads)
        return case

    def test_an_inference_only_case_does_not_force(self):
        case = self._case([_make_evidence(source_file_id=None)])

        assert (
            _should_force_tools("directed_analysis", case, has_pending=False) is False
        )
        assert (
            _route_toolless_turn_single_shot("directed_analysis", case, False) is False
        )

    def test_a_case_whose_only_row_has_no_stored_file_does_not_force(self):
        upload = _make_uploaded_file("timerange: 07:40\n503 x12", stored=False)
        case = self._case([_make_evidence(source_file_id=upload.file_id)], [upload])

        assert (
            _should_force_tools("directed_analysis", case, has_pending=False) is False
        )
        assert (
            _route_toolless_turn_single_shot("directed_analysis", case, False) is False
        )

    def test_a_row_whose_file_is_stored_forces(self):
        # The upload's own index is too short to render, so only the row can
        # make this material.
        upload = _make_uploaded_file(None)
        case = self._case([_make_evidence(source_file_id=upload.file_id)], [upload])

        assert _should_force_tools("directed_analysis", case, has_pending=False) is True
