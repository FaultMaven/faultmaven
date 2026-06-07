"""Unit tests for the Investigation Journal feature.

Tests the JournalEntry model, journal context building, journal extraction
in state updates, and persistence round-trip.

Design Reference:
- docs/architecture/investigation-engine/investigation-journal.md (Phase 2)
"""

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    build_investigation_context,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Stabilization,
    InvestigationResponse_Treatment,
    JournalEntryOutput,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    JournalEntry,
)

# ============================================================
# Helpers
# ============================================================


def _make_investigating_case(**overrides) -> Case:
    """Create a minimal INVESTIGATING case."""
    defaults = {
        "case_id": "case_aabb11223344",
        "title": "Test Case",
        "description": "Test description",
        "user_id": "user_123",
        "organization_id": "org_123",
        "state": CaseState.INVESTIGATING,
        "inquiry": InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test description",
        ),
    }
    defaults.update(overrides)
    return Case(**defaults)


def _make_journal_entry(
    turn: int = 1,
    entry_type: str = "finding",
    content: str = "Test finding",
    evidence_id: str | None = None,
    hypothesis_id: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        turn=turn,
        entry_type=entry_type,
        content=content,
        evidence_id=evidence_id,
        hypothesis_id=hypothesis_id,
    )


# ============================================================
# JournalEntry Model
# ============================================================


class TestJournalEntryModel:
    """Test JournalEntry Pydantic model."""

    def test_create_finding(self):
        entry = JournalEntry(
            turn=3,
            entry_type="finding",
            content="142 OOM errors from service-A, 14:02-16:45 UTC",
        )
        assert entry.turn == 3
        assert entry.entry_type == "finding"
        assert entry.evidence_id is None
        assert entry.hypothesis_id is None

    def test_create_with_evidence_id(self):
        entry = JournalEntry(
            turn=5,
            entry_type="finding",
            content="Memory spike correlates with deploy",
            evidence_id="ev_abc123def456",
        )
        assert entry.evidence_id == "ev_abc123def456"

    def test_create_with_hypothesis_id(self):
        entry = JournalEntry(
            turn=7,
            entry_type="ruled_out",
            content="Network hypothesis refuted",
            hypothesis_id="hyp_abc123def456",
        )
        assert entry.hypothesis_id == "hyp_abc123def456"

    def test_all_entry_types(self):
        """All six entry types should be valid."""
        valid_types = [
            "finding",
            "decision",
            "user_context",
            "ruled_out",
            "blocker",
            "milestone",
        ]
        for entry_type in valid_types:
            entry = JournalEntry(
                turn=1, entry_type=entry_type, content=f"Test {entry_type}"
            )
            assert entry.entry_type == entry_type

    def test_content_max_length(self):
        """Content is capped at 200 characters."""
        with pytest.raises(Exception):
            JournalEntry(
                turn=1,
                entry_type="finding",
                content="x" * 201,
            )

    def test_content_at_max_length(self):
        """Content at exactly 200 characters should be valid."""
        entry = JournalEntry(
            turn=1,
            entry_type="finding",
            content="x" * 200,
        )
        assert len(entry.content) == 200

    def test_invalid_entry_type(self):
        """Invalid entry type should raise validation error."""
        with pytest.raises(Exception):
            JournalEntry(
                turn=1,
                entry_type="invalid_type",
                content="Test",
            )

    def test_serialization_roundtrip(self):
        """JournalEntry should survive model_dump/parse cycle."""
        entry = JournalEntry(
            turn=5,
            entry_type="decision",
            content="Focus on ChromaDB pooling",
            evidence_id="ev_abc123def456",
        )
        data = entry.model_dump(mode="json")
        restored = JournalEntry(**data)
        assert restored == entry


# ============================================================
# JournalEntryOutput Schema
# ============================================================


class TestJournalEntryOutput:
    """Test the LLM output schema for journal entries."""

    def test_create_output(self):
        output = JournalEntryOutput(
            entry_type="finding",
            content="142 OOM errors detected",
        )
        assert output.entry_type == "finding"
        assert output.evidence_id is None

    def test_content_max_length_truncates(self):
        """Long content is truncated to 200 chars, not rejected."""
        output = JournalEntryOutput(
            entry_type="finding",
            content="x" * 300,
        )
        assert len(output.content) == 200
        assert output.content.endswith("...")


# ============================================================
# Case Model Integration
# ============================================================


class TestCaseJournalField:
    """Test investigation_journal field on Case model."""

    def test_default_empty(self):
        case = _make_investigating_case()
        assert case.investigation_journal == []

    def test_append_entries(self):
        case = _make_investigating_case()
        entry = _make_journal_entry(turn=1, content="First finding")
        case.investigation_journal.append(entry)
        assert len(case.investigation_journal) == 1

    def test_multiple_entries(self):
        entries = [
            _make_journal_entry(
                turn=1, entry_type="finding", content="OOM errors found"
            ),
            _make_journal_entry(
                turn=3, entry_type="decision", content="Focus on memory"
            ),
            _make_journal_entry(
                turn=5, entry_type="ruled_out", content="Network ruled out"
            ),
        ]
        case = _make_investigating_case(investigation_journal=entries)
        assert len(case.investigation_journal) == 3
        assert case.investigation_journal[0].entry_type == "finding"
        assert case.investigation_journal[2].entry_type == "ruled_out"

    def test_serialization_roundtrip(self):
        """Journal entries survive model_dump/parse cycle within metadata blob."""
        entries = [
            _make_journal_entry(turn=1, content="Finding A"),
            _make_journal_entry(turn=3, entry_type="decision", content="Decision B"),
        ]
        # Serialize journal entries as they would be in the metadata blob
        serialized = [e.model_dump(mode="json") for e in entries]
        restored = [JournalEntry(**d) for d in serialized]
        assert len(restored) == 2
        assert restored[0].content == "Finding A"
        assert restored[1].entry_type == "decision"


# ============================================================
# Context Builder Integration
# ============================================================


class TestJournalContextBuilder:
    """Test journal section in build_investigation_context."""

    def test_empty_journal_produces_empty_string(self):
        case = _make_investigating_case()
        ctx = build_investigation_context(case, "Hello", max_tokens=8000)
        assert ctx["investigation_journal"] == ""

    def test_journal_formatted_as_xml(self):
        entries = [
            _make_journal_entry(turn=3, entry_type="finding", content="142 OOM errors"),
            _make_journal_entry(
                turn=5, entry_type="user_context", content="Deployed v0.4.22"
            ),
        ]
        case = _make_investigating_case(investigation_journal=entries)
        ctx = build_investigation_context(case, "What's next?", max_tokens=8000)
        journal = ctx["investigation_journal"]
        assert "<investigation_journal>" in journal
        assert "</investigation_journal>" in journal
        assert "[T3] FINDING: 142 OOM errors" in journal
        assert "[T5] USER_CONTEXT: Deployed v0.4.22" in journal

    def test_all_entry_types_formatted(self):
        entries = [
            _make_journal_entry(turn=1, entry_type="finding", content="A"),
            _make_journal_entry(turn=2, entry_type="decision", content="B"),
            _make_journal_entry(turn=3, entry_type="user_context", content="C"),
            _make_journal_entry(turn=4, entry_type="ruled_out", content="D"),
            _make_journal_entry(turn=5, entry_type="blocker", content="E"),
            _make_journal_entry(turn=6, entry_type="milestone", content="F"),
        ]
        case = _make_investigating_case(investigation_journal=entries)
        ctx = build_investigation_context(case, "Test", max_tokens=8000)
        journal = ctx["investigation_journal"]
        assert "FINDING" in journal
        assert "DECISION" in journal
        assert "USER_CONTEXT" in journal
        assert "RULED_OUT" in journal
        assert "BLOCKER" in journal
        assert "MILESTONE" in journal

    def test_journal_included_in_template_format(self):
        """Journal content should be present in the formatted prompt."""
        entries = [
            _make_journal_entry(turn=3, entry_type="finding", content="Key discovery"),
        ]
        case = _make_investigating_case(investigation_journal=entries)
        ctx = build_investigation_context(case, "What's next?", max_tokens=8000)
        # The ctx dict has investigation_journal key that gets injected into template
        assert "investigation_journal" in ctx
        assert "Key discovery" in ctx["investigation_journal"]


# ============================================================
# Schema Integration (journal_entries on all investigation schemas)
# ============================================================


class TestSchemaJournalEntries:
    """Test journal_entries field exists on all investigation state_updates."""

    def test_diagnosis_has_journal_entries(self):
        schema = InvestigationResponse_Diagnosis.DiagnosisStateUpdate
        assert "journal_entries" in schema.model_fields

    def test_stabilization_has_journal_entries(self):
        schema = InvestigationResponse_Stabilization.StabilizationStateUpdate
        assert "journal_entries" in schema.model_fields

    def test_treatment_has_journal_entries(self):
        schema = InvestigationResponse_Treatment.TreatmentStateUpdate
        assert "journal_entries" in schema.model_fields

    def test_general_has_journal_entries(self):
        schema = InvestigationResponse_General.GeneralStateUpdate
        assert "journal_entries" in schema.model_fields

    def test_journal_entries_defaults_to_empty_list(self):
        """journal_entries should default to [] (consistent with other list fields)."""
        from faultmaven.core.investigation.schemas import TurnOutcome

        state = InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            outcome=TurnOutcome.MILESTONE_COMPLETED,
        )
        assert state.journal_entries == []

    def test_journal_entries_accepts_list(self):
        from faultmaven.core.investigation.schemas import TurnOutcome

        entries = [
            JournalEntryOutput(entry_type="finding", content="OOM errors found"),
        ]
        state = InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            outcome=TurnOutcome.MILESTONE_COMPLETED,
            journal_entries=entries,
        )
        assert len(state.journal_entries) == 1
        assert state.journal_entries[0].entry_type == "finding"

    def test_journal_entries_null_becomes_none(self):
        """LLM returning null for journal_entries is accepted (Optional allows None)."""
        from faultmaven.core.investigation.schemas import TurnOutcome

        state = InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            outcome=TurnOutcome.MILESTONE_COMPLETED,
            journal_entries=None,
        )
        # Optional[List[T]] accepts None explicitly
        assert state.journal_entries is None


# Persistence-resilience tests previously verified
# `DatabaseCaseRepository._parse_investigation_journal` — a defensive
# parser that lived in the now-deleted parallel repository hierarchy.
# investigation_journal is currently an in-memory-only field on Case
# (no DB column, no read/write through any repository), so there is no
# persistence layer to be defensive about. If journal persistence is
# ever added back, port the resilience tests to the new home then.
