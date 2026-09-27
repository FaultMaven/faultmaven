from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

# ============================================================
# Special State Models (Section 11)
# ============================================================


class EscalationType(str, Enum):
    """Reason for escalation"""

    EXPERTISE_REQUIRED = "expertise_required"
    """
    Requires specialized domain expertise.
    Beyond agent knowledge.
    """

    PERMISSIONS_REQUIRED = "permissions_required"
    """
    User lacks permissions for needed actions.
    Requires higher privileges.
    """

    NO_PROGRESS = "no_progress"
    """
    Investigation is stuck despite best efforts.
    Human insight needed.
    """

    USER_REQUEST = "user_request"
    """
    User explicitly requested escalation.
    """

    CRITICAL_SEVERITY = "critical_severity"
    """
    Problem too critical for agent-only investigation.
    Human oversight required.
    """

    OTHER = "other"
    """
    Does not fit standard escalation reasons.
    """


class EscalationState(BaseModel):
    """
    Investigation escalated to human expert.
    Tracks escalation lifecycle.
    """

    escalation_type: EscalationType = Field(description="Why escalation was needed")

    reason: str = Field(
        description="Detailed explanation of escalation reason", max_length=1000
    )

    escalated_to: Optional[str] = Field(
        default=None, description="Team or person escalated to", max_length=200
    )

    escalated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When escalation occurred",
    )

    # ============================================================
    # Context Transfer
    # ============================================================
    context_summary: str = Field(
        description="Summary of investigation so far for escalation recipient",
        max_length=5000,
    )

    key_findings: List[str] = Field(
        default_factory=list, description="Key findings to communicate to expert"
    )

    # ============================================================
    # Resolution
    # ============================================================
    resolution: Optional[str] = Field(
        default=None, description="How escalation was resolved", max_length=2000
    )

    resolved_at: Optional[datetime] = Field(
        default=None, description="When escalation was resolved"
    )

    @property
    def is_active(self) -> bool:
        """Check if escalation is still active"""
        return self.resolved_at is None


# ============================================================
# Documentation Models (Section 12)
# ============================================================


class DocumentType(str, Enum):
    """Type of generated document."""

    RUNBOOK = "runbook"
    """Runbook entry for future reference"""

    CHAT_SUMMARY = "chat_summary"
    """Summary of investigation conversation"""

    TIMELINE = "timeline"
    """Timeline visualization of events"""

    EVIDENCE_BUNDLE = "evidence_bundle"
    """Compiled evidence package"""

    OTHER = "other"
    """Does not fit standard document types"""


class GeneratedDocument(BaseModel):
    """
    A generated document artifact.
    """

    document_id: str = Field(
        default_factory=lambda: f"doc_{uuid4().hex[:12]}",
        description="Unique document identifier",
    )

    document_type: DocumentType = Field(description="Type of document")

    title: str = Field(description="Document title", min_length=1, max_length=200)

    content_ref: str = Field(
        description="Reference to document content (S3 URI, file path, etc.)",
        max_length=1000,
    )

    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When document was generated",
    )

    format: str = Field(
        description="Document format: markdown | pdf | html | json | other",
        max_length=50,
    )

    size_bytes: Optional[int] = Field(
        default=None, ge=0, description="Document size in bytes"
    )

    @field_validator("format")
    @classmethod
    def valid_format(cls, v):
        """
        Validate format.
        """
        allowed = ["markdown", "pdf", "html", "json", "txt", "other"]
        if v not in allowed:
            raise ValueError(f"format must be one of: {allowed}")
        return v


class DocumentationData(BaseModel):
    """
    Documentation generated when case closes.
    Captures lessons learned and artifacts.
    """

    documents_generated: List[GeneratedDocument] = Field(
        default_factory=list, description="All documents generated for this case"
    )

    runbook_entry: Optional[str] = Field(
        default=None,
        description="Runbook entry created from this case",
        max_length=5000,
    )

    # ============================================================
    # Lessons Learned
    # ============================================================
    lessons_learned: List[str] = Field(
        default_factory=list, description="Key takeaways from investigation"
    )

    what_went_well: List[str] = Field(
        default_factory=list, description="Positive aspects of investigation"
    )

    what_could_improve: List[str] = Field(
        default_factory=list, description="Areas for improvement"
    )

    # ============================================================
    # Prevention
    # ============================================================
    preventive_measures: List[str] = Field(
        default_factory=list, description="How to prevent recurrence"
    )

    monitoring_recommendations: List[str] = Field(
        default_factory=list, description="Monitoring/alerts to add"
    )

    # ============================================================
    # Metadata
    # ============================================================
    generated_at: Optional[datetime] = Field(
        default=None, description="When documentation was generated"
    )

    generated_by: str = Field(
        default="agent", description="Who generated: 'agent' or user_id"
    )


# ============================================================
# Investigation Journal (Durable Long-Term Memory)
# ============================================================


class JournalEntry(BaseModel):
    """A single entry in the investigation journal.

    Captures a distilled insight, decision, or context that the agent
    needs to remember across the entire investigation. Entries are
    append-only and always included in the LLM context.
    """

    turn: int = Field(description="Turn number when this entry was created")

    entry_type: Literal[
        "finding", "decision", "user_context", "ruled_out", "blocker", "milestone"
    ] = Field(description="Type of journal entry")

    content: str = Field(
        description="The distilled insight (max 200 chars)",
        max_length=200,
    )

    evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID this entry relates to, if any",
    )

    hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID this entry relates to, if any",
    )
