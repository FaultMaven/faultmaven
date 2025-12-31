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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import enum

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Boolean,
    Numeric,
    ForeignKey,
    Enum,
    UniqueConstraint,
    CheckConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
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

    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_accessed = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Session context metadata (JSON)
    session_metadata = Column("metadata", Text, nullable=True, default='{}')

    # Relationship to cases
    cases = relationship(
        "CaseModel",
        back_populates="session",
        foreign_keys="CaseModel.session_id"
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
    CONSULTING = "consulting"
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


class FileProcessingStatusEnum(str, enum.Enum):
    """File processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


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
    status = Column(String(50), nullable=False, default="consulting", index=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    # JSONB Columns (flexible data)
    consulting = Column(Text, nullable=False, default='{}')  # JSON as TEXT for SQLite compat
    problem_verification = Column(Text)
    working_conclusion = Column(Text)
    root_cause_conclusion = Column(Text)
    path_selection = Column(Text)
    degraded_mode = Column(Text)
    escalation_state = Column(Text)
    documentation = Column(Text, default='{}')
    progress = Column(Text, default='{}')
    # Use case_metadata as attribute to avoid conflict with SQLAlchemy Base.metadata
    case_metadata = Column("metadata", Text, default='{}')

    # Organization/Team
    org_id = Column(String(20), index=True)
    team_id = Column(String(20), index=True)

    # Session link (optional - cases can exist without sessions)
    session_id = Column(
        String(36),
        ForeignKey("sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Relationships
    session = relationship("SessionModel", back_populates="cases", foreign_keys=[session_id])
    evidence = relationship("EvidenceModel", back_populates="case", cascade="all, delete-orphan")
    hypotheses = relationship("HypothesisModel", back_populates="case", cascade="all, delete-orphan")
    solutions = relationship("SolutionModel", back_populates="case", cascade="all, delete-orphan")
    messages = relationship("CaseMessageModel", back_populates="case", cascade="all, delete-orphan")
    uploaded_files = relationship("UploadedFileModel", back_populates="case", cascade="all, delete-orphan")
    status_transitions = relationship("CaseStatusTransitionModel", back_populates="case", cascade="all, delete-orphan")
    tags = relationship("CaseTagModel", back_populates="case", cascade="all, delete-orphan")
    tool_calls = relationship("AgentToolCallModel", back_populates="case", cascade="all, delete-orphan")
    evidence_artifacts = relationship("EvidenceArtifactModel", back_populates="case", cascade="all, delete-orphan")

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
    """Evidence collected during investigation."""
    __tablename__ = "evidence"

    evidence_id = Column(String(15), primary_key=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    summary = Column(String(500), nullable=False)
    preprocessed_content = Column(Text, nullable=False)
    content_ref = Column(String(1000))
    file_size = Column(Integer)
    filename = Column(String(255))
    upload_timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    evidence_metadata = Column("metadata", Text, default='{}')

    # Relationship
    case = relationship("CaseModel", back_populates="evidence")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"),
        CheckConstraint("LENGTH(TRIM(preprocessed_content)) > 0", name="evidence_content_not_empty"),
    )

    def __repr__(self) -> str:
        return f"<EvidenceModel(evidence_id={self.evidence_id}, category={self.category})>"


# ============================================================
# Hypothesis Model
# ============================================================

class HypothesisModel(Base):
    """Hypothesis for root cause analysis."""
    __tablename__ = "hypotheses"

    hypothesis_id = Column(String(15), primary_key=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="proposed", index=True)
    confidence_score = Column(Numeric(3, 2))
    supporting_evidence_ids = Column(Text)  # JSON array as TEXT
    validation_result = Column(Text)
    validation_timestamp = Column(DateTime(timezone=True))
    proposed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    hypothesis_metadata = Column("metadata", Text, default='{}')

    # Multi-tenancy and audit fields (TASK-026)
    organization_id = Column(String(20), nullable=False, index=True)
    created_by = Column(String(255), nullable=False, index=True)
    updated_by = Column(String(255), nullable=True)

    # Relationship
    case = relationship("CaseModel", back_populates="hypotheses")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(description)) > 0", name="hypotheses_description_not_empty"),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="hypotheses_confidence_range"
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
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="proposed", index=True)
    implementation_steps = Column(Text)  # JSON array as TEXT
    risk_level = Column(String(20))
    estimated_effort = Column(String(50))
    verification_result = Column(Text)
    verification_timestamp = Column(DateTime(timezone=True))
    proposed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    implemented_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    solution_metadata = Column("metadata", Text, default='{}')

    # Multi-tenancy and audit fields (TASK-026)
    organization_id = Column(String(20), nullable=False, index=True)
    created_by = Column(String(255), nullable=False, index=True)
    updated_by = Column(String(255), nullable=True)

    # Relationship
    case = relationship("CaseModel", back_populates="solutions")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(description)) > 0", name="solutions_description_not_empty"),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')",
            name="solutions_risk_level_valid"
        ),
    )

    def __repr__(self) -> str:
        return f"<SolutionModel(solution_id={self.solution_id}, status={self.status})>"


# ============================================================
# Case Message Model
# ============================================================

class CaseMessageModel(Base):
    """Case conversation messages."""
    __tablename__ = "case_messages"

    message_id = Column(String(20), primary_key=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    message_metadata = Column("metadata", Text, default='{}')

    # Relationship
    case = relationship("CaseModel", back_populates="messages")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(content)) > 0", name="case_messages_content_not_empty"),
    )

    def __repr__(self) -> str:
        return f"<CaseMessageModel(message_id={self.message_id}, role={self.role})>"


# ============================================================
# Uploaded File Model
# ============================================================

class UploadedFileModel(Base):
    """Files uploaded to cases."""
    __tablename__ = "uploaded_files"

    file_id = Column(String(15), primary_key=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=False)
    content_type = Column(String(100))
    storage_path = Column(String(1000))
    processing_status = Column(String(20), nullable=False, default="pending")
    processing_error = Column(Text)
    uploaded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at = Column(DateTime(timezone=True))
    file_metadata = Column("metadata", Text, default='{}')

    # Relationship
    case = relationship("CaseModel", back_populates="uploaded_files")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(filename)) > 0", name="uploaded_files_filename_not_empty"),
        CheckConstraint("file_size > 0", name="uploaded_files_file_size_positive"),
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
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(50))
    to_status = Column(String(50), nullable=False)
    reason = Column(Text)
    transitioned_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    transition_metadata = Column("metadata", Text, default='{}')

    # Relationship
    case = relationship("CaseModel", back_populates="status_transitions")

    def __repr__(self) -> str:
        return f"<CaseStatusTransitionModel(from={self.from_status}, to={self.to_status})>"


# ============================================================
# Case Tag Model
# ============================================================

class CaseTagModel(Base):
    """Case tags for categorization."""
    __tablename__ = "case_tags"

    tag_id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    tag = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

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
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False, index=True)
    tool_input = Column(Text, nullable=False)  # JSON as TEXT
    tool_output = Column(Text)  # JSON as TEXT
    status = Column(String(20), nullable=False, default="pending", index=True)
    error_message = Column(Text)
    duration_ms = Column(Integer)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    tool_metadata = Column("metadata", Text, default='{}')

    # Relationship
    case = relationship("CaseModel", back_populates="tool_calls")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(tool_name)) > 0", name="agent_tool_calls_tool_name_not_empty"),
        CheckConstraint("status IN ('pending', 'running', 'success', 'error')", name="agent_tool_calls_status_valid"),
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
    organization_id = Column(String(64), nullable=False, index=True)

    # File metadata
    original_filename = Column(String(512), nullable=False)
    stored_filename = Column(String(512), nullable=False)
    file_path = Column(String(2048), nullable=False)
    evidence_type = Column(String(64), nullable=False, index=True)
    mime_type = Column(String(256), nullable=False)
    file_size = Column(Integer, nullable=False)  # BigInteger in migration, Integer for ORM compat
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
        CheckConstraint("LENGTH(TRIM(original_filename)) > 0", name="evidence_artifacts_filename_not_empty"),
        CheckConstraint("LENGTH(TRIM(file_path)) > 0", name="evidence_artifacts_file_path_not_empty"),
        CheckConstraint("file_size >= 0", name="evidence_artifacts_file_size_non_negative"),
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
            name="agent_executions_status_check"
        ),
        CheckConstraint(
            "agent_type IN ('investigator', 'debugger', 'researcher', 'validator', 'reporter', 'custom')",
            name="agent_executions_agent_type_check"
        ),
        CheckConstraint(
            "execution_duration_ms IS NULL OR execution_duration_ms >= 0",
            name="agent_executions_duration_check"
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
            name="agent_tool_calls_v2_status_check"
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="agent_tool_calls_v2_duration_check"
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
    organization_id = Column(String(64), nullable=False, index=True)

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
            name="investigation_sessions_status_check"
        ),
        CheckConstraint(
            "total_duration_ms IS NULL OR total_duration_ms >= 0",
            name="investigation_sessions_duration_check"
        ),
        CheckConstraint(
            "total_token_usage >= 0",
            name="investigation_sessions_token_usage_check"
        ),
        CheckConstraint(
            "total_agent_executions >= 0",
            name="investigation_sessions_executions_check"
        ),
        CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit >= 0",
            name="investigation_sessions_budget_check"
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
# Knowledge Item Type Enum
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
    organization_id = Column(String(64), nullable=False, index=True)

    # Content
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    item_type = Column(String(64), nullable=False, index=True)

    # Categorization
    category = Column(String(128), nullable=True, index=True)
    tags = Column(Text, nullable=False, default="[]")  # JSON array

    # Vector search
    embedding_model = Column(String(128), nullable=False, default="text-embedding-3-small")
    embedding_vector = Column(Text, nullable=True)  # VECTOR(1536) for PostgreSQL+pgvector
    embedding_version = Column(Integer, nullable=False, default=1)

    # Source metadata
    source_url = Column(String(2048), nullable=True)
    author = Column(String(255), nullable=True)
    language = Column(String(8), nullable=False, default="en")

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
            name="knowledge_items_item_type_check"
        ),
        CheckConstraint("view_count >= 0", name="knowledge_items_view_count_check"),
        CheckConstraint("helpful_count >= 0", name="knowledge_items_helpful_count_check"),
        CheckConstraint("not_helpful_count >= 0", name="knowledge_items_not_helpful_count_check"),
        CheckConstraint("embedding_version >= 1", name="knowledge_items_embedding_version_check"),
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeItemModel(item_id={self.item_id}, "
            f"title={self.title}, "
            f"item_type={self.item_type}, "
            f"is_published={self.is_published})>"
        )
