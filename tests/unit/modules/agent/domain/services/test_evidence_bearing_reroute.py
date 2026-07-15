"""Tests for the TRIAGE→DIRECTED_ANALYSIS reroute on fresh evidence (#708).

Pins the rule that a turn delivering a successfully-classified, extracted
attachment counts as evidence-bearing even under a generic cover message
("here's the logs"). ``classify_query`` only sees the message string, so
such a cover note routes to TRIAGE and lets the agent skip evidence
analysis; the reroute uses the attachment signal the preprocessor already
produced. Pure predicate — no LLM, no model variance.
"""

from __future__ import annotations

from faultmaven.modules.agent.domain.services.investigation_service import (
    _PreprocessedAttachment,
    _turn_delivers_evidence_bearing_attachment,
)
from faultmaven.modules.case.domain.models import UploadedFile


def _preprocessed(
    structural_index: str | None,
    *,
    classification_failed: bool = False,
    idx: int = 1,
) -> _PreprocessedAttachment:
    uf = UploadedFile(
        filename=f"data_{idx}.log",
        size_bytes=1800,
        uploaded_at_turn=3,
    )
    uf.structural_index = structural_index
    return _PreprocessedAttachment(
        uploaded_file=uf,
        classification_failed=classification_failed,
        attachment_filename=uf.filename,
    )


class TestTurnDeliversEvidenceBearingAttachment:
    def test_no_attachments_returns_false(self):
        assert _turn_delivers_evidence_bearing_attachment([]) is False

    def test_extracted_attachment_returns_true(self):
        results = [_preprocessed("timerange: 07:40\n503 x12\nSSLException x3")]
        assert _turn_delivers_evidence_bearing_attachment(results) is True

    def test_classification_failed_returns_false(self):
        """An upload awaiting user clarification is not yet evidence-bearing."""
        results = [
            _preprocessed(
                "some content here that is long enough", classification_failed=True
            )
        ]
        assert _turn_delivers_evidence_bearing_attachment(results) is False

    def test_empty_structural_index_returns_false(self):
        assert (
            _turn_delivers_evidence_bearing_attachment([_preprocessed(None)]) is False
        )

    def test_trivial_structural_index_returns_false(self):
        assert (
            _turn_delivers_evidence_bearing_attachment([_preprocessed("short")])
            is False
        )

    def test_mixed_batch_one_bearing_returns_true(self):
        """One extracted attachment is sufficient even if others failed."""
        results = [
            _preprocessed("short", idx=1),
            _preprocessed("a real structural index with content", idx=2),
        ]
        assert _turn_delivers_evidence_bearing_attachment(results) is True
