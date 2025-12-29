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

    # Relationships
    evidence = relationship("EvidenceModel", back_populates="case", cascade="all, delete-orphan")
    hypotheses = relationship("HypothesisModel", back_populates="case", cascade="all, delete-orphan")
    solutions = relationship("SolutionModel", back_populates="case", cascade="all, delete-orphan")
    messages = relationship("CaseMessageModel", back_populates="case", cascade="all, delete-orphan")
    uploaded_files = relationship("UploadedFileModel", back_populates="case", cascade="all, delete-orphan")
    status_transitions = relationship("CaseStatusTransitionModel", back_populates="case", cascade="all, delete-orphan")
    tags = relationship("CaseTagModel", back_populates="case", cascade="all, delete-orphan")
    tool_calls = relationship("AgentToolCallModel", back_populates="case", cascade="all, delete-orphan")

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
