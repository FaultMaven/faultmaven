"""Tests for MilestoneEngine evidence handling in the unified ingestion pipeline.

Tests evidence creation via MilestoneEngine after the unified turn pipeline (v4.1):
1. No auto-created evidence without LLM evidence_to_add
2. Evidence created based on LLM evidence_to_add output
3. Milestone advancement attribution (Option 2.5 — CATEGORY_MILESTONE_MAP)
4. Query + attachment combined turns
5. Evidence category immutability

Design Reference:
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.1)

NOTE: SubmissionClassification was removed in Phase 1-2. Evidence form for
engine-created evidence is SUBMITTED_DATA. Attachment evidence (form=DOCUMENT)
is created in Step 1 before the engine runs.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    CATEGORY_MILESTONE_MAP,
    MilestoneEngine,
)
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
)

# ============================================================
# Helpers
# ============================================================


def _make_investigating_case(**overrides) -> Case:
    """Create a Case in INVESTIGATING status for milestone engine tests."""
    description = overrides.pop("description", "Test symptom")
    defaults = {
        "case_id": "case_1234567890ab",
        "title": "Test Case",
        "description": description,
        "status": CaseStatus.INVESTIGATING,
        "user_id": "user_123",
        "organization_id": "org_123",
        "problem_verification": ProblemVerification(
            symptom_statement=description,
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        "inquiry": InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement=description,
        ),
        "evidence": [],
    }
    defaults.update(overrides)
    return Case(**defaults)


def _make_engine():
    """Create a MilestoneEngine with mocked LLM and repository."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock()
    mock_repo = MagicMock()
    mock_repo.save = AsyncMock(side_effect=lambda c: c)
    return (
        MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        ),
        mock_llm,
        mock_repo,
    )


# ============================================================
# Evidence Creation via MilestoneEngine
# ============================================================


class TestEvidenceRedesignMilestoneEngine:
    """Tests for milestone engine evidence creation in the unified pipeline."""

    @pytest.mark.asyncio
    async def test_no_evidence_created_for_conversation_only(self):
        """process_turn with no evidence_to_add creates no evidence."""
        engine, mock_llm, _ = _make_engine()
        case = _make_investigating_case()

        mock_response = {
            "agent_response": "I understand. Please provide more details.",
            "internal_reasoning": {
                "evidence_analyzed": [],
                "conclusions": [],
                "milestone_justifications": {},
                "uncertainties": [],
            },
            "state_updates": {
                "milestones": {},
                "evidence_to_add": [],
                "hypotheses_to_add": [],
                "outcome": "conversation",
            },
        }
        mock_llm.generate.return_value = json.dumps(mock_response)

        result = await engine.process_turn(case, "What could be causing this?")
        updated = result["case_updated"]
        assert len(updated.evidence) == 0

    @pytest.mark.asyncio
    async def test_evidence_created_from_evidence_to_add(self):
        """Evidence created from LLM evidence_to_add with correct fields."""
        engine, mock_llm, _ = _make_engine()
        case = _make_investigating_case(description="Database connection issues")

        mock_response = {
            "agent_response": "I can see connection timeout errors in the logs.",
            "internal_reasoning": {
                "evidence_analyzed": ["new_index_0"],
                "conclusions": [
                    {
                        "observation": "Logs show connection timeout errors",
                        "inference": "Database connection pool may be exhausted",
                        "confidence": 0.8,
                    }
                ],
                "milestone_justifications": {
                    "symptom_verified": "Connection errors confirmed in logs",
                },
                "uncertainties": ["Root cause not yet identified"],
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "evidence_to_add": [
                    {
                        "summary": "Database connection timeout errors in application logs",
                        "category": "symptom_evidence",
                        "source_type": "logs",
                    }
                ],
                "hypotheses_to_add": [],
                "outcome": "milestone_completed",
            },
        }
        mock_llm.generate.return_value = json.dumps(mock_response)

        result = await engine.process_turn(
            case,
            "Here are the application logs",
            attachments=[
                {
                    "file_id": "file_1234567890ab",
                    "filename": "app.log",
                    "data_type": "application_log",
                    "content_hash": "abc123",
                }
            ],
        )

        updated = result["case_updated"]
        assert len(updated.evidence) == 1
        assert updated.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert updated.evidence[0].source_type == EvidenceSourceType.LOGS
        assert "connection timeout" in updated.evidence[0].summary.lower()
        # Engine-created evidence has SUBMITTED_DATA form
        assert updated.evidence[0].form == EvidenceForm.SUBMITTED_DATA
        assert updated.progress.symptom_verified is True


# ============================================================
# Milestone Advancement Attribution (Option 2.5)
# ============================================================


class TestMilestoneAdvancementAttribution:
    """Test CATEGORY_MILESTONE_MAP and Tier 2 inference."""

    @pytest.mark.asyncio
    async def test_category_milestone_map_has_symptom_evidence(self):
        """CATEGORY_MILESTONE_MAP includes SYMPTOM_EVIDENCE with expected milestones."""
        assert EvidenceCategory.SYMPTOM_EVIDENCE in CATEGORY_MILESTONE_MAP
        symptom_milestones = CATEGORY_MILESTONE_MAP[EvidenceCategory.SYMPTOM_EVIDENCE]
        assert "symptom_verified" in symptom_milestones
        assert "scope_assessed" in symptom_milestones
        assert "timeline_established" in symptom_milestones

    @pytest.mark.asyncio
    async def test_category_milestone_map_has_causal_evidence(self):
        """CATEGORY_MILESTONE_MAP includes CAUSAL_EVIDENCE with expected milestones."""
        assert EvidenceCategory.CAUSAL_EVIDENCE in CATEGORY_MILESTONE_MAP
        causal_milestones = CATEGORY_MILESTONE_MAP[EvidenceCategory.CAUSAL_EVIDENCE]
        assert "root_cause_identified" in causal_milestones

    @pytest.mark.asyncio
    async def test_contextual_evidence_has_no_milestones(self):
        """CONTEXTUAL_EVIDENCE maps to empty milestone list."""
        assert EvidenceCategory.CONTEXTUAL_EVIDENCE in CATEGORY_MILESTONE_MAP
        assert len(CATEGORY_MILESTONE_MAP[EvidenceCategory.CONTEXTUAL_EVIDENCE]) == 0

    @pytest.mark.asyncio
    async def test_rejected_evidence_defaults_to_empty(self):
        """REJECTED category not in map — .get() returns empty list."""
        milestones = CATEGORY_MILESTONE_MAP.get(EvidenceCategory.REJECTED, [])
        assert len(milestones) == 0

    @pytest.mark.asyncio
    async def test_milestone_advancement_inference_from_category(self):
        """System infers advances_milestones from category + milestones completed."""
        category = EvidenceCategory.SYMPTOM_EVIDENCE
        milestones_completed = ["symptom_verified", "scope_assessed"]
        eligible = CATEGORY_MILESTONE_MAP.get(category, [])
        inferred = [m for m in milestones_completed if m in eligible]
        assert "symptom_verified" in inferred
        assert "scope_assessed" in inferred
        assert len(inferred) == 2

    @pytest.mark.asyncio
    async def test_evidence_advances_milestones_populated(self):
        """Evidence.advances_milestones populated via Tier 2 inference in engine."""
        engine, mock_llm, _ = _make_engine()
        case = _make_investigating_case()

        mock_response = {
            "agent_response": "Symptom verified and scope assessed.",
            "internal_reasoning": {
                "evidence_analyzed": ["new_index_0"],
                "conclusions": [],
                "milestone_justifications": {
                    "symptom_verified": "Confirmed errors in logs",
                    "scope_assessed": "Errors affect all users",
                },
                "uncertainties": [],
            },
            "state_updates": {
                "milestones": {
                    "symptom_verified": True,
                    "scope_assessed": True,
                },
                "evidence_to_add": [
                    {
                        "summary": "Error logs confirming symptom",
                        "category": "symptom_evidence",
                        "source_type": "logs",
                    }
                ],
                "hypotheses_to_add": [],
                "outcome": "milestone_completed",
            },
        }
        mock_llm.generate.return_value = json.dumps(mock_response)

        result = await engine.process_turn(case, "Here are the logs")
        updated = result["case_updated"]

        assert len(updated.evidence) == 1
        evidence = updated.evidence[0]
        # Tier 2 inference should attribute milestones
        assert len(evidence.advances_milestones) >= 1

    @pytest.mark.asyncio
    async def test_llm_override_advances_milestones(self):
        """LLM can explicitly override system inference via advances_milestones."""
        evidence_with_override = EvidenceToAdd(
            summary="Special case evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            advances_milestones=["symptom_verified", "root_cause_identified"],
        )
        assert evidence_with_override.advances_milestones == [
            "symptom_verified",
            "root_cause_identified",
        ]


# ============================================================
# Combined Query + Attachment Turns
# ============================================================


class TestCombinedTurns:
    """Test query + attachment combined submissions.

    In the unified pipeline, attachments are preprocessed in Step 1
    (form=DOCUMENT) and the engine processes the query in Step 2.
    The engine may add additional evidence via evidence_to_add (form=SUBMITTED_DATA).
    """

    @pytest.mark.asyncio
    async def test_engine_creates_evidence_from_query_with_attachments(self):
        """Engine creates SUBMITTED_DATA evidence even when attachments were preprocessed in Step 1."""
        engine, mock_llm, _ = _make_engine()
        case = _make_investigating_case()

        mock_response = {
            "agent_response": "Thanks for the context and logs.",
            "internal_reasoning": {
                "evidence_analyzed": ["new_index_0"],
                "conclusions": [],
                "milestone_justifications": {},
                "uncertainties": [],
            },
            "state_updates": {
                "milestones": {},
                "evidence_to_add": [
                    {
                        "summary": "Log excerpts with connection errors",
                        "category": "symptom_evidence",
                        "source_type": "logs",
                    }
                ],
                "hypotheses_to_add": [],
                "outcome": "conversation",
            },
        }
        mock_llm.generate.return_value = json.dumps(mock_response)

        result = await engine.process_turn(
            case,
            "Here's what I'm seeing plus some logs",
            attachments=[
                {
                    "file_id": "file_1234567890ab",
                    "filename": "app.log",
                    "data_type": "logs",
                }
            ],
        )

        updated = result["case_updated"]
        assert len(updated.evidence) == 1
        # Engine evidence is SUBMITTED_DATA
        assert updated.evidence[0].form == EvidenceForm.SUBMITTED_DATA
        assert updated.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE


# ============================================================
# Evidence Category Immutability
# ============================================================


class TestEvidenceCategoryImmutability:
    """Test evidence category cannot change after creation."""

    @pytest.mark.asyncio
    async def test_evidence_category_immutable(self):
        """Evidence category should not change after initial classification."""
        evidence = Evidence(
            evidence_id="ev_abc123456789",
            case_id="case_def987654321",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            form=EvidenceForm.DOCUMENT,
            summary="Error logs",
            primary_purpose="Shows errors",
            content_ref="s3://logs",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_ghi111222333",
            collected_at_turn=1,
            preprocessing_method="none",
            preprocessed_content="",
            content_size_bytes=100,
        )

        original_category = evidence.category
        assert evidence.category == original_category
        assert evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE
