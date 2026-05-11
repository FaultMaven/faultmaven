"""EvidenceToAdd.summary soft-truncation contract — ISS-057.

The Evidence domain model caps ``summary`` at 500 chars (max_length=500
on ``modules/case/domain/models.py``). Verbose providers — DeepSeek V4
Pro on logs-zookeeper q4 in particular — overshoot that bound and the
turn used to 500 with a Pydantic ValidationError leaking out to the
client.

This pins the contract that the LLM-output schema
(``core/investigation/schemas.py:EvidenceToAdd``) soft-truncates the
summary before it propagates downstream into the domain Evidence model.
Truncation is preferred over hard rejection because the summary is
cosmetic and losing the entire turn over a 50-char overshoot is the
worse failure mode (multi-provider robustness gap).

Same graceful-degrade pattern as the binary-decode placeholder.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceSourceType,
)


def _mk_evidence(summary: str) -> EvidenceToAdd:
    # Post-010: 4 categories survive; USER_DESCRIPTION source_type lets us
    # construct without needing a source_file_id (the source-invariant
    # validator's exception case).
    return EvidenceToAdd(
        summary=summary,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
    )


@pytest.mark.unit
class TestEvidenceSummaryTruncation:
    def test_under_500_passes_unchanged(self):
        """Short summaries are not modified."""
        s = "ZooKeeper quorum elected leader at 19:37:21"
        ev = _mk_evidence(s)
        assert ev.summary == s

    def test_exactly_500_passes_unchanged(self):
        """The boundary value (500 chars) is accepted as-is."""
        s = "x" * 500
        ev = _mk_evidence(s)
        assert ev.summary == s
        assert len(ev.summary) == 500

    def test_501_truncates_with_marker(self):
        """One char over the bound triggers truncation + marker."""
        s = "x" * 501
        ev = _mk_evidence(s)
        assert len(ev.summary) <= 500
        assert ev.summary.endswith(" [...trunc]")

    def test_long_overshoot_truncates_to_500(self):
        """A heavily over-length summary (e.g. DeepSeek's 700 chars) fits."""
        s = "x" * 700
        ev = _mk_evidence(s)
        assert len(ev.summary) <= 500
        assert ev.summary.endswith(" [...trunc]")
        # Original prefix preserved (loose check — first 50 chars same)
        assert ev.summary.startswith("x" * 50)

    def test_non_string_passthrough(self):
        """Non-str values pass through unchanged (other validators handle)."""
        # Pydantic will reject non-str at the type level after the validator;
        # the validator itself must not crash on non-str inputs.
        from faultmaven.core.investigation.schemas import EvidenceToAdd as _E

        assert _E.truncate_summary(None) is None
        assert _E.truncate_summary(42) == 42
