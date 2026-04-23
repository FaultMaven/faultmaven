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
- CaseActionModel: Case action (phase transition / disposition) audit trail
- CaseTagModel: Case tagging
"""

import enum
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

Base = declarative_base()


# ============================================================
# Auth & RBAC Models (User Domain)
# ============================================================


class UserModel(Base):
    """User account."""

    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True)
    username = Column(String(100), nullable=False, unique=True)
    email = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(200), nullable=False)
    avatar_url = Column(String(500), nullable=True)
    timezone = Column(String(50), nullable=False, server_default="UTC")
    locale = Column(String(10), nullable=False, server_default="en-US")
    hashed_password = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="1")
    is_email_verified = Column(Boolean, nullable=False, server_default="0")
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    sso_provider = Column(String(50), nullable=True)
    sso_provider_id = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_password_change_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    roles = Column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_username", "username", unique=True),
        Index("ix_users_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<User(user_id={self.user_id}, username={self.username})>"


class OrganizationModel(Base):
    """Organization (multi-tenancy)."""

    __tablename__ = "organizations"

    organization_id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, unique=True)
    owner_id = Column(String(36), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="1")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_ = Column("metadata", Text, nullable=True)
    description = Column(Text, nullable=True)
    plan_tier = Column(String(20), nullable=False, server_default="free")
    max_members = Column(Integer, nullable=False, server_default="5")
    max_cases = Column(Integer, nullable=True)
    settings = Column(Text, nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_organizations_owner_id", "owner_id"),
        Index("ix_organizations_slug", "slug", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<Organization(organization_id={self.organization_id}, name={self.name})>"
        )


class RoleModel(Base):
    """RBAC role definition."""

    __tablename__ = "roles"

    role_id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    scope = Column(String(20), nullable=False, server_default="organization")
    is_system_role = Column(Boolean, nullable=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Role(role_id={self.role_id}, name={self.name})>"


class PermissionModel(Base):
    """RBAC permission definition."""

    __tablename__ = "permissions"

    permission_id = Column(String(36), primary_key=True)
    resource = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "resource", "action", name="permissions_resource_action_unique"
        ),
    )

    def __repr__(self) -> str:
        return f"<Permission(permission_id={self.permission_id})>"


class RolePermissionModel(Base):
    """RBAC role-to-permission mapping."""

    __tablename__ = "role_permissions"

    role_id = Column(
        String(36),
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id = Column(
        String(36),
        ForeignKey("permissions.permission_id", ondelete="CASCADE"),
        primary_key=True,
    )


class OrganizationMemberModel(Base):
    """Organization membership."""

    __tablename__ = "organization_members"

    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id = Column(String(36), ForeignKey("roles.role_id"), nullable=False)
    invited_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    invited_at = Column(DateTime(timezone=True), nullable=True)
    invitation_accepted_at = Column(DateTime(timezone=True), nullable=True)
    joined_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_org_members_organization_id", "organization_id"),
        Index("ix_org_members_role_id", "role_id"),
    )


class TeamModel(Base):
    """Team within an organization."""

    __tablename__ = "teams"

    team_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", name="teams_organization_name_unique"
        ),
        Index("ix_teams_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:
        return f"<Team(team_id={self.team_id}, name={self.name})>"


class TeamMemberModel(Base):
    """Team membership."""

    __tablename__ = "team_members"

    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_role = Column(String(50), nullable=True)
    joined_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_team_members_team_id", "team_id"),)


class UserAuditLogModel(Base):
    """Audit trail for user actions."""

    __tablename__ = "user_audit_log"

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type = Column(String(100), nullable=False, index=True)
    event_category = Column(String(50), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(String(50), nullable=True)
    details = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_user_audit_log_user_id", "user_id", "created_at"),
        Index("ix_user_audit_log_organization_id", "organization_id", "created_at"),
    )


class OAuthRevokedTokenModel(Base):
    """Revoked JWT tokens for token invalidation."""

    __tablename__ = "oauth_revoked_tokens"

    jti = Column(String(64), primary_key=True)
    revoked_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_revoked_tokens_expires_at", "expires_at"),)


class OAuthAuthorizationCodeModel(Base):
    """OAuth 2.0 authorization codes for PKCE flow."""

    __tablename__ = "oauth_authorization_codes"

    code = Column(String(64), primary_key=True)
    # Phase 9 audit fix: width normalization VARCHAR(255)→VARCHAR(36) per Phase 4 policy.
    user_id = Column(String(36), nullable=False)
    redirect_uri = Column(Text, nullable=False)
    code_challenge = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, server_default="0", nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )

    __table_args__ = (Index("idx_auth_codes_expires_at", "expires_at"),)


# ============================================================
# Session Model: REMOVED in storage redesign 2026-04 phase 3.
#
# Per case-and-session-concepts.md v2.1, sessions are Redis-only
# (RedisSessionStore over real Redis in cloud and FakeRedis in
# local). The SQL `sessions` table was an unused artifact and a
# documented anti-pattern. The corresponding cases.session_id FK
# column was deleted at the same time (Anti-Pattern 1: cases must
# not bind to sessions). See deployment-schema-strategy.md §11.1
# + §8.1 + §12 decisions #14, #15.
# ============================================================


# ============================================================
# Enums (Python-side - maps to DB enums or VARCHAR)
# ============================================================


class CaseStatusEnum(str, enum.Enum):
    """Case lifecycle status (phases and dispositions)."""

    INQUIRY = "inquiry"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


# ------------------------------------------------------------------
# Storage redesign 2026-04 — Phase 5: enum reconciliation.
#
# The following ORM enum classes were deleted because they duplicated or
# collided with domain enums. Per deployment-schema-strategy.md §3.3 +
# §12 decision #20, there is one authoritative enum per concept and the
# domain enum wins.
#
# - EvidenceCategoryEnum  → was misleadingly named (its values described
#                           data form, not investigation category).
#                           The evidence.category column already stores
#                           domain EvidenceCategory string values
#                           (symptom_evidence | causal_evidence | …).
#                           A separate evidence.form column bound to a
#                           renamed EvidenceFormEnum is planned for
#                           Phase 6.
# - HypothesisStatusEnum  → had drifted values
#                           (proposed/testing/validated/invalidated/deferred);
#                           domain HypothesisStatus
#                           (captured/active/validated/refuted/inconclusive/
#                           retired) is now authoritative.
# - SolutionStatusEnum    → identical value set to domain SolutionStatus;
#                           was redundant.
#
# The columns that previously bound to these classes
# (evidence.category, hypotheses.status, solutions.status) remain plain
# String columns and store the corresponding domain enum string values.
# No native PG enum types are used (Tier 2 decision: enum binding via
# CHECK + String is dialect-agnostic).
# ------------------------------------------------------------------


class EvidenceFormEnum(str, enum.Enum):
    """Data-form classification for uploaded evidence content.

    Replaces the old (misnamed) EvidenceCategoryEnum which was deleted
    in Phase 5. Values describe what the file CONTAINS (data shape),
    not the investigation role (which is `evidence.category` bound to
    domain `EvidenceCategory`).
    """

    TEXT = "text"
    IMAGE = "image"
    METRIC = "metric"
    STRUCTURED = "structured"


class MessageRoleEnum(str, enum.Enum):
    """Message role in conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# ToolCallStatusEnum was deleted in storage redesign 2026-04 phase 9 (audit fixes).
# It had zero callers outside its own definition AND its values disagreed with the
# CHECK constraint on agent_tool_calls.status (which uses domain values:
# pending|running|success|failed; old enum had ERROR="error" which the CHECK rejected).
# Tool call status values come from the domain layer (modules/case/domain/owned_models/
# agent_execution.py uses "success"/"failed"); the CHECK constraint enforces them.


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
    case_id = Column(String(36), primary_key=True)

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
    escalation_state = Column(Text)
    documentation = Column(Text, default="{}")
    progress = Column(Text, default="{}")
    # Use case_metadata as attribute to avoid conflict with SQLAlchemy Base.metadata
    case_metadata = Column("metadata", Text, default="{}")

    # Organization/Team — tenant scoping (Phase 9 audit fix: NOT NULL + FK).
    # organization_id is the basis for Phase 8 RLS (PG); RLS cannot enforce on
    # a nullable, unconstrained column. Default value is the single-org
    # local-deployment org id (matches startup bootstrap).
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    # team_id is optional (cases need not be team-scoped) but if set must
    # reference a real team.
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Archival (independent of case status)
    is_archived = Column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )
    archived_at = Column(DateTime(timezone=True), nullable=True)

    # Storage redesign 2026-04 — Phase 6 Tier 1 column additions.
    # Per deployment-schema-strategy.md §7.1 + §12 decision #19: lift terminal
    # state metadata out of the JSON `metadata` blob into first-class columns.
    # Domain `Case` already exposes these fields; the writers (postgresql/sqlite
    # repos) populate them directly when the case transitions to a terminal
    # status. `last_activity_at` is updated on every save.
    closure_reason = Column(String(100), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True, index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # cases.session_id (and the related `session` relationship) was removed
    # in storage redesign 2026-04 phase 3. Per case-and-session-concepts.md
    # v2.1, cases do not bind to sessions (Anti-Pattern 1). Sessions are
    # Redis-only. See deployment-schema-strategy.md §8.1 / §12 decision #15.

    # Relationships
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
    case_actions = relationship(
        "CaseActionModel", back_populates="case", cascade="all, delete-orphan"
    )
    tags = relationship(
        "CaseTagModel", back_populates="case", cascade="all, delete-orphan"
    )
    # evidence_artifacts relationship removed in storage redesign 2026-04
    # phase 2 (standalone evidence path deletion).

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
    - Unique constraint prevents duplicate uploads per case (content_hash)
    - New source_type field uses simplified 5-value enum (logs, metrics, configuration, visual, user_description)
    """

    __tablename__ = "evidence"

    evidence_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )

    # Classification fields
    category = Column(String(50), nullable=False, index=True)
    # DataType field (simplified 5 values: logs, metrics, configuration, image, text)
    source_type = Column(String(50), nullable=True)

    # Content fields
    summary = Column(String(500), nullable=False)
    preprocessed_content = Column(Text, nullable=False)
    content_ref = Column(String(1000))
    file_size = Column(BigInteger, nullable=False, server_default="0")
    filename = Column(String(255))

    # Deduplication and turn tracking (NEW in redesign)
    content_hash = Column(String(64), nullable=True, index=True)
    collected_at_turn = Column(Integer, nullable=True, index=True)

    # Source file linkage (Gap #20: Unified Data Processing)
    source_file_id = Column(String(36), nullable=True)

    # Timestamps
    upload_timestamp = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    evidence_metadata = Column("metadata", Text, default="{}")

    # Storage redesign 2026-04 — Phase 6 Tier 1 column additions.
    # Per deployment-schema-strategy.md §7.1 (evidence target shape) +
    # §12 decision #19. Tier 2 PG-only enhancements (CHECK on
    # reliability_score, TEXT[]+GIN on tags) are deferred to Phase 7.
    # Phase 9 audit fix: bind to EvidenceFormEnum via SQLAlchemy Enum so the
    # value space is enforced cross-dialect (native_enum=False emits a CHECK
    # constraint that works on both SQLite and PostgreSQL — complements the
    # Phase 7 PG-only CHECK).
    form = Column(
        Enum(
            EvidenceFormEnum,
            name="evidence_form",
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        server_default="text",
    )
    is_primary = Column(Boolean, nullable=False, server_default="0")
    content_type = Column(String(100), nullable=True)
    reliability_score = Column(Float, nullable=True)
    tags = Column(Text, nullable=True)

    # Lifecycle state: set to True by the investigation engine once this
    # evidence's structural index has been persisted into the case vector
    # store. Persisting the flag prevents cross-turn re-vectorization of the
    # same file, which otherwise stacks concurrent BGE-M3 encodes and drives
    # each past the 60s wait_for bound in _vectorize_evidence.
    vectorized = Column(Boolean, nullable=False, server_default="0")

    # Phase 3 — Case-level timeline. See migration
    # ``20260423_1200_c3d4e5f6a708_phase_3_evidence_coverage_timestamps``
    # and docs/working/WIP-data-processing-improvement-plan.md §Phase 3.
    # The time span the evidence's *content* covers, distinct from
    # upload_timestamp (receipt) and collected_at_turn (agent turn).
    # Nullable — evidence without parseable timestamps (configs, code,
    # screenshots, short pastes) has both columns NULL.
    coverage_start_ts = Column(DateTime(timezone=True), nullable=True)
    coverage_end_ts = Column(DateTime(timezone=True), nullable=True)

    # Relationship
    case = relationship("CaseModel", back_populates="evidence")

    __table_args__ = (
        # Data validation constraints
        CheckConstraint("LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"),
        CheckConstraint(
            "LENGTH(TRIM(preprocessed_content)) > 0", name="evidence_content_not_empty"
        ),
        # Composite index for "primary evidence per case" lookups
        # (consumed by list_evidence_tool). Added Phase 6 Tier 1.
        Index("ix_evidence_case_is_primary", "case_id", "is_primary"),
        # Phase 3 — case-level timeline index. Supports the query
        # "all evidence in this case whose coverage intersects [start, end]".
        # Case-id-prefixed so the index narrows cheaply before the overlap
        # check runs.
        Index(
            "idx_evidence_coverage", "case_id", "coverage_start_ts", "coverage_end_ts"
        ),
        # Unique constraints (via indexes for SQLite compatibility)
        # Note: These are implemented as unique indexes in the migration
        # uq_evidence_case_hash - no duplicate uploads per case
        # Note: uq_evidence_case_turn removed (Gap #20) - multiple evidence per turn allowed
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

    hypothesis_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    statement = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="captured", index=True)
    likelihood = Column(Numeric(3, 2), default=0.5)
    initial_likelihood = Column(Numeric(3, 2), default=0.5)

    # Tracking
    generated_at_turn = Column(Integer, nullable=False, default=0, server_default="0")
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
        String(36),
        ForeignKey("organizations.organization_id"),
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

    solution_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    solution_type = Column(String(30), default="other")
    title = Column(String(500), default="Untitled solution")
    description = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="proposed", index=True)
    immediate_action = Column(Text)
    longterm_fix = Column(Text)
    implementation_steps = Column(Text)  # JSON array as TEXT
    commands = Column(Text)  # JSON array as TEXT
    risks = Column(Text)  # JSON array as TEXT
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
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    created_by = Column(String(255), nullable=False, index=True)
    updated_by = Column(String(255), nullable=True)

    # Storage redesign 2026-04 — Phase 6 Tier 1.
    # Optional link to the hypothesis this solution was generated to address.
    # Nullable: fast-track resolutions skip hypothesis formulation entirely,
    # so not every solution has a parent hypothesis.
    hypothesis_id = Column(
        String(36),
        ForeignKey("hypotheses.hypothesis_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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

    message_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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

    file_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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


class CaseActionModel(Base):
    """Case action (phase transition / disposition) audit trail."""

    __tablename__ = "case_actions"

    transition_id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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
    case = relationship("CaseModel", back_populates="case_actions")

    def __repr__(self) -> str:
        return f"<CaseActionModel(from={self.from_status}, to={self.to_status})>"


# Backward compatibility alias
CaseStatusTransitionModel = CaseActionModel


# ============================================================
# Case Tag Model
# ============================================================


class CaseTagModel(Base):
    """Case tags for categorization."""

    __tablename__ = "case_tags"

    tag_id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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
    checkpoint_id = Column(String(36), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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
    execution_id = Column(String(36), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Agent identification
    organization_id = Column(
        String(36),
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
        String(36),
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
    tool_calls = relationship(
        "AgentToolCallModel",
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
# Agent Tool Call Model (Execution-level)
# ============================================================


class AgentToolCallModel(Base):
    """Tool call tracking for agent executions.

    Tracks individual tool invocations made during an agent execution,
    including input, output, status, and timing information. Linked to
    agent_executions via execution_id FK.
    """

    __tablename__ = "agent_tool_calls"

    # Primary Key
    tool_call_id = Column(String(36), primary_key=True)

    # Foreign Key to agent_executions
    execution_id = Column(
        String(36),
        ForeignKey("agent_executions.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Tool information
    organization_id = Column(
        String(36),
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
    execution = relationship("AgentExecutionModel", back_populates="tool_calls")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed')",
            name="agent_tool_calls_status_check",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="agent_tool_calls_duration_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentToolCallModel(tool_call_id={self.tool_call_id}, "
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
    session_id = Column(String(36), primary_key=True)

    # Foreign Key to cases
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ownership
    user_id = Column(String(36), nullable=False, index=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
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
    item_id = Column(String(36), primary_key=True)

    # Organization scope (NO foreign key - items persist independently)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    scope = Column(String(20), nullable=False, default="global", index=True)
    owner_id = Column(String(36), nullable=True, index=True)
    team_id = Column(String(36), nullable=True, index=True)

    # Content
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    item_type = Column(String(64), nullable=False, index=True)

    # Categorization
    category = Column(String(128), nullable=True, index=True)
    tags = Column(Text, nullable=False, default="[]")  # JSON array

    # Vector search
    embedding_model = Column(String(128), nullable=False, default="bge-m3")
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
    # Phase 9 audit fix: VARCHAR(64)→VARCHAR(36) per Phase 4 width policy.
    verified_by = Column(String(36), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    # Lineage tracking (for suggestions that became knowledge items)
    source_suggestion_id = Column(String(36), nullable=True, index=True)

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
            "scope IN ('personal', 'team', 'global')",
            name="knowledge_items_scope_check",
        ),
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
    suggestion_id = Column(String(36), primary_key=True)

    # Organization and Case scope
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    case_id = Column(String(36), nullable=False, index=True)

    # Status
    status = Column(
        String(32), nullable=False, default="pending_review", index=True
    )  # pending_review, approved, rejected, draft

    # Suggested content
    suggested_title = Column(String(512), nullable=False)
    suggested_content = Column(Text, nullable=False)
    suggested_type = Column(String(64), nullable=False, default="troubleshooting_guide")

    # Extraction metadata. Phase 9 audit fix: VARCHAR(64)→VARCHAR(36) per Phase 4
    # policy (entity user_id reference).
    extracted_by = Column(String(36), nullable=False, index=True)
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
    # Phase 9 audit fix: VARCHAR(64)→VARCHAR(36) per Phase 4 width policy.
    pii_remediated_by = Column(String(36), nullable=True)
    pii_remediated_at = Column(DateTime(timezone=True), nullable=True)

    # Lineage (for Review Inbox footer)
    source_case_title = Column(String(512), nullable=True)
    message_count = Column(Integer, nullable=False, default=0)
    evidence_count = Column(Integer, nullable=False, default=0)

    # Review metadata
    # Phase 9 audit fix: VARCHAR(64)→VARCHAR(36) per Phase 4 width policy.
    reviewed_by = Column(String(36), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    # Bidirectional link to KnowledgeItem (when approved)
    knowledge_item_id = Column(String(36), nullable=True, index=True)

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


# ============================================================
# LLM Configuration Overrides (Dashboard Phase 1a)
# ============================================================


class LLMConfigOverrideModel(Base):
    """Key-value overrides for LLM configuration.

    Stores dashboard-applied configuration changes that take precedence
    over environment variables. This allows users to configure LLM
    providers through the dashboard without editing .env files.

    Keys follow the settings field naming convention:
    - "primary_provider"   → overrides CHAT_PROVIDER
    - "strict_provider_mode" → overrides STRICT_PROVIDER_MODE
    - "anthropic_api_key"  → overrides ANTHROPIC_API_KEY
    - "openai_api_key"     → overrides OPENAI_API_KEY
    - etc.
    """

    __tablename__ = "llm_config_overrides"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = Column(String(255), nullable=True)

    def __repr__(self) -> str:
        # Never log the value — it may contain API keys
        return f"<LLMConfigOverride(key={self.key}, updated_at={self.updated_at})>"


# ============================================================
# Document-to-Runbook Conversion Models
# ============================================================


class ConversionJobModel(Base):
    """Tracks a document-to-runbook conversion job."""

    __tablename__ = "conversion_jobs"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    # Phase 9 audit fix: NOT NULL + FK + default for tenant scoping.
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
        default="00000000-0000-0000-0000-000000000001",
    )
    scope = Column(String(20), nullable=False)
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(20), nullable=False, server_default="processing")
    source_filename = Column(String(255), nullable=False)
    source_content_type = Column(String(100), nullable=False)
    source_size_bytes = Column(Integer, nullable=False)
    source_path = Column(String(500), nullable=False)
    source_type = Column(String(20), nullable=False, server_default="document")
    case_id = Column(String(36), nullable=True, index=True)
    failure_modes_detected = Column(Integer, nullable=False, server_default="0")
    analysis_result = Column(JSON, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    drafts = relationship(
        "ConversionDraftModel",
        back_populates="conversion_job",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<ConversionJob(id={self.id}, status={self.status}, "
            f"source={self.source_filename})>"
        )


class ReportModel(Base):
    """Generated case documentation report (incident reports, runbooks, post-mortems).

    Versioned, persistent storage linked to cases via FK.
    Repository layer uses raw SQL for SQLite compatibility, but this ORM model
    ensures the table is created by Alembic and available for future ORM queries.
    """

    __tablename__ = "reports"

    report_id = Column(String(36), primary_key=True)
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_type = Column(
        String(30), nullable=False
    )  # resolution_summary | closure_summary | runbook
    version = Column(Integer, nullable=False, server_default="1")
    is_current = Column(Boolean, nullable=False, server_default="1")
    linked_to_closure = Column(Boolean, nullable=False, server_default="0")
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    format = Column(String(20), nullable=False, server_default="markdown")
    generation_status = Column(
        String(20), nullable=False
    )  # generating | completed | failed
    generation_time_ms = Column(Integer, nullable=False)
    report_metadata = Column("metadata", JSON, nullable=True)  # RunbookMetadata as JSON
    generated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Storage redesign 2026-04 — Phase 6 Tier 1.
    # Stores user_id of the user who triggered report generation, or "system"
    # for auto-generated reports (e.g., on case resolution / closure).
    generated_by = Column(String(36), nullable=True)

    # Relationships
    case = relationship("CaseModel", backref="reports")

    __table_args__ = (
        Index("idx_reports_type_version", "case_id", "report_type"),
        CheckConstraint("version >= 1 AND version <= 5", name="reports_version_check"),
        CheckConstraint(
            "generation_time_ms >= 0 AND generation_time_ms <= 120000",
            name="reports_gen_time_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Report(id={self.report_id}, case={self.case_id}, "
            f"type={self.report_type}, v{self.version})>"
        )


class ConversionDraftModel(Base):
    """Individual runbook draft generated from a conversion job."""

    __tablename__ = "conversion_drafts"

    id = Column(String(36), primary_key=True)
    conversion_id = Column(
        String(36),
        ForeignKey("conversion_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runbook_id = Column(String(100), nullable=False)
    title = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, server_default="draft")
    source_type = Column(String(20), nullable=False, server_default="document")
    validation_passed = Column(Boolean, nullable=False, server_default="1")
    validation_errors = Column(JSON, nullable=True)
    validation_warnings = Column(JSON, nullable=True)
    quality_score = Column(Numeric(5, 1), nullable=True)
    quality_details = Column(JSON, nullable=True)
    knowledge_item_id = Column(String(36), nullable=True)
    # KB metadata — populated from frontmatter during scan/verify
    domain = Column(String(50), nullable=True)
    service = Column(String(100), nullable=True)
    severity = Column(String(20), nullable=True)
    tags = Column(Text, nullable=True)  # JSON array or comma-separated
    document_type = Column(String(50), nullable=True, server_default="runbook")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(String(36), nullable=True)

    conversion_job = relationship("ConversionJobModel", back_populates="drafts")

    def __repr__(self) -> str:
        return (
            f"<ConversionDraft(id={self.id}, runbook_id={self.runbook_id}, "
            f"status={self.status})>"
        )
