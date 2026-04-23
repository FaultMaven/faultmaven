"""Phase 1 — Context-builder surfaces classifier confidence to the agent.

Pins the following end-to-end behaviour:

- Evidence with a low-confidence `classification` metadata block emits
  ``<evidence ... confidence="low">`` in the prompt when the feature
  flag is ON; the XML attribute is absent when the flag is OFF.
- The low-confidence evidence's ``<structural_index>`` block gets an
  inline advisory line reinforcing the attribute.
- Evidence with high confidence, missing classification, or absent
  metadata entirely is unchanged — no marker appears.

These tests would have failed against the pre-Phase-1 context builder,
which ignored ``evidence.metadata`` entirely.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_context,
)
from faultmaven.modules.case.contracts import (
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.models import Case

_EV_COUNTER = 0


def _next_ev_id() -> str:
    global _EV_COUNTER
    _EV_COUNTER += 1
    return f"ev_{_EV_COUNTER:012x}"


def _make_evidence(metadata=None, preprocessed_content="Structural index content"):
    return Evidence(
        evidence_id=_next_ev_id(),
        form=EvidenceForm.DOCUMENT,
        summary="Test summary",
        preprocessed_content=preprocessed_content,
        data_type="LOGS",
        content_size_bytes=5000,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        collected_at=datetime.now(UTC),
        collected_by="user_123",
        collected_at_turn=1,
        primary_purpose="Test",
        preprocessing_method="crime_scene",
        metadata=metadata,
    )


def _make_case(evidence_list):
    return Case(
        case_id="case_aabb11223344",
        title="Test Case",
        description="Test description",
        user_id="user_123",
        organization_id="org_123",
        status=CaseStatus.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test description",
        ),
        evidence=evidence_list,
    )


def _enable_flag(value: bool):
    """Patch the feature flag via the settings accessor used by the
    context builder. We patch the settings module's ``get_settings`` to
    return a stub whose ``preprocessing.confidence_marker_enabled`` has
    the requested value."""

    class _Prep:
        confidence_marker_enabled = value

    class _Settings:
        preprocessing = _Prep()

    return patch(
        "faultmaven.config.settings.get_settings",
        return_value=_Settings(),
    )


# ---------------------------------------------------------------------------
# Marker emission
# ---------------------------------------------------------------------------


class TestLowConfidenceMarker:
    def test_low_confidence_emits_attribute_when_flag_on(self):
        ev = _make_evidence(
            metadata={
                "classification": {
                    "confidence": 0.42,
                    "source": "rule_based",
                    "failed": False,
                    "suggested_types": [],
                }
            }
        )
        case = _make_case([ev])

        with _enable_flag(True):
            out = _build_evidence_context(case)

        assert 'confidence="low"' in out
        # Advisory sentence must appear inside the structural_index block.
        assert "Classifier confidence: 0.42" in out
        assert "tentative" in out

    def test_low_confidence_silent_when_flag_off(self):
        """Ship-dark default: flag OFF means no behaviour change."""
        ev = _make_evidence(
            metadata={
                "classification": {
                    "confidence": 0.42,
                    "source": "rule_based",
                    "failed": False,
                    "suggested_types": [],
                }
            }
        )
        case = _make_case([ev])

        with _enable_flag(False):
            out = _build_evidence_context(case)

        assert 'confidence="low"' not in out
        assert "tentative" not in out

    def test_high_confidence_no_marker(self):
        ev = _make_evidence(
            metadata={
                "classification": {
                    "confidence": 0.92,
                    "source": "rule_based",
                    "failed": False,
                    "suggested_types": [],
                }
            }
        )
        case = _make_case([ev])

        with _enable_flag(True):
            out = _build_evidence_context(case)

        assert 'confidence="low"' not in out

    def test_evidence_without_metadata_no_marker(self):
        """Existing evidence predating Phase 1 has metadata=None. The
        context builder must tolerate this without raising or emitting
        a spurious marker."""
        ev = _make_evidence(metadata=None)
        case = _make_case([ev])

        with _enable_flag(True):
            out = _build_evidence_context(case)

        assert 'confidence="low"' not in out
        # Evidence block still rendered.
        assert f'id="{ev.evidence_id}"' in out

    def test_metadata_without_classification_key_no_marker(self):
        """Forward-compat: evidence that carries only Phase 4 entity
        overflow markers (no Phase 1 classification block) must not
        accidentally get a low-confidence marker."""
        ev = _make_evidence(metadata={"entities": {"overflow_types": ["ip"]}})
        case = _make_case([ev])

        with _enable_flag(True):
            out = _build_evidence_context(case)

        assert 'confidence="low"' not in out

    def test_corrupt_metadata_does_not_raise(self):
        """Defensive: a metadata blob with an unexpected shape (e.g. the
        classification block is a string rather than a dict) must not
        crash the context builder. The marker is suppressed instead."""
        ev = _make_evidence(metadata={"classification": "not a dict"})
        case = _make_case([ev])

        with _enable_flag(True):
            out = _build_evidence_context(case)

        assert 'confidence="low"' not in out
        assert f'id="{ev.evidence_id}"' in out


# ---------------------------------------------------------------------------
# Tier B path (summary-only evidence also gets the attribute)
# ---------------------------------------------------------------------------


class TestLowConfidenceMarkerTierB:
    def test_tier_b_summary_only_still_carries_attribute(self):
        """Older evidence is rendered in Tier B (summary only). The
        confidence marker must still appear on the tag so the agent
        knows to be careful even when it only sees the summary."""
        # Five DOCUMENT evidence items; tier A takes the first 3 by
        # score, leaving the remaining two in tier B.
        items = []
        for i in range(5):
            items.append(
                _make_evidence(
                    metadata={
                        "classification": {
                            "confidence": 0.3,
                            "source": "rule_based",
                            "failed": False,
                            "suggested_types": [],
                        }
                    }
                )
            )
        case = _make_case(items)

        with _enable_flag(True):
            out = _build_evidence_context(case)

        # Count markers in all evidence tags.
        assert out.count('confidence="low"') == 5
