"""Unit tests for HypothesisEvidenceLink datetime serialization.

Regression tests for the datetime serialization bug where model_dump() was used
instead of model_dump(mode='json'), causing JSON serialization failures when
HypothesisEvidenceLink objects with analyzed_at datetime fields were persisted.

These tests ensure that:
1. HypothesisEvidenceLink.model_dump(mode='json') produces JSON-serializable output
2. Datetime fields are converted to ISO format strings
3. Round-trip serialization (serialize → JSON → deserialize) preserves data
4. The bug would be caught early in unit tests, not just in integration tests
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.case.domain.models import (
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisStatus,
)


class TestHypothesisEvidenceLinkSerialization:
    """Test HypothesisEvidenceLink serialization with datetime fields."""

    def test_model_dump_json_mode_produces_serializable_output(self):
        """Test that model_dump(mode='json') produces JSON-serializable output.

        This is the core fix - model_dump(mode='json') converts datetime to ISO strings,
        while model_dump() leaves them as datetime objects that json.dumps() can't handle.
        """
        now = datetime.now(timezone.utc)

        link = HypothesisEvidenceLink(
            hypothesis_id="hyp_test123456",
            evidence_id="evidence_001",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Test reasoning",
            stance_confidence=0.9,
            analyzed_at=now,
        )

        # This should produce a dict with ISO string for analyzed_at
        dumped = link.model_dump(mode="json")

        # Verify it's JSON serializable (this would fail with model_dump())
        json_str = json.dumps(dumped)
        assert json_str is not None

        # Verify analyzed_at was converted to string
        assert isinstance(dumped["analyzed_at"], str)
        # Pydantic uses 'Z' suffix for UTC instead of '+00:00'
        assert dumped["analyzed_at"] in [
            now.isoformat(),
            now.isoformat().replace("+00:00", "Z"),
        ]

    def test_model_dump_without_json_mode_fails_json_serialization(self):
        """Test that model_dump() (without mode='json') produces non-serializable output.

        This demonstrates the original bug - model_dump() returns datetime objects
        that json.dumps() cannot serialize.
        """
        now = datetime.now(timezone.utc)

        link = HypothesisEvidenceLink(
            hypothesis_id="hyp_test123456",
            evidence_id="evidence_001",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Test reasoning",
            stance_confidence=0.9,
            analyzed_at=now,
        )

        # model_dump() without mode='json' returns datetime objects
        dumped = link.model_dump()

        # Verify analyzed_at is still a datetime object
        assert isinstance(dumped["analyzed_at"], datetime)

        # This should fail with: TypeError: Object of type datetime is not JSON serializable
        with pytest.raises(
            TypeError, match="Object of type datetime is not JSON serializable"
        ):
            json.dumps(dumped)

    def test_round_trip_serialization_preserves_data(self):
        """Test that serialize → JSON → deserialize preserves all data correctly."""
        now = datetime.now(timezone.utc)

        original_link = HypothesisEvidenceLink(
            hypothesis_id="hyp_test123456",
            evidence_id="evidence_001",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Log shows database connection pool exhausted",
            stance_confidence=0.85,
            analyzed_at=now,
        )

        # Serialize with mode='json' (the fix)
        dumped = original_link.model_dump(mode="json")

        # Convert to JSON string (simulates database storage)
        json_str = json.dumps(dumped)

        # Parse back to dict
        parsed = json.loads(json_str)

        # Deserialize back to model
        restored_link = HypothesisEvidenceLink(**parsed)

        # Verify all fields preserved
        assert restored_link.hypothesis_id == original_link.hypothesis_id
        assert restored_link.evidence_id == original_link.evidence_id
        assert restored_link.stance == original_link.stance
        assert restored_link.reasoning == original_link.reasoning
        assert restored_link.stance_confidence == original_link.stance_confidence

        # Verify datetime was preserved (allowing small margin for parsing)
        assert isinstance(restored_link.analyzed_at, datetime)
        time_diff = abs(
            (restored_link.analyzed_at - original_link.analyzed_at).total_seconds()
        )
        assert time_diff < 1  # Less than 1 second difference

    def test_multiple_evidence_links_serialization(self):
        """Test serializing multiple evidence links (as stored in hypothesis.evidence_links dict).

        This simulates the exact pattern used in the repository code that was failing.
        """
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(hours=2)

        # Create multiple evidence links with different timestamps
        evidence_links = {
            "evidence_001": HypothesisEvidenceLink(
                hypothesis_id="hyp_test123456",
                evidence_id="evidence_001",
                stance=EvidenceStance.SUPPORTS,
                reasoning="First piece of evidence",
                stance_confidence=0.9,
                analyzed_at=now,
            ),
            "evidence_002": HypothesisEvidenceLink(
                hypothesis_id="hyp_test123456",
                evidence_id="evidence_002",
                stance=EvidenceStance.REFUTES,
                reasoning="Second piece of evidence",
                stance_confidence=0.7,
                analyzed_at=earlier,
            ),
            "evidence_003": HypothesisEvidenceLink(
                hypothesis_id="hyp_test123456",
                evidence_id="evidence_003",
                stance=EvidenceStance.NEUTRAL,
                reasoning="Third piece of evidence",
                stance_confidence=0.5,
                analyzed_at=now - timedelta(minutes=30),
            ),
        }

        # This is the exact pattern from the repository code (fixed version)
        serialized_dict = {
            eid: link.model_dump(mode="json") for eid, link in evidence_links.items()
        }

        # This should work without raising TypeError
        json_str = json.dumps(serialized_dict)
        assert json_str is not None

        # Verify we can deserialize back
        parsed = json.loads(json_str)
        assert len(parsed) == 3
        assert "evidence_001" in parsed
        assert "evidence_002" in parsed
        assert "evidence_003" in parsed

        # Verify datetimes were converted to strings
        for eid, link_data in parsed.items():
            assert isinstance(link_data["analyzed_at"], str)

    def test_hypothesis_with_evidence_links_serialization(self):
        """Hypothesis with evidence_links serializes cleanly via mode='json'.

        Regression for the original datetime-in-dict-keys bug. Note: since
        the schema redesign (commit 7b5a1b93), evidence_links is a list of
        HypothesisEvidenceLink rows backing the hypothesis_evidence
        junction table, not a dict keyed by evidence_id.
        """
        now = datetime.now(timezone.utc)

        hypothesis = Hypothesis(
            hypothesis_id="hyp_0123456789ab",
            statement="Database connection pool exhaustion",
            category=HypothesisCategory.DATABASE,
            status=HypothesisStatus.ACTIVE,
            likelihood=0.8,
            initial_likelihood=0.5,
            evidence_links=[
                HypothesisEvidenceLink(
                    hypothesis_id="hyp_0123456789ab",
                    evidence_id="evidence_001",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="Pool metrics show exhaustion",
                    stance_confidence=0.95,
                    analyzed_at=now,
                ),
                HypothesisEvidenceLink(
                    hypothesis_id="hyp_0123456789ab",
                    evidence_id="evidence_002",
                    stance=EvidenceStance.NEUTRAL,
                    reasoning="Inconclusive network metrics",
                    stance_confidence=0.5,
                    analyzed_at=now - timedelta(minutes=5),
                ),
            ],
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="High correlation between pool usage and errors",
        )

        # Serialize evidence_links (the list shape persisted to the
        # hypothesis_evidence junction table).
        evidence_links_json = json.dumps(
            [link.model_dump(mode="json") for link in hypothesis.evidence_links]
        )

        # Should not raise TypeError
        assert evidence_links_json is not None

        # Verify we can parse it back
        parsed_links = json.loads(evidence_links_json)
        assert len(parsed_links) == 2
        assert {link["evidence_id"] for link in parsed_links} == {
            "evidence_001",
            "evidence_002",
        }

    def test_edge_case_empty_evidence_links(self):
        """Empty evidence_links list serializes to '[]'."""
        hypothesis = Hypothesis(
            hypothesis_id="hyp_0123456789ab",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            status=HypothesisStatus.CAPTURED,
            likelihood=0.5,
            initial_likelihood=0.5,
            evidence_links=[],
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="Test rationale",
        )

        evidence_links_json = json.dumps(
            [link.model_dump(mode="json") for link in hypothesis.evidence_links]
        )

        assert evidence_links_json == "[]"

    def test_analyzed_at_default_factory(self):
        """Test that analyzed_at gets a default UTC datetime if not provided."""
        before = datetime.now(timezone.utc)

        link = HypothesisEvidenceLink(
            hypothesis_id="hyp_test123456",
            evidence_id="evidence_001",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Test",
            stance_confidence=0.8,
            # analyzed_at not provided - should use default_factory
        )

        after = datetime.now(timezone.utc)

        # Verify default was set
        assert isinstance(link.analyzed_at, datetime)
        assert link.analyzed_at.tzinfo == timezone.utc
        assert before <= link.analyzed_at <= after

        # Verify it can still be serialized
        dumped = link.model_dump(mode="json")
        json_str = json.dumps(dumped)
        assert json_str is not None
