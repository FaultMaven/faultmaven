"""Tests for the Hypothesis refutation_reason invariant.

Pair integrity: status=REFUTED and refutation_reason travel together.
A Hypothesis cannot exist in memory with one but not the other; RETIRED
is the fallback when there is no disproof evidence.

Reference: investigation-journal.md Phase 3, agent-behavioral-rules.md Rule 2.
"""

import pytest
from pydantic import ValidationError

from faultmaven.modules.case.domain.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
)


def _hypothesis_fields(**overrides):
    """Common required fields for Hypothesis construction in these tests."""
    base = dict(
        statement="test hypothesis",
        category=HypothesisCategory.CODE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="test rationale",
        generated_at_turn=1,
    )
    base.update(overrides)
    return base


class TestRefutationReasonInvariant:
    """Pair integrity between status=REFUTED and refutation_reason."""

    @pytest.mark.unit
    def test_refuted_with_reason_accepted(self):
        """REFUTED with a refutation_reason is the canonical refutation."""
        h = Hypothesis(
            status=HypothesisStatus.REFUTED,
            refutation_reason="metrics at 14:02 ruled out connection pool exhaustion",
            **_hypothesis_fields(),
        )
        assert h.status == HypothesisStatus.REFUTED
        assert h.refutation_reason is not None

    @pytest.mark.unit
    def test_refuted_without_reason_rejected(self):
        """A Hypothesis cannot be constructed with REFUTED and no reason."""
        with pytest.raises(ValidationError) as excinfo:
            Hypothesis(
                status=HypothesisStatus.REFUTED,
                **_hypothesis_fields(),
            )
        assert "refutation_reason is required" in str(excinfo.value)
        assert "RETIRED" in str(excinfo.value)  # fallback hint

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            HypothesisStatus.CAPTURED,
            HypothesisStatus.ACTIVE,
            HypothesisStatus.VALIDATED,
            HypothesisStatus.RETIRED,
            HypothesisStatus.INCONCLUSIVE,
        ],
    )
    def test_non_refuted_with_reason_rejected(self, status):
        """refutation_reason is only valid when status=REFUTED."""
        with pytest.raises(ValidationError) as excinfo:
            Hypothesis(
                status=status,
                refutation_reason="should not be here",
                **_hypothesis_fields(),
            )
        assert "only valid when status=REFUTED" in str(excinfo.value)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            HypothesisStatus.CAPTURED,
            HypothesisStatus.ACTIVE,
            HypothesisStatus.VALIDATED,
            HypothesisStatus.RETIRED,
            HypothesisStatus.INCONCLUSIVE,
        ],
    )
    def test_non_refuted_without_reason_accepted(self, status):
        """Every non-REFUTED status can exist without a refutation_reason."""
        h = Hypothesis(status=status, **_hypothesis_fields())
        assert h.status == status
        assert h.refutation_reason is None

    @pytest.mark.unit
    def test_refutation_reason_max_length_enforced(self):
        """refutation_reason is capped at 200 chars to keep context dense."""
        long_reason = "x" * 201
        with pytest.raises(ValidationError) as excinfo:
            Hypothesis(
                status=HypothesisStatus.REFUTED,
                refutation_reason=long_reason,
                **_hypothesis_fields(),
            )
        assert "200" in str(excinfo.value) or "at most" in str(excinfo.value).lower()

    @pytest.mark.unit
    def test_refutation_reason_at_max_length_accepted(self):
        """Exactly 200 chars is the boundary — accepted."""
        h = Hypothesis(
            status=HypothesisStatus.REFUTED,
            refutation_reason="x" * 200,
            **_hypothesis_fields(),
        )
        assert len(h.refutation_reason) == 200

    @pytest.mark.unit
    def test_empty_reason_treated_as_missing(self):
        """An empty string for refutation_reason is treated as missing."""
        with pytest.raises(ValidationError):
            Hypothesis(
                status=HypothesisStatus.REFUTED,
                refutation_reason="",
                **_hypothesis_fields(),
            )
