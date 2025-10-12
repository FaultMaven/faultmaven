"""Unit tests for HypothesisManager

Tests:
- Hypothesis creation and lifecycle
- Confidence decay mechanism
- Anchoring detection and prevention
- Alternative hypothesis generation
- Hypothesis validation and categorization

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime

from faultmaven.core.investigation.hypothesis_manager import (
    HypothesisManager,
    create_hypothesis_manager,
)
from faultmaven.models.investigation import (
    Hypothesis,
    HypothesisStatus,
)


class TestHypothesisCreation:
    """Test hypothesis creation and initialization"""

    def test_create_hypothesis_basic(self):
        """Test creating a basic hypothesis"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Database connection pool exhausted",
            category="infrastructure",
            initial_likelihood=0.75,
            current_turn=1,
        )

        assert hypothesis.statement == "Database connection pool exhausted"
        assert hypothesis.category == "infrastructure"
        assert hypothesis.likelihood == 0.75
        assert hypothesis.initial_likelihood == 0.75
        assert hypothesis.status == HypothesisStatus.PROPOSED
        assert hypothesis.iterations_without_progress == 0
        assert len(hypothesis.hypothesis_id) > 0

    def test_create_hypothesis_with_evidence(self):
        """Test creating hypothesis with supporting evidence"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Recent deployment introduced regression",
            category="code",
            initial_likelihood=0.80,
            current_turn=1,
        )

        # Add supporting evidence
        hypothesis.supporting_evidence.append("evidence_001")
        hypothesis.supporting_evidence.append("evidence_002")

        assert len(hypothesis.supporting_evidence) == 2
        assert len(hypothesis.refuting_evidence) == 0

    def test_create_multiple_hypotheses(self):
        """Test creating multiple hypotheses with unique IDs"""
        manager = create_hypothesis_manager()

        h1 = manager.create_hypothesis("Hypothesis 1", "code", 0.7, 1)
        h2 = manager.create_hypothesis("Hypothesis 2", "config", 0.6, 1)
        h3 = manager.create_hypothesis("Hypothesis 3", "infrastructure", 0.5, 1)

        # All should have unique IDs
        assert h1.hypothesis_id != h2.hypothesis_id
        assert h2.hypothesis_id != h3.hypothesis_id
        assert h1.hypothesis_id != h3.hypothesis_id


class TestConfidenceDecay:
    """Test confidence decay mechanism"""

    def test_apply_confidence_decay_one_iteration(self):
        """Test decay after one iteration without progress"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Memory leak in service",
            category="code",
            initial_likelihood=0.80,
            current_turn=1,
        )

        # Mark as testing
        hypothesis.status = HypothesisStatus.TESTING
        hypothesis.iterations_without_progress = 1

        # Apply decay: 0.80 × 0.85^1 = 0.68
        decayed = manager.apply_confidence_decay(hypothesis, current_turn=2)

        assert decayed.likelihood == pytest.approx(0.68, abs=0.01)
        assert decayed.initial_likelihood == 0.80  # Unchanged

    def test_apply_confidence_decay_multiple_iterations(self):
        """Test decay after multiple iterations"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Configuration mismatch",
            category="config",
            initial_likelihood=0.70,
            current_turn=1,
        )

        hypothesis.status = HypothesisStatus.TESTING

        # After 3 iterations: 0.70 × 0.85^3 = 0.43
        hypothesis.iterations_without_progress = 3
        decayed = manager.apply_confidence_decay(hypothesis, current_turn=4)

        assert decayed.likelihood == pytest.approx(0.43, abs=0.02)

    def test_no_decay_for_proposed_hypothesis(self):
        """Test that proposed hypotheses don't decay"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="New hypothesis",
            category="code",
            initial_likelihood=0.75,
            current_turn=1,
        )

        # Status is PROPOSED, no decay should occur
        assert hypothesis.status == HypothesisStatus.PROPOSED

        decayed = manager.apply_confidence_decay(hypothesis, current_turn=2)
        assert decayed.likelihood == 0.75  # Unchanged

    def test_no_decay_for_validated_hypothesis(self):
        """Test that validated hypotheses don't decay"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Validated root cause",
            category="code",
            initial_likelihood=0.85,
            current_turn=1,
        )

        hypothesis.status = HypothesisStatus.VALIDATED
        hypothesis.iterations_without_progress = 5  # Should be ignored

        decayed = manager.apply_confidence_decay(hypothesis, current_turn=6)
        assert decayed.likelihood == 0.85  # No decay for validated


class TestConfidenceUpdate:
    """Test hypothesis confidence updates"""

    def test_update_hypothesis_confidence_increase(self):
        """Test increasing hypothesis confidence"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="API rate limiting triggered",
            category="infrastructure",
            initial_likelihood=0.60,
            current_turn=1,
        )

        # Update confidence with new evidence
        updated = manager.update_hypothesis_confidence(
            hypothesis=hypothesis,
            new_likelihood=0.85,
            current_turn=2,
            reason="Found rate limit errors in logs",
        )

        assert updated.likelihood == 0.85
        assert updated.iterations_without_progress == 0  # Reset on update

    def test_update_hypothesis_confidence_decrease(self):
        """Test decreasing hypothesis confidence"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Network latency issue",
            category="infrastructure",
            initial_likelihood=0.80,
            current_turn=1,
        )

        # Update with lower confidence
        updated = manager.update_hypothesis_confidence(
            hypothesis=hypothesis,
            new_likelihood=0.50,
            current_turn=2,
            reason="Network metrics look normal",
        )

        assert updated.likelihood == 0.50


class TestAnchoringDetection:
    """Test anchoring bias detection"""

    def test_detect_anchoring_same_category(self):
        """Test detection when 4+ hypotheses in same category"""
        manager = create_hypothesis_manager()

        hypotheses = [
            manager.create_hypothesis(f"Code issue {i}", "code", 0.7, i)
            for i in range(1, 5)
        ]

        # All are "code" category
        for h in hypotheses:
            h.status = HypothesisStatus.TESTING

        anchored, reason, alternatives = manager.detect_anchoring(
            hypotheses=hypotheses,
            current_iteration=5,
        )

        assert anchored is True
        assert "same category" in reason.lower()
        assert len(alternatives) > 0

    def test_detect_anchoring_stalled_iterations(self):
        """Test detection when hypothesis stalled for 3+ iterations"""
        manager = create_hypothesis_manager()

        hypothesis = manager.create_hypothesis(
            statement="Stalled hypothesis",
            category="config",
            initial_likelihood=0.70,
            current_turn=1,
        )

        hypothesis.status = HypothesisStatus.TESTING
        hypothesis.iterations_without_progress = 3

        anchored, reason, alternatives = manager.detect_anchoring(
            hypotheses=[hypothesis],
            current_iteration=4,
        )

        assert anchored is True
        assert "stalled" in reason.lower()

    def test_no_anchoring_diverse_hypotheses(self):
        """Test no anchoring with diverse hypotheses"""
        manager = create_hypothesis_manager()

        hypotheses = [
            manager.create_hypothesis("Code issue", "code", 0.7, 1),
            manager.create_hypothesis("Config issue", "config", 0.6, 1),
            manager.create_hypothesis("Infra issue", "infrastructure", 0.5, 1),
        ]

        for h in hypotheses:
            h.status = HypothesisStatus.TESTING
            h.iterations_without_progress = 1  # Not stalled

        anchored, reason, alternatives = manager.detect_anchoring(
            hypotheses=hypotheses,
            current_iteration=2,
        )

        assert anchored is False


class TestAlternativeGeneration:
    """Test forced alternative hypothesis generation"""

    def test_force_alternative_generation(self):
        """Test generating alternatives to break anchoring"""
        manager = create_hypothesis_manager()

        existing = [
            manager.create_hypothesis("Code bug", "code", 0.7, 1),
            manager.create_hypothesis("Another code bug", "code", 0.6, 1),
        ]

        result = manager.force_alternative_generation(
            existing_hypotheses=existing,
            current_turn=3,
        )

        assert result["action"] == "force_alternatives"
        assert len(result["suggested_categories"]) > 0
        # Should suggest non-code categories
        assert "infrastructure" in result["suggested_categories"] or \
               "config" in result["suggested_categories"]


class TestHypothesisValidation:
    """Test hypothesis validation and retrieval"""

    def test_get_validated_hypothesis(self):
        """Test retrieving validated hypothesis"""
        manager = create_hypothesis_manager()

        h1 = manager.create_hypothesis("Unvalidated", "code", 0.7, 1)
        h2 = manager.create_hypothesis("Validated root cause", "config", 0.9, 1)
        h3 = manager.create_hypothesis("Refuted", "infrastructure", 0.3, 1)

        h2.status = HypothesisStatus.VALIDATED
        h3.status = HypothesisStatus.REFUTED

        validated = manager.get_validated_hypothesis([h1, h2, h3])

        assert validated is not None
        assert validated.hypothesis_id == h2.hypothesis_id
        assert validated.statement == "Validated root cause"

    def test_get_validated_hypothesis_none(self):
        """Test when no hypothesis is validated"""
        manager = create_hypothesis_manager()

        hypotheses = [
            manager.create_hypothesis("H1", "code", 0.7, 1),
            manager.create_hypothesis("H2", "config", 0.6, 1),
        ]

        validated = manager.get_validated_hypothesis(hypotheses)
        assert validated is None


class TestHypothesisLifecycle:
    """Test complete hypothesis lifecycle"""

    def test_hypothesis_lifecycle_validated(self):
        """Test hypothesis from creation to validation"""
        manager = create_hypothesis_manager()

        # Create hypothesis
        h = manager.create_hypothesis(
            statement="Database deadlock",
            category="infrastructure",
            initial_likelihood=0.60,
            current_turn=1,
        )

        assert h.status == HypothesisStatus.PROPOSED

        # Start testing
        h.status = HypothesisStatus.TESTING
        h.supporting_evidence.append("evidence_001")

        # Update confidence
        h = manager.update_hypothesis_confidence(h, 0.75, 2, "Found deadlock traces")
        assert h.likelihood == 0.75

        # More evidence
        h.supporting_evidence.append("evidence_002")
        h = manager.update_hypothesis_confidence(h, 0.90, 3, "Reproduced deadlock")

        # Validate
        h.status = HypothesisStatus.VALIDATED
        assert h.status == HypothesisStatus.VALIDATED
        assert h.likelihood == 0.90

    def test_hypothesis_lifecycle_refuted(self):
        """Test hypothesis from creation to refutation"""
        manager = create_hypothesis_manager()

        h = manager.create_hypothesis(
            statement="Network issue",
            category="infrastructure",
            initial_likelihood=0.70,
            current_turn=1,
        )

        # Start testing
        h.status = HypothesisStatus.TESTING

        # Find refuting evidence
        h.refuting_evidence.append("evidence_003")
        h = manager.update_hypothesis_confidence(h, 0.40, 2, "Network metrics normal")

        # More refuting evidence
        h.refuting_evidence.append("evidence_004")
        h = manager.update_hypothesis_confidence(h, 0.15, 3, "Issue persists with network disabled")

        # Refute
        h.status = HypothesisStatus.REFUTED
        assert h.status == HypothesisStatus.REFUTED
        assert h.likelihood < 0.20


class TestEdgeCases:
    """Test edge cases and error conditions"""

    def test_create_hypothesis_with_zero_likelihood(self):
        """Test creating hypothesis with 0% likelihood"""
        manager = create_hypothesis_manager()

        h = manager.create_hypothesis(
            statement="Unlikely cause",
            category="code",
            initial_likelihood=0.0,
            current_turn=1,
        )

        assert h.likelihood == 0.0

    def test_create_hypothesis_with_100_percent_likelihood(self):
        """Test creating hypothesis with 100% likelihood"""
        manager = create_hypothesis_manager()

        h = manager.create_hypothesis(
            statement="Certain cause",
            category="code",
            initial_likelihood=1.0,
            current_turn=1,
        )

        assert h.likelihood == 1.0

    def test_decay_stops_at_minimum(self):
        """Test that confidence decay has a floor"""
        manager = create_hypothesis_manager()

        h = manager.create_hypothesis(
            statement="Low confidence",
            category="code",
            initial_likelihood=0.10,
            current_turn=1,
        )

        h.status = HypothesisStatus.TESTING
        h.iterations_without_progress = 10  # Heavy decay

        # Even with heavy decay, should stay > 0
        decayed = manager.apply_confidence_decay(h, current_turn=11)
        assert decayed.likelihood > 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
