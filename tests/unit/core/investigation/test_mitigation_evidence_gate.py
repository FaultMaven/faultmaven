"""Tests for the MITIGATION → DIAGNOSTIC evidence gate.

Pins Behavioral Rule 2 (Evidence-Grounded) applied to MITIGATION
ProposedActions: a mitigation must target an observed failure
(SYMPTOM_EVIDENCE), not an unverified user claim. When the LLM emits
a MITIGATION SolutionToAdd on a case with no SYMPTOM_EVIDENCE, the
engine downgrades the resulting ProposedAction to DIAGNOSTIC and
attaches a downgrade_reason that surfaces to the LLM next turn so it
can recover.

Symmetric to the pre-existing SOLUTION → DIAGNOSTIC hypothesis gate at
the same call site in `_apply_investigation_updates`.

Design reference: docs/architecture/investigation-engine/investigation-lifecycle-logic.md
§2.3 (minimum-evidence discipline) and
docs/architecture/investigation-engine/agent-behavioral-rules.md
(Rule 2: Evidence-Grounded).
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultmaven.core.investigation.milestone_engine import (
    _case_has_symptom_evidence,
)
from faultmaven.modules.case.domain.models import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationActionType,
    ProposedAction,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_case() -> Case:
    """Bare-bones case for evidence-gate testing. No path_selection or
    inquiry state needed — the gate is over `case.evidence` only."""
    return Case(
        user_id="u1",
        organization_id="o1",
        title="Test",
        description="Pods are crashing",
    )


def _make_evidence(category: EvidenceCategory, idx: int = 1) -> Evidence:
    """Build a minimal Evidence row in the requested category."""
    return Evidence(
        evidence_id=f"ev_{idx:012d}",
        summary=f"Test evidence {idx}",
        content_ref=f"test_{idx}.log",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(timezone.utc),
        collected_by="user_test",
        primary_purpose="Testing gate",
        preprocessed_content="content",
        content_size_bytes=100,
        preprocessing_method="manual",
        source_file_id=None,
        collected_at_turn=1,
    )


# ---------------------------------------------------------------------------
# Helper: _case_has_symptom_evidence
# ---------------------------------------------------------------------------


class TestCaseHasSymptomEvidence:
    """The gate predicate is pure over `case.evidence`. It looks only at
    category, not at source, attribution, or any other field."""

    def test_empty_evidence_returns_false(self):
        case = _make_case()
        assert case.evidence == []
        assert _case_has_symptom_evidence(case) is False

    def test_no_symptom_category_returns_false(self):
        """Evidence exists but none is SYMPTOM_EVIDENCE — gate fires."""
        case = _make_case()
        case.evidence.append(_make_evidence(EvidenceCategory.CAUSAL_EVIDENCE, idx=1))
        case.evidence.append(
            _make_evidence(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, idx=2)
        )
        assert _case_has_symptom_evidence(case) is False

    def test_one_symptom_row_returns_true(self):
        case = _make_case()
        case.evidence.append(_make_evidence(EvidenceCategory.SYMPTOM_EVIDENCE, idx=1))
        assert _case_has_symptom_evidence(case) is True

    def test_symptom_mixed_with_other_returns_true(self):
        """At least one symptom row is sufficient — other categories don't
        change the verdict."""
        case = _make_case()
        case.evidence.append(_make_evidence(EvidenceCategory.SYMPTOM_EVIDENCE, idx=1))
        case.evidence.append(_make_evidence(EvidenceCategory.CAUSAL_EVIDENCE, idx=2))
        assert _case_has_symptom_evidence(case) is True


# ---------------------------------------------------------------------------
# ProposedAction.downgrade_reason field
# ---------------------------------------------------------------------------


class TestProposedActionDowngradeReason:
    """The downgrade_reason field on ProposedAction carries the engine's
    explanation when action_type was rewritten from the LLM's intent.
    Surfaced to the LLM via context_builder so the agent can recover."""

    def test_field_defaults_to_none(self):
        action = ProposedAction(
            case_id="case_1",
            action_type=InvestigationActionType.MITIGATION,
            description="Roll back the deployment",
            proposed_in_turn=2,
        )
        assert action.downgrade_reason is None

    def test_field_accepts_explanation_string(self):
        action = ProposedAction(
            case_id="case_1",
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Roll back the deployment",
            proposed_in_turn=2,
            downgrade_reason=(
                "Your previous MITIGATION proposal was downgraded to "
                "DIAGNOSTIC because no SYMPTOM_EVIDENCE existed on the case."
            ),
        )
        assert action.downgrade_reason is not None
        assert "DIAGNOSTIC" in action.downgrade_reason
        assert "SYMPTOM_EVIDENCE" in action.downgrade_reason

    def test_field_round_trips_through_json(self):
        """JSON-blob persistence path: ProposedAction is serialized to JSON
        as part of the case metadata blob, then rehydrated. The new field
        must survive the round trip."""
        original = ProposedAction(
            case_id="case_1",
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Roll back the deployment",
            proposed_in_turn=2,
            downgrade_reason="explanation text",
        )
        as_json = original.model_dump(mode="json")
        rehydrated = ProposedAction(**as_json)
        assert rehydrated.downgrade_reason == "explanation text"
        assert rehydrated.action_type == InvestigationActionType.DIAGNOSTIC
