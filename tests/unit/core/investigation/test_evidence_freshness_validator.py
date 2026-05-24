"""Freshness validator for evidence_to_add — guards against the agent
recycling a prior-turn file's content as if it were "the latest" data.

Origin: Run 23 (2026-05-24) surfaced FaultMaven creating new Evidence
rows pointing at prior-turn files when the user said "here are the
latest logs" without attaching anything. See
project_fm_evidence_recycling_bug.md memory entry.

The fix has two surfaces, both tested here:
1. The ``re_analysis_reason`` field on ``EvidenceToAdd``
2. The pure helper ``filter_stale_source_evidence`` in milestone_engine
   that soft-rejects rows pointing at prior-turn files without
   re_analysis_reason
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import (
    filter_stale_source_evidence,
)
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.domain.models import (
    EvidenceCategory,
    EvidenceSourceType,
)


@pytest.mark.unit
class TestReAnalysisReasonField:
    """The schema field itself accepts None or a string."""

    def test_default_is_none(self):
        ev = EvidenceToAdd(
            summary="symptom seen",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_abc",
        )
        assert ev.re_analysis_reason is None

    def test_accepts_string(self):
        ev = EvidenceToAdd(
            summary="comparing T7 baseline against T13 post-fix logs",
            category=EvidenceCategory.MITIGATION_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_abc",
            re_analysis_reason="Comparing pre- and post-mitigation log behavior",
        )
        assert (
            ev.re_analysis_reason == "Comparing pre- and post-mitigation log behavior"
        )


# ─── Pure helper tests ───────────────────────────────────────────────────────


def _f(file_id: str, turn: int):
    """Stub uploaded_file with the two attributes the helper reads."""
    return SimpleNamespace(file_id=file_id, uploaded_at_turn=turn)


def _ev(source_file_id: str | None = None, re_analysis_reason: str | None = None):
    """EvidenceToAdd-shaped object — using SimpleNamespace to avoid the
    schema's other invariants (source_type / category coupling) since
    we only exercise the freshness check here."""
    return SimpleNamespace(
        summary="test ev",
        source_file_id=source_file_id,
        re_analysis_reason=re_analysis_reason,
    )


@pytest.mark.unit
class TestFilterStaleSourceEvidence:

    def test_current_turn_file_accepted(self):
        files = [_f("file_now", 5)]
        items = [_ev(source_file_id="file_now")]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=5)
        assert len(valid) == 1
        assert rejections == []

    def test_prior_turn_file_without_reason_rejected(self):
        files = [_f("file_old", 3)]
        items = [_ev(source_file_id="file_old")]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        assert valid == []
        assert len(rejections) == 1
        r = rejections[0]
        assert r["source_file_id"] == "file_old"
        assert r["file_uploaded_at_turn"] == 3
        assert r["current_turn"] == 9
        assert r["rejected_summary"] == "test ev"

    def test_prior_turn_file_with_reason_accepted(self):
        files = [_f("file_old", 3)]
        items = [
            _ev(
                source_file_id="file_old",
                re_analysis_reason="Comparing baseline against post-fix logs",
            )
        ]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        assert len(valid) == 1
        assert rejections == []

    def test_empty_reason_treated_as_missing(self):
        """Empty string for re_analysis_reason must NOT be a valid bypass —
        the LLM should provide a real justification, not just any truthy value."""
        files = [_f("file_old", 3)]
        items = [_ev(source_file_id="file_old", re_analysis_reason="")]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        # An empty string is falsy → treated as missing → rejected.
        assert valid == []
        assert len(rejections) == 1

    def test_user_description_evidence_unaffected(self):
        """source_file_id=None (USER_DESCRIPTION case) bypasses the check."""
        files = [_f("file_old", 3)]
        items = [_ev(source_file_id=None)]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        assert len(valid) == 1
        assert rejections == []

    def test_unknown_file_id_passes_through(self):
        """Conservative: if source_file_id isn't in uploaded_files, this
        validator doesn't reject — other validators handle nonexistent IDs."""
        files = [_f("file_other", 3)]
        items = [_ev(source_file_id="file_nonexistent")]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        assert len(valid) == 1
        assert rejections == []

    def test_mixed_batch_drops_only_stale(self):
        files = [_f("file_old", 3), _f("file_new", 9)]
        items = [
            _ev(source_file_id="file_new"),  # accept
            _ev(source_file_id="file_old"),  # reject (no reason)
            _ev(
                source_file_id="file_old",
                re_analysis_reason="comparing baselines",
            ),  # accept (has reason)
        ]
        valid, rejections = filter_stale_source_evidence(items, files, current_turn=9)
        assert len(valid) == 2
        assert len(rejections) == 1

    def test_empty_inputs(self):
        valid, rejections = filter_stale_source_evidence([], [], current_turn=1)
        assert valid == []
        assert rejections == []

    def test_no_uploaded_files(self):
        """No uploaded_files registry → can't check freshness → pass through."""
        items = [_ev(source_file_id="file_anything")]
        valid, rejections = filter_stale_source_evidence(items, [], current_turn=5)
        assert len(valid) == 1
        assert rejections == []

    def test_uploaded_file_missing_turn_attr(self):
        """Defensive: skip files whose uploaded_at_turn is None."""
        broken_file = SimpleNamespace(file_id="file_x", uploaded_at_turn=None)
        items = [_ev(source_file_id="file_x")]
        valid, rejections = filter_stale_source_evidence(
            items, [broken_file], current_turn=5
        )
        # file_x isn't in the turn map → treated as unknown → pass through
        assert len(valid) == 1
        assert rejections == []
