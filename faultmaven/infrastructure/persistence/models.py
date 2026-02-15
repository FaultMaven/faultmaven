"""SQLAlchemy ORM models for FaultMaven persistence layer.

This module defines SQLAlchemy models that map to the database schema
created by the Alembic migration (001_baseline_schema).

Database Support:
- PostgreSQL: Full feature set with native enums, JSONB
- SQLite: Simplified for development (TEXT for JSON, VARCHAR for enums)

Models:
- CaseModel: Main case entity with JSONB columns for flexible data
- EvidenceModel: Normalized evidence linked to cases
- HypothesisModel: Normalized hypotheses linked to cases
- SolutionModel: Normalized solutions linked to cases
- CaseMessageModel: Case conversation messages
- UploadedFileModel: Files uploaded to cases
- CaseStatusTransitionModel: Status change audit trail
- CaseTagModel: Case tagging
- AgentToolCallModel: Agent tool execution tracking
"""

import enum
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


# ============================================================
# Session Model
# ============================================================


class SessionModel(Base):
    """
    User session entity for tracking session-case relationships.

    Sessions enable context continuity across user interactions
    and support session-based case creation and retrieval.
    """

    __tablename__ = "sessions"

    # Primary Key - UUID format
    session_id = Column(String(36), primary_key=True)

    # Required Fields
    user_id = Column(String(255), nullable=False, index=True)
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_accessed = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Session context metadata (JSON)
    session_metadata = Column("metadata", Text, nullable=True, default="{}")

    # Relationship to cases
    cases = relationship(
        "CaseModel", back_populates="session", foreign_keys="CaseModel.session_id"
    )

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(user_id)) > 0", name="sessions_user_id_not_empty"),
    )

    def __repr__(self) -> str:
        return f"<SessionModel(session_id={self.session_id}, user_id={self.user_id})>"


# ============================================================
# Enums (Python-side - maps to DB enums or VARCHAR)
# ============================================================


class CaseStatusEnum(str, enum.Enum):
    """Case lifecycle status."""

    INQUIRY = "inquiry"
    PROBLEM_VERIFICATION = "problem_verification"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    SOLUTION_IMPLEMENTATION = "solution_implementation"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ARCHIVED = "archived"


class EvidenceCategoryEnum(str, enum.Enum):
    """Evidence category classification."""

    LOGS_AND_ERRORS = "LOGS_AND_ERRORS"
    STRUCTURED_CONFIG = "STRUCTURED_CONFIG"
    METRICS_AND_PERFORMANCE = "METRICS_AND_PERFORMANCE"
    UNSTRUCTURED_TEXT = "UNSTRUCTURED_TEXT"
    SOURCE_CODE = "SOURCE_CODE"
    VISUAL_EVIDENCE = "VISUAL_EVIDENCE"
    UNKNOWN = "UNKNOWN"


class HypothesisStatusEnum(str, enum.Enum):
    """Hypothesis lifecycle status."""

    PROPOSED = "proposed"
    TESTING = "testing"
    VALIDATED = "validated"
    INVALIDATED = "invalidated"
    DEFERRED = "deferred"


class SolutionStatusEnum(str, enum.Enum):
    """Solution lifecycle status."""

    PROPOSED = "proposed"
    IN_PROGRESS = "in_progress"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    REJECTED = "rejected"


class MessageRoleEnum(str, enum.Enum):
    """Message role in conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ToolCallStatusEnum(str, enum.Enum):
    """Tool call execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class RiskLevelEnum(str, enum.Enum):
    """Solution risk level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================
# Main Case Model
# ============================================================


class CaseModel(Base):
    """
    Main case entity with hybrid schema design.

    Normalized: High-cardinality data in related tables
    JSONB: Low-cardinality flexible data embedded
    """

    __tablename__ = "cases"

    # Primary Key
    case_id = Column(String(17), primary_key=True)

    # Required Fields
    user_id = Column(String(255), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    status = Column(String(50), nullable=False, default="inquiry", index=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # JSONB Columns (flexible data)
    inquiry = Column(
        Text, nullable=False, default="{}"
    )  # JSON as TEXT for SQLite compat
    problem_verification = Column(Text)
    working_conclusion = Column(Text)
    root_cause_conclusion = Column(Text)
    path_selection = Column(Text)
    degraded_mode = Column(Text)
    escalation_state = Column(Text)
    documentation = Column(Text, default="{}")
    progress = Column(Text, default="{}")
    # Use case_metadata as attribute to avoid conflict with SQLAlchemy Base.metadata
    case_metadata = Column("metadata", Text, default="{}")

    # Organization/Team
    organization_id = Column(String(20), index=True)
    team_id = Column(String(20), index=True)

    # Session link (optional - cases can exist without sessions)
    session_id = Column(
        String(36),
        ForeignKey("sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationships
    session = relationship(
        "SessionModel", back_populates="cases", foreign_keys=[session_id]
    )
    evidence = relationship(
        "EvidenceModel", back_populates="case", cascade="all, delete-orphan"
    )
    hypotheses = relationship(
        "HypothesisModel", back_populates="case", cascade="all, delete-orphan"
    )
    solutions = relationship(
        "SolutionModel", back_populates="case", cascade="all, delete-orphan"
    )
    messages = relationship(
        "CaseMessageModel", back_populates="case", cascade="all, delete-orphan"
    )
    uploaded_files = relationship(
        "UploadedFileModel", back_populates="case", cascade="all, delete-orphan"
    )
    status_transitions = relationship(
        "CaseStatusTransitionModel", back_populates="case", cascade="all, delete-orphan"
    )
    tags = relationship(
        "CaseTagModel", back_populates="case", cascade="all, delete-orphan"
    )
    tool_calls = relationship(
        "AgentToolCallModel", back_populates="case", cascade="all, delete-orphan"
    )
    evidence_artifacts = relationship(
        "EvidenceArtifactModel", back_populates="case", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(title)) > 0", name="cases_title_not_empty"),
        CheckConstraint("LENGTH(TRIM(user_id)) > 0", name="cases_user_id_not_empty"),
    )

    def __repr__(self) -> str:
        return f"<CaseModel(case_id={self.case_id}, title={self.title}, status={self.status})>"


# ============================================================
# Evidence Model
# ============================================================


class EvidenceModel(Base):
    """
    Evidence collected during investigation.

    Post-redesign (2026-02-11):
    - Tracks both valid evidence (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL) and
      rejected submissions (REJECTED category)
    - Includes deduplication via content_hash
    - Unique constraints ensure one evidence per turn and no duplicate uploads per case
    - New source_type field uses simplified 5-value enum (logs, metrics, configuration, visual, user_description)
    """

    __tablename__ = "evidence"

    evidence_id = Column(String(15), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Classification fields
    category = Column(String(50), nullable=False, index=True)
    # DataType field (simplified 5 values: logs, metrics, configuration, image, text)
    # Note: Column name is 'source_type_new' in database
    source_type = Column(String(50), name="source_type_new", nullable=True)

    # Content fields
    summary = Column(String(500), nullable=False)
    preprocessed_content = Column(Text, nullable=False)
    content_ref = Column(String(1000))
    file_size = Column(Integer)
    filename = Column(String(255))

    # Deduplication and turn tracking (NEW in redesign)
    content_hash = Column(String(64), nullable=True, index=True)
    collected_at_turn = Column(Integer, nullable=True, index=True)

    # Timestamps
    upload_timestamp = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    evidence_metadata = Column("metadata", Text, default="{}")

    # Relationship
    case = relationship("CaseModel", back_populates="evidence")

    __table_args__ = (
        # Data validation constraints
        CheckConstraint("LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"),
        CheckConstraint(
            "LENGTH(TRIM(preprocessed_content)) > 0", name="evidence_content_not_empty"
        ),
        # Unique constraints (via indexes for SQLite compatibility)
        # Note: These are implemented as unique indexes in the migration
        # uq_evidence_case_turn - one evidence per turn per case
        # uq_evidence_case_hash - no duplicate uploads per case
        # Performance indexes:
        # idx_evidence_case_category - case + category queries
        # idx_evidence_case_turn - case + turn queries
        # idx_evidence_content_hash - deduplication lookups
    )

    def __repr__(self) -> str:
        return (
            f"<EvidenceModel(evidence_id={self.evidence_id}, category={self.category})>"
        )


# ============================================================
# Hypothesis Model
# ============================================================


class HypothesisModel(Base):
    """Hypothesis for root cause analysis."""

    __tablename__ = "hypotheses"

    hypothesis_id = Column(String(15), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    statement = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="captured", index=True)
    likelihood = Column(Numeric(3, 2), default=0.5)
    initial_likelihood = Column(Numeric(3, 2), default=0.5)

    # Tracking
    last_updated_turn = Column(Integer, default=0)
    last_progress_at_turn = Column(Integer, default=0)
    iterations_without_progress = Column(Integer, default=0)

    category = Column(String(50), nullable=False, index=True)
    generation_mode = Column(String(20), nullable=False, default="systematic")

    rationale = Column(Text)
    retirement_reason = Column(Text)

    # Evidence Relationships (Many-to-Many via JSONB for simplicity in hybrid model,
    # or junction table. We'll stick to JSONB in the 'evidence_links' field to match Domain)
    evidence_links = Column(
        Text, default="{}"
    )  # JSON mapping of evidence_id -> details

    # Timestamps
    tested_at = Column(DateTime(timezone=True))
    concluded_at = Column(DateTime(timezone=True))
    proposed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    hypothesis_metadata = Column("metadata", Text, default="{}")

    # Multi-tenancy and audit fields (TASK-026)
    organization_id = Column(
        String(20),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    created_by = Column(String(255), nullable=False, index=True)
    updated_by = Column(String(255), nullable=True)

    # Relationship
    case = relationship("CaseModel", back_populates="hypotheses")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="hypotheses_statement_not_empty"
        ),
        CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="hypotheses_likelihood_range",
        ),
    )

    def __repr__(self) -> str:
        return f"<HypothesisModel(hypothesis_id={self.hypothesis_id}, status={self.status})>"


# ============================================================
# Solution Model
# ============================================================


class SolutionModel(Base):
    """Proposed and applied solutions."""

    __tablename__ = "solutions"

    solution_id = Column(String(15), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="proposed", index=True)
    implementation_steps = Column(Text)  # JSON array as TEXT
    risk_level = Column(String(20))
    estimated_effort = Column(String(50))
    verification_result = Column(Text)
    verification_timestamp = Column(DateTime(timezone=True))
    proposed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    implemented_at = Column(DateTime(timezone=True))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    solution_metadata = Column("metadata", Text, default="{}")

    # Multi-tenancy and audit fields (TASK-026)
    organization_id = Column(
        String(20),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    created_by = Column(String(255), nullable=False, index=True)
    updated_by = Column(String(255), nullable=True)

    # Relationship
    case = relationship("CaseModel", back_populates="solutions")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(description)) > 0", name="solutions_description_not_empty"
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')",
            name="solutions_risk_level_valid",
        ),
    )

    def __repr__(self) -> str:
        return f"<SolutionModel(solution_id={self.solution_id}, status={self.status})>"


# ============================================================
# Case Message Model
# ============================================================


class CaseMessageModel(Base):
    """Case conversation messages.

    Schema per design spec (case-schema.md §4.7):
    - turn_number, created_at, token_count
    """

    __tablename__ = "case_messages"

    message_id = Column(String(20), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    turn_number = Column(Integer, nullable=False, default=0)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    token_count = Column(Integer)
    message_metadata = Column("metadata", Text, default="{}")

    # Relationship
    case = relationship("CaseModel", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(content)) > 0", name="case_messages_content_not_empty"
        ),
        CheckConstraint("turn_number >= 0", name="case_messages_turn_nonnegative"),
        # Index for turn-based retrieval (multiple messages can share same turn_number)
        Index("idx_case_messages_case_turn", "case_id", "turn_number"),
        # Composite index for efficient ORDER BY created_at queries
        Index("idx_case_messages_case_created", "case_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<CaseMessageModel(message_id={self.message_id}, role={self.role})>"


# ============================================================
# Uploaded File Model
# ============================================================


class UploadedFileModel(Base):
    """Files uploaded to cases.

    Schema per design spec (case-schema.md §4.6):
    - size_bytes, data_type, content_ref, uploaded_at_turn, source_type, preprocessing_summary
    """

    __tablename__ = "uploaded_files"

    file_id = Column(String(15), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    filename = Column(String(255), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    data_type = Column(String(50), nullable=False, default="other")
    uploaded_at_turn = Column(Integer, nullable=False, default=0)
    uploaded_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_type = Column(String(50), nullable=False, default="file_upload")
    content_ref = Column(String(1000))
    preprocessing_summary = Column(Text)
    file_metadata = Column("metadata", Text, default="{}")

    # Relationship
    case = relationship("CaseModel", back_populates="uploaded_files")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(filename)) > 0", name="uploaded_files_filename_not_empty"
        ),
        CheckConstraint("size_bytes > 0", name="uploaded_files_size_positive"),
        CheckConstraint(
            "uploaded_at_turn >= 0", name="uploaded_files_turn_nonnegative"
        ),
    )

    def __repr__(self) -> str:
        return f"<UploadedFileModel(file_id={self.file_id}, filename={self.filename})>"


# ============================================================
# Case Status Transition Model
# ============================================================


class CaseStatusTransitionModel(Base):
    """Status change audit trail."""

    __tablename__ = "case_status_transitions"

    transition_id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    from_status = Column(String(50))
    to_status = Column(String(50), nullable=False)
    reason = Column(Text)
    transitioned_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    transition_metadata = Column("metadata", Text, default="{}")

    # Relationship
    case = relationship("CaseModel", back_populates="status_transitions")

    def __repr__(self) -> str:
        return (
            f"<CaseStatusTransitionModel(from={self.from_status}, to={self.to_status})>"
        )


# ============================================================
# Case Tag Model
# ============================================================


class CaseTagModel(Base):
    """Case tags for categorization."""

    __tablename__ = "case_tags"

    tag_id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    tag = Column(String(50), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationship
    case = relationship("CaseModel", back_populates="tags")

    __table_args__ = (
        UniqueConstraint("case_id", "tag", name="case_tags_unique"),
        CheckConstraint("LENGTH(TRIM(tag)) > 0", name="case_tags_tag_not_empty"),
    )

    def __repr__(self) -> str:
        return f"<CaseTagModel(case_id={self.case_id}, tag={self.tag})>"


# ============================================================
# Agent Tool Call Model
# ============================================================


class AgentToolCallModel(Base):
    """Agent tool execution tracking."""

    __tablename__ = "agent_tool_calls"

    call_id = Column(String(20), primary_key=True)
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    tool_name = Column(String(100), nullable=False, index=True)
    tool_input = Column(Text, nullable=False)  # JSON as TEXT
    tool_output = Column(Text)  # JSON as TEXT
    status = Column(String(20), nullable=False, default="pending", index=True)
    error_message = Column(Text)
    duration_ms = Column(Integer)
    started_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at = Column(DateTime(timezone=True))
    tool_metadata = Column("metadata", Text, default="{}")

    # Relationship
    case = relationship("CaseModel", back_populates="tool_calls")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(tool_name)) > 0", name="agent_tool_calls_tool_name_not_empty"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error')",
            name="agent_tool_calls_status_valid",
        ),
    )

    def __repr__(self) -> str:
        return f"<AgentToolCallModel(call_id={self.call_id}, tool_name={self.tool_name}, status={self.status})>"


# ============================================================
# Evidence Artifact Model
# ============================================================


class EvidenceArtifactTypeEnum(str, enum.Enum):
    """Evidence artifact type classification."""

    SCREENSHOT = "screenshot"
    LOG_FILE = "log_file"
    NETWORK_TRACE = "network_trace"
    CODE_SNIPPET = "code_snippet"
    CONFIGURATION = "configuration"
    VIDEO_RECORDING = "video_recording"
    HAR_FILE = "har_file"
    CRASH_DUMP = "crash_dump"
    HEAP_DUMP = "heap_dump"
    THREAD_DUMP = "thread_dump"
    METRICS_EXPORT = "metrics_export"
    OTHER = "other"


class StorageBackendEnum(str, enum.Enum):
    """Storage backend type."""

    LOCAL_FILESYSTEM = "local_filesystem"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    GCS = "gcs"


class EvidenceArtifactModel(Base):
    """Evidence artifact file metadata linked to cases.

    Represents files (screenshots, logs, traces, etc.) collected as
    evidence during case investigation. Supports multiple storage backends.
    """

    __tablename__ = "evidence_artifacts"

    # Primary Key
    evidence_id = Column(String(64), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ownership
    user_id = Column(String(255), nullable=False, index=True)
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # File metadata
    original_filename = Column(String(512), nullable=False)
    stored_filename = Column(String(512), nullable=False)
    file_path = Column(String(2048), nullable=False)
    evidence_type = Column(String(64), nullable=False, index=True)
    mime_type = Column(String(256), nullable=False)
    file_size = Column(
        Integer, nullable=False
    )  # BigInteger in migration, Integer for ORM compat
    storage_backend = Column(String(64), nullable=False, default="local_filesystem")

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Optional fields
    artifact_metadata = Column("metadata", Text, default="{}")
    description = Column(Text, nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)

    # Relationship to case
    case = relationship("CaseModel", back_populates="evidence_artifacts")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(original_filename)) > 0",
            name="evidence_artifacts_filename_not_empty",
        ),
        CheckConstraint(
            "LENGTH(TRIM(file_path)) > 0", name="evidence_artifacts_file_path_not_empty"
        ),
        CheckConstraint(
            "file_size >= 0", name="evidence_artifacts_file_size_non_negative"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EvidenceArtifactModel(evidence_id={self.evidence_id}, "
            f"case_id={self.case_id}, "
            f"original_filename={self.original_filename}, "
            f"evidence_type={self.evidence_type})>"
        )


# ============================================================
# Agent Execution Status Enum
# ============================================================


class ExecutionStatusEnum(str, enum.Enum):
    """Agent execution status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class AgentTypeEnum(str, enum.Enum):
    """Types of AI agents."""

    INVESTIGATOR = "investigator"
    DEBUGGER = "debugger"
    RESEARCHER = "researcher"
    VALIDATOR = "validator"
    REPORTER = "reporter"
    CUSTOM = "custom"


# ============================================================
# Case Checkpoint Model (TASK-028)
# ============================================================


class CaseCheckpointModel(Base):
    """
    Immutable snapshot of a case at a specific turn.
    Used for time-travel debugging, drift detection, and undo functionality.
    """

    __tablename__ = "case_checkpoints"

    # Primary Key
    checkpoint_id = Column(String(50), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    turn_number = Column(Integer, nullable=False)

    # Snapshot Data
    case_snapshot = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )  # Full case state
    snapshot_hash = Column(String(64), nullable=False)  # SHA256 for drift/dedup
    trigger = Column(String(50), nullable=False)  # Reason for checkpoint

    # Metadata
    checkpoint_metadata = Column("metadata", Text, default="{}")

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    # Relationships
    case = relationship("CaseModel", backref="checkpoints")

    __table_args__ = (
        # Compound index for efficient turn-based retrieval
        Index("ix_case_turn", "case_id", "turn_number"),
        CheckConstraint(
            "LENGTH(TRIM(snapshot_hash)) > 0", name="case_checkpoints_hash_not_empty"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CaseCheckpointModel(checkpoint_id={self.checkpoint_id}, "
            f"case_id={self.case_id}, turn={self.turn_number})>"
        )


# ============================================================
# Agent Execution Model
# ============================================================


class AgentExecutionModel(Base):
    """Agent execution tracking for AI agent transparency.

    Tracks the full lifecycle of an agent execution from queued to completion,
    including prompt, response, token usage, and timing information.
    """

    __tablename__ = "agent_executions"

    # Primary Key
    execution_id = Column(String(64), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Agent identification
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",  # Default org for single-tenant
    )
    agent_type = Column(String(64), nullable=False, index=True)
    agent_model = Column(String(128), nullable=False, index=True)

    # Execution status
    status = Column(String(32), nullable=False, default="queued", index=True)

    # Timing
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    execution_duration_ms = Column(Integer, nullable=True)

    # Prompt and response
    prompt = Column(Text, nullable=True)
    response = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    # Token usage (JSON)
    token_usage = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Metadata (JSON)
    execution_metadata = Column("metadata", Text, default="{}")

    # Optional: link execution to session (ON DELETE SET NULL)
    session_id = Column(
        String(64),
        ForeignKey("investigation_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationships
    case = relationship("CaseModel", backref="agent_executions")
    session = relationship(
        "InvestigationSessionModel",
        back_populates="agent_executions",
        foreign_keys=[session_id],
    )
    tool_calls_v2 = relationship(
        "AgentToolCallV2Model",
        back_populates="execution",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timeout')",
            name="agent_executions_status_check",
        ),
        CheckConstraint(
            "agent_type IN ('investigator', 'debugger', 'researcher', 'validator', 'reporter', 'custom')",
            name="agent_executions_agent_type_check",
        ),
        CheckConstraint(
            "execution_duration_ms IS NULL OR execution_duration_ms >= 0",
            name="agent_executions_duration_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentExecutionModel(execution_id={self.execution_id}, "
            f"case_id={self.case_id}, "
            f"agent_type={self.agent_type}, "
            f"status={self.status})>"
        )


# ============================================================
# Agent Tool Call V2 Model (Execution-level)
# ============================================================


class AgentToolCallV2Model(Base):
    """Tool call tracking for agent executions.

    Tracks individual tool invocations made during an agent execution,
    including input, output, status, and timing information.

    Note: This is separate from AgentToolCallModel which tracks
    tool calls at the case level. This model tracks tool calls
    within a specific agent execution context.
    """

    __tablename__ = "agent_tool_calls_v2"

    # Primary Key
    tool_call_id = Column(String(64), primary_key=True)

    # Foreign Key to agent_executions
    execution_id = Column(
        String(64),
        ForeignKey("agent_executions.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Tool information
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",  # Default org for single-tenant
    )
    tool_name = Column(String(128), nullable=False, index=True)
    tool_input = Column(Text, nullable=True)  # JSON as TEXT
    tool_output = Column(Text, nullable=True)  # JSON as TEXT

    # Status and error
    status = Column(String(32), nullable=False, default="pending", index=True)
    error_message = Column(Text, nullable=True)

    # Timing
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationship to execution
    execution = relationship("AgentExecutionModel", back_populates="tool_calls_v2")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed')",
            name="agent_tool_calls_v2_status_check",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="agent_tool_calls_v2_duration_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentToolCallV2Model(tool_call_id={self.tool_call_id}, "
            f"execution_id={self.execution_id}, "
            f"tool_name={self.tool_name}, "
            f"status={self.status})>"
        )


# ============================================================
# Investigation Session Status Enum
# ============================================================


class InvestigationSessionStatusEnum(str, enum.Enum):
    """Investigation session status."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# ============================================================
# Investigation Session Model
# ============================================================


class InvestigationSessionModel(Base):
    """Investigation session tracking for case investigations.

    Tracks investigation sessions within cases, including temporal structure,
    agent executions, token usage, and session state management.

    This creates a four-level CASCADE delete chain:
        Case → InvestigationSession → AgentExecution → AgentToolCall
    """

    __tablename__ = "investigation_sessions"

    # Primary Key
    session_id = Column(String(64), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(17),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ownership
    user_id = Column(String(255), nullable=False, index=True)
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Session status
    status = Column(String(32), nullable=False, default="active", index=True)

    # Temporal tracking
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=False)
    total_duration_ms = Column(Integer, nullable=True)

    # Investigation context
    session_goal = Column(Text, nullable=True)
    findings_summary = Column(Text, nullable=True)

    # Resource tracking
    total_token_usage = Column(Integer, nullable=False, default=0)
    total_agent_executions = Column(Integer, nullable=False, default=0)
    token_budget_limit = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Metadata (JSON)
    session_metadata = Column("metadata", Text, default="{}")

    # Relationships
    case = relationship("CaseModel", backref="investigation_sessions")
    agent_executions = relationship(
        "AgentExecutionModel",
        back_populates="session",
        foreign_keys="AgentExecutionModel.session_id",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'abandoned')",
            name="investigation_sessions_status_check",
        ),
        CheckConstraint(
            "total_duration_ms IS NULL OR total_duration_ms >= 0",
            name="investigation_sessions_duration_check",
        ),
        CheckConstraint(
            "total_token_usage >= 0", name="investigation_sessions_token_usage_check"
        ),
        CheckConstraint(
            "total_agent_executions >= 0",
            name="investigation_sessions_executions_check",
        ),
        CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit >= 0",
            name="investigation_sessions_budget_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<InvestigationSessionModel(session_id={self.session_id}, "
            f"case_id={self.case_id}, "
            f"status={self.status}, "
            f"total_agent_executions={self.total_agent_executions})>"
        )


# ============================================================
# Knowledge Item Model
# ============================================================


class KnowledgeItemTypeEnum(str, enum.Enum):
    """Knowledge item type classification."""

    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_TEMPLATE = "solution_template"
    API_DOCUMENTATION = "api_documentation"
    CONFIGURATION_GUIDE = "configuration_guide"
    BEST_PRACTICE = "best_practice"
    FAQ = "faq"
    RUNBOOK = "runbook"


# ============================================================
# Standalone Evidence Model (PR #46b)
# ============================================================


class StandaloneEvidenceModel(Base):
    """Standalone evidence file metadata for Evidence module (PR #46b).

    Unlike EvidenceModel (which is case-scoped), this model supports
    standalone evidence files that can be linked to multiple cases.

    Used by the Evidence Service API endpoints:
    - POST /api/v1/evidence (upload)
    - GET /api/v1/evidence/{id} (get details)
    - DELETE /api/v1/evidence/{id} (delete)
    - POST /api/v1/evidence/{id}/link (link to case)
    """

    __tablename__ = "standalone_evidence"

    # Primary Key (UUID)
    id = Column(String(36), primary_key=True)

    # File metadata
    filename = Column(String(512), nullable=False)
    content_type = Column(String(256), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    storage_path = Column(String(2048), nullable=False)

    # Ownership
    uploaded_by = Column(String(36), nullable=False, index=True)
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Timestamps
    uploaded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    # Optional fields
    description = Column(Text, nullable=True)
    tags = Column(Text, nullable=False, default="[]")  # JSON array as TEXT
    linked_cases = Column(Text, nullable=False, default="[]")  # JSON array of case IDs

    # Metadata (JSON)
    evidence_metadata = Column("metadata", Text, default="{}")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(filename)) > 0", name="standalone_evidence_filename_not_empty"
        ),
        CheckConstraint(
            "LENGTH(TRIM(storage_path)) > 0",
            name="standalone_evidence_storage_path_not_empty",
        ),
        CheckConstraint(
            "size_bytes >= 0", name="standalone_evidence_size_non_negative"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<StandaloneEvidenceModel(id={self.id}, "
            f"filename={self.filename}, "
            f"size_bytes={self.size_bytes})>"
        )


# ============================================================
# Knowledge Item Model
# ============================================================


class KnowledgeItemModel(Base):
    """Knowledge base item for RAG system.

    Represents an indexed document or knowledge snippet with embeddings
    for semantic search and retrieval.

    Note: This model does NOT have a foreign key to cases. Knowledge items
    are organization-scoped and persist independently for compliance/audit.
    """

    __tablename__ = "knowledge_items"

    # Primary Key
    item_id = Column(String(64), primary_key=True)

    # Organization scope (NO foreign key - items persist independently)
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Content
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    item_type = Column(String(64), nullable=False, index=True)

    # Categorization
    category = Column(String(128), nullable=True, index=True)
    tags = Column(Text, nullable=False, default="[]")  # JSON array

    # Vector search
    embedding_model = Column(
        String(128), nullable=False, default="text-embedding-3-small"
    )
    embedding_vector = Column(
        Text, nullable=True
    )  # VECTOR(1536) for PostgreSQL+pgvector
    embedding_version = Column(Integer, nullable=False, default=1)

    # Source metadata
    source_url = Column(String(2048), nullable=True)
    author = Column(String(255), nullable=True)
    language = Column(String(8), nullable=False, default="en")

    # Verification status (0=experimental, 1=community, 2=admin_verified)
    verification_level = Column(Integer, nullable=False, default=0, index=True)
    verification_reason = Column(String(512), nullable=True)
    verified_by = Column(String(64), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    # Lineage tracking (for suggestions that became knowledge items)
    source_suggestion_id = Column(String(64), nullable=True, index=True)

    # Usage tracking
    view_count = Column(Integer, nullable=False, default=0)
    helpful_count = Column(Integer, nullable=False, default=0)
    not_helpful_count = Column(Integer, nullable=False, default=0)
    last_retrieved_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Lifecycle
    is_published = Column(Boolean, nullable=False, default=True, index=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Metadata (JSON)
    knowledge_metadata = Column("metadata", Text, default="{}")

    __table_args__ = (
        CheckConstraint(
            "item_type IN ('troubleshooting_guide', 'error_pattern', 'solution_template', "
            "'api_documentation', 'configuration_guide', 'best_practice', 'faq', 'runbook')",
            name="knowledge_items_item_type_check",
        ),
        CheckConstraint("view_count >= 0", name="knowledge_items_view_count_check"),
        CheckConstraint(
            "helpful_count >= 0", name="knowledge_items_helpful_count_check"
        ),
        CheckConstraint(
            "not_helpful_count >= 0", name="knowledge_items_not_helpful_count_check"
        ),
        CheckConstraint(
            "embedding_version >= 1", name="knowledge_items_embedding_version_check"
        ),
        CheckConstraint(
            "verification_level >= 0 AND verification_level <= 2",
            name="knowledge_items_verification_level_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeItemModel(item_id={self.item_id}, "
            f"title={self.title}, "
            f"item_type={self.item_type}, "
            f"is_published={self.is_published})>"
        )


# ============================================================
# Knowledge Suggestion Model
# ============================================================


class KnowledgeSuggestionModel(Base):
    """Knowledge suggestion extracted from a case, pending review.

    Represents knowledge that was extracted from an incident case
    and is awaiting admin review before being added to the knowledge base.

    PII Scanning: All suggestions must pass PII scanning before review (HITL).
    Lineage: Links back to source case and forward to created KnowledgeItem.
    """

    __tablename__ = "knowledge_suggestions"

    # Primary Key
    suggestion_id = Column(String(64), primary_key=True)

    # Organization and Case scope
    organization_id = Column(
        String(64),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    case_id = Column(String(64), nullable=False, index=True)

    # Status
    status = Column(
        String(32), nullable=False, default="pending_review", index=True
    )  # pending_review, approved, rejected, draft

    # Suggested content
    suggested_title = Column(String(512), nullable=False)
    suggested_content = Column(Text, nullable=False)
    suggested_type = Column(String(64), nullable=False, default="troubleshooting_guide")

    # Extraction metadata
    extracted_by = Column(String(64), nullable=False, index=True)
    extracted_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    include_messages = Column(Boolean, nullable=False, default=True)
    include_evidence = Column(Boolean, nullable=False, default=True)

    # PII scanning (HITL requirement)
    pii_scan_status = Column(
        String(32), nullable=False, default="not_scanned", index=True
    )  # not_scanned, scanning, clean, pii_detected, remediated, scan_failed
    pii_scan_result = Column(Text, nullable=True)  # JSON
    pii_remediated_by = Column(String(64), nullable=True)
    pii_remediated_at = Column(DateTime(timezone=True), nullable=True)

    # Lineage (for Review Inbox footer)
    source_case_title = Column(String(512), nullable=True)
    message_count = Column(Integer, nullable=False, default=0)
    evidence_count = Column(Integer, nullable=False, default=0)

    # Review metadata
    reviewed_by = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    # Bidirectional link to KnowledgeItem (when approved)
    knowledge_item_id = Column(String(64), nullable=True, index=True)

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Metadata (JSON)
    suggestion_metadata = Column("metadata", Text, default="{}")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'draft')",
            name="knowledge_suggestions_status_check",
        ),
        CheckConstraint(
            "pii_scan_status IN ('not_scanned', 'scanning', 'clean', "
            "'pii_detected', 'remediated', 'scan_failed')",
            name="knowledge_suggestions_pii_scan_status_check",
        ),
        CheckConstraint(
            "message_count >= 0", name="knowledge_suggestions_message_count_check"
        ),
        CheckConstraint(
            "evidence_count >= 0", name="knowledge_suggestions_evidence_count_check"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeSuggestionModel(suggestion_id={self.suggestion_id}, "
            f"case_id={self.case_id}, "
            f"status={self.status}, "
            f"pii_scan_status={self.pii_scan_status})>"
        )
