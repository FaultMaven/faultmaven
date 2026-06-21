"""SQLAlchemy ORM models for FaultMaven persistence layer.

Schema is organized into four domains:

- **User domain** — `enterprises`, `users`, `organizations`, `teams` and their
  membership/audit/auth tables. Three-tier tenancy: enterprises (corporate
  umbrella) → organizations (workspaces, hard isolation boundary) → teams
  (routing buckets).
- **Case domain** — `cases` and its children: evidence, hypotheses, solutions,
  messages, files, actions, tags, checkpoints, entities, sessions, agent
  executions, tool calls, hypothesis-evidence junction, reports.
- **Knowledge domain** — `knowledge_items` (RAG corpus), `knowledge_suggestions`
  (HITL pipeline from cases), `conversion_jobs` / `conversion_drafts`
  (document-to-runbook).
- **Config domain** — `llm_config_overrides` (dashboard hot-reloaded settings).

Conventions:

- Primary keys: VARCHAR(36) UUIDs, named `<entity>_id`.
- Audit columns: `created_at`, `updated_at` (server-default `now()`).
- `*_by` audit columns are real FKs to `users.user_id` with `ON DELETE SET NULL`.
- `organization_id` is denormalized onto every case-domain child table for fast
  RLS without JOINs; `ON DELETE CASCADE` so workspace deletion clears child rows.
- `case_id` ON DELETE policy splits by table role:
  * Lifecycle-side (evidence, hypotheses, messages, etc.): `CASCADE` — die with the case.
  * Permanence-side (knowledge_suggestions, conversion_jobs): `SET NULL` — survive case deletion.
- JSON columns use `Text` for cross-dialect compatibility (PG promotes to JSONB
  via dialect-specific migration; SQLite stores as TEXT).
- Tags: PG-only `TEXT[]` + GIN; SQLite uses comma-separated TEXT with a CHECK
  forbidding commas in tag values, so the format round-trips.
- Soft delete: `deleted_at` exists only on `users`, `organizations`, `teams`,
  `enterprises` (account-style entities). Cases and their children use hard
  delete with FK CASCADE.
"""

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import JSON, TypeDecorator

Base = declarative_base()


# ============================================================
# Enums (Python-side; stored as VARCHAR with CHECK constraints)
# ============================================================


class CaseState(str, enum.Enum):
    INQUIRY = "inquiry"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


class HypothesisState(str, enum.Enum):
    CAPTURED = "captured"
    ACTIVE = "active"
    VALIDATED = "validated"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    RETIRED = "retired"


class SolutionState(str, enum.Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"


class KnowledgeScope(str, enum.Enum):
    PERSONAL = "personal"
    TEAM = "team"
    ORGANIZATION = "organization"
    GLOBAL = "global"


class KnowledgeItemType(str, enum.Enum):
    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_TEMPLATE = "solution_template"
    API_DOCUMENTATION = "api_documentation"
    CONFIGURATION_GUIDE = "configuration_guide"
    BEST_PRACTICE = "best_practice"
    FAQ = "faq"
    RUNBOOK = "runbook"


class ReportType(str, enum.Enum):
    """Case-summary documents. Runbooks live in `knowledge_items`, not here."""

    RESOLUTION_SUMMARY = "resolution_summary"
    CLOSURE_SUMMARY = "closure_summary"


class ReportFormat(str, enum.Enum):
    MARKDOWN = "markdown"
    HTML = "html"


class ReportStatus(str, enum.Enum):
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentExecutionStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class AgentType(str, enum.Enum):
    INVESTIGATOR = "investigator"
    DEBUGGER = "debugger"
    RESEARCHER = "researcher"
    VALIDATOR = "validator"
    REPORTER = "reporter"
    CUSTOM = "custom"


class ToolCallStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class InvestigationSessionState(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class RoleScope(str, enum.Enum):
    SYSTEM = "system"
    ENTERPRISE = "enterprise"
    ORGANIZATION = "organization"
    TEAM = "team"


class PiiScanStatus(str, enum.Enum):
    NOT_SCANNED = "not_scanned"
    SCANNING = "scanning"
    CLEAN = "clean"
    PII_DETECTED = "pii_detected"
    REMEDIATED = "remediated"
    SCAN_FAILED = "scan_failed"


# Tags: cross-dialect column type. PG uses native VARCHAR(50)[] + GIN
# (the GIN index is declared per-table via postgresql_using="gin"); SQLite
# uses comma-separated TEXT. The Pydantic-side comma-ban validator on tag
# values keeps the round-trip lossless.
#
# Implemented as a TypeDecorator so ORM-mode repositories (e.g. the
# knowledge module's KnowledgeItemRepository) can pass a Python list[str]
# directly without manual serialization. Raw-SQL repos (the case-domain
# SQLite + PG hybrid repos) bypass the decorator and continue to bind
# pre-serialized values via their own _serialize_tags helpers — that's
# fine because TypeDecorator only intercepts ORM-level binds, not raw
# `text(...)` parameter binding.
class _TagsArrayType(TypeDecorator):
    """Cross-dialect Python list[str] <-> column storage."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(String(50)))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError(f"tags must be a list of str, got {type(value).__name__}")
        if dialect.name == "postgresql":
            # asyncpg/psycopg bind list directly to text[]
            return value if value else None
        # SQLite: comma-separated TEXT; the no-comma validator on the
        # domain side keeps this round-trip lossless.
        return ",".join(value) if value else None

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return []
        if dialect.name == "postgresql":
            return list(value)
        # SQLite: comma-separated TEXT
        return [t for t in value.split(",") if t]


TagsArray = _TagsArrayType()

# JSON blob: cross-dialect column type for JSON-shaped data (metadata,
# domain-state JSON, etc.). PG promotes to JSONB for fast key lookups and
# expression indexes; SQLite keeps it as TEXT and parses in application
# code. Use this in place of `Text` whenever the column's contents are
# `json.dumps()`-encoded structured data, not free-form prose.
JsonBlob = Text().with_variant(JSONB, "postgresql")


# ============================================================
# User Domain
# ============================================================


class EnterpriseModel(Base):
    """Top-tier tenant: corporate umbrella. Holds SSO/SAML config, billing, and
    enterprise-wide knowledge scope. Enterprises contain organizations."""

    __tablename__ = "enterprises"

    enterprise_id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, unique=True)
    plan_tier = Column(String(20), nullable=False, server_default="free")
    max_members = Column(Integer, nullable=False, server_default="5")
    max_cases = Column(Integer, nullable=True)
    billing_email = Column(String(255), nullable=True)
    settings = Column(JsonBlob, nullable=True)  # JSON: SSO config, feature flags, etc.
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_enterprises_slug", "slug", unique=True),
        CheckConstraint("LENGTH(TRIM(name)) > 0", name="enterprises_name_not_empty"),
        CheckConstraint(
            "plan_tier IN ('free', 'starter', 'pro', 'enterprise')",
            name="enterprises_plan_tier_check",
        ),
    )


class UserModel(Base):
    """User account. Belongs to one enterprise; can be a member of multiple
    organizations within that enterprise via `organization_members`."""

    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True)
    # Every user belongs to exactly one enterprise. In single-tenant mode
    # this is the default enterprise seeded by SingleTenantProvider /
    # migration 006; in multi-tenant mode, the OAuth flow populates it.
    enterprise_id = Column(
        String(36),
        ForeignKey("enterprises.enterprise_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_password_change_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Local/dev-mode role storage. Cloud mode derives roles from RBAC tables
    # (organization_members → roles). Stores a JSON array, e.g. '["user","admin"]'.
    dev_roles = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("sso_provider", "sso_provider_id", name="users_sso_unique"),
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_username", "username", unique=True),
        Index("ix_users_is_active", "is_active"),
        CheckConstraint(
            "LENGTH(TRIM(display_name)) > 0", name="users_display_name_not_empty"
        ),
        # NOTE: a "(hashed_password IS NOT NULL) OR (sso_provider IS NOT NULL)"
        # CHECK was previously declared here. It blocked the intentional
        # passwordless dev-login flow (local mode), which creates users
        # with both fields NULL. Credential-required-for-login is enforced
        # at the auth-endpoint layer via AUTH_MODE, not at the table.
        # Dropped in migration 007.
    )


class OrganizationModel(Base):
    """Workspace: hard data-isolation boundary. Cases never cross organization
    lines; PostgreSQL RLS enforces this. An organization is owned by an
    enterprise and contains teams."""

    __tablename__ = "organizations"

    organization_id = Column(String(36), primary_key=True)
    # Every organization is owned by an enterprise (Enterprise > Org > Team).
    enterprise_id = Column(
        String(36),
        ForeignKey("enterprises.enterprise_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = Column(Boolean, nullable=False, server_default="1")
    settings = Column(JsonBlob, nullable=True)  # JSON
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "slug", name="organizations_slug_unique_per_enterprise"
        ),
        CheckConstraint("LENGTH(TRIM(name)) > 0", name="organizations_name_not_empty"),
        CheckConstraint("LENGTH(slug) > 0", name="organizations_slug_not_empty"),
    )


class OrganizationMemberModel(Base):
    """User ↔ organization membership. Composite PK; one role per (user, org)."""

    __tablename__ = "organization_members"

    user_id = Column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id = Column(
        String(36),
        ForeignKey("roles.role_id", ondelete="RESTRICT"),
        nullable=False,
    )
    invited_by = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    invited_at = Column(DateTime(timezone=True), nullable=True)
    invitation_accepted_at = Column(DateTime(timezone=True), nullable=True)
    joined_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_org_members_organization_id", "organization_id"),
        Index("ix_org_members_role_id", "role_id"),
    )


class TeamModel(Base):
    """Sub-group within an organization for case routing & collaboration."""

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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", name="teams_organization_name_unique"
        ),
        Index("ix_teams_organization_id", "organization_id"),
        CheckConstraint("LENGTH(TRIM(name)) > 0", name="teams_name_not_empty"),
    )


class TeamMemberModel(Base):
    """User ↔ team membership. Composite PK."""

    __tablename__ = "team_members"

    user_id = Column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    team_id = Column(
        String(36), ForeignKey("teams.team_id", ondelete="CASCADE"), primary_key=True
    )
    team_role = Column(String(50), nullable=True)
    joined_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_team_members_team_id", "team_id"),)


class RoleModel(Base):
    """RBAC role. `scope` indicates the level at which the role grants access:
    system-wide, enterprise-wide, organization-wide, or team-wide."""

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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('system', 'enterprise', 'organization', 'team')",
            name="roles_scope_check",
        ),
    )


class PermissionModel(Base):
    """RBAC permission: a (resource, action) pair like ('cases', 'read')."""

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


class RolePermissionModel(Base):
    """Role-to-permission junction."""

    __tablename__ = "role_permissions"

    role_id = Column(
        String(36), ForeignKey("roles.role_id", ondelete="CASCADE"), primary_key=True
    )
    permission_id = Column(
        String(36),
        ForeignKey("permissions.permission_id", ondelete="CASCADE"),
        primary_key=True,
    )


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
    """Revoked JWT tokens for invalidation tracking."""

    __tablename__ = "oauth_revoked_tokens"

    jti = Column(String(64), primary_key=True)
    revoked_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_revoked_tokens_expires_at", "expires_at"),)


class OAuthAuthorizationCodeModel(Base):
    """OAuth 2.0 PKCE authorization codes."""

    __tablename__ = "oauth_authorization_codes"

    code = Column(String(64), primary_key=True)
    user_id = Column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    redirect_uri = Column(Text, nullable=False)
    code_challenge = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_auth_codes_expires_at", "expires_at"),)


# ============================================================
# Case Domain
# ============================================================


class CaseModel(Base):
    """Investigation case. Aggregate root of the case domain."""

    __tablename__ = "cases"

    case_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, server_default="")
    state = Column(String(50), nullable=False, server_default="inquiry", index=True)

    # Investigation state (first-class columns; drive milestone engine logic)
    investigation_strategy = Column(Text, nullable=True)
    current_turn = Column(Integer, nullable=False, server_default="0")
    turns_without_progress = Column(Integer, nullable=False, server_default="0")

    # Optimistic concurrency control
    version = Column(Integer, nullable=False, server_default="1")

    # Terminal state
    closure_reason = Column(String(100), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True, index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Denormalized per-disposition eligibility view (JSON text), maintained
    # at CaseRepository.save() chokepoint via derive_disposition_eligibility.
    # Shape: {"resolved": "ready|needs_info|not_eligible",
    #         "closed":   "ready|needs_info|not_eligible"}
    # Read by the UI adapter to gate dropdown affordances on case content.
    disposition_eligibility = Column(Text, nullable=True)

    # JSON blobs for complex domain objects (not query targets)
    inquiry = Column(JsonBlob, nullable=False, server_default="{}")
    problem_verification = Column(JsonBlob, nullable=True)
    working_conclusion = Column(JsonBlob, nullable=True)
    root_cause_conclusion = Column(JsonBlob, nullable=True)
    escalation_state = Column(JsonBlob, nullable=True)
    documentation = Column(JsonBlob, nullable=False, server_default="{}")
    progress = Column(JsonBlob, nullable=False, server_default="{}")
    case_metadata = Column("metadata", JsonBlob, nullable=False, server_default="{}")

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    evidence = relationship(
        "EvidenceModel", back_populates="case", cascade="all, delete-orphan"
    )
    evidence_needs = relationship(
        "EvidenceNeedModel", back_populates="case", cascade="all, delete-orphan"
    )
    hypotheses = relationship(
        "HypothesisModel", back_populates="case", cascade="all, delete-orphan"
    )
    solutions = relationship(
        "SolutionModel", back_populates="case", cascade="all, delete-orphan"
    )
    causal_nodes = relationship(
        "CausalNodeModel", back_populates="case", cascade="all, delete-orphan"
    )
    messages = relationship(
        "CaseMessageModel", back_populates="case", cascade="all, delete-orphan"
    )
    uploaded_files = relationship(
        "UploadedFileModel", back_populates="case", cascade="all, delete-orphan"
    )
    actions = relationship(
        "CaseActionModel", back_populates="case", cascade="all, delete-orphan"
    )
    tags = relationship(
        "CaseTagModel", back_populates="case", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(title)) > 0", name="cases_title_not_empty"),
        CheckConstraint(
            "state IN ('inquiry', 'investigating', 'resolved', 'closed')",
            name="cases_state_check",
        ),
        # Description = problem statement. INVESTIGATING and RESOLVED both
        # require the problem to be articulated:
        #   - INVESTIGATING: entry gate; you can't investigate without a problem.
        #   - RESOLVED: a resolved case must have a known problem (per the
        #     transitions spec, RESOLVED only comes from INVESTIGATING which
        #     already required it; this CHECK is defense-in-depth).
        # INQUIRY allows empty (still being formulated). CLOSED allows empty
        # because the inquiry → closed early-abandon path is legitimate; if
        # the case went through INVESTIGATING first, description is non-empty
        # via that gate's enforcement.
        CheckConstraint(
            "state IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0",
            name="cases_description_required_for_investigation",
        ),
        CheckConstraint("current_turn >= 0", name="cases_current_turn_nonnegative"),
        CheckConstraint(
            "turns_without_progress >= 0",
            name="cases_turns_without_progress_nonnegative",
        ),
        CheckConstraint("version >= 1", name="cases_version_positive"),
    )


class UploadedFileModel(Base):
    """File uploaded to the system. `case_id` is nullable: case evidence
    uploads carry a case_id; KB conversion uploads (`POST /knowledge/convert`)
    do not. `content_hash` enables file-level dedup at the storage backend."""

    __tablename__ = "uploaded_files"

    file_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    uploaded_by = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    filename = Column(String(255), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    content_type = Column(String(100), nullable=True)  # MIME type
    content_hash = Column(String(64), nullable=True, index=True)
    # Opaque key passed to the file-storage backend (local FS path, S3 key,
    # Azure blob name, etc.). The backend interprets it; nothing else does.
    storage_ref = Column(String(1000), nullable=True)
    # Provenance: how this file got into the system. Distinct from the
    # data-classification field on `evidence.source_type`. Values:
    # `file_upload`, `conversion_source`, `api_push`, etc.
    upload_source = Column(String(50), nullable=False, server_default="file_upload")
    uploaded_at_turn = Column(Integer, nullable=False, server_default="0")
    file_metadata = Column("metadata", JsonBlob, nullable=False, server_default="{}")

    # Preprocessing artifacts — landed here after migration 010 collapsed
    # the dual evidence-creation paths. Previously these lived on an
    # auto-DOCUMENT Evidence row that was created at upload time. They
    # describe the FILE, not any claim about it, so they belong with the
    # file. All nullable: preprocessing may not run (e.g., on KB-conversion
    # source uploads), and small/short files may have no temporal coverage.
    summary = Column(Text, nullable=True)
    structural_index = Column(Text, nullable=True)
    data_type = Column(String(50), nullable=True)
    coverage_start_ts = Column(DateTime(timezone=True), nullable=True)
    coverage_end_ts = Column(DateTime(timezone=True), nullable=True)

    uploaded_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    case = relationship("CaseModel", back_populates="uploaded_files")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(filename)) > 0", name="uploaded_files_filename_not_empty"
        ),
        CheckConstraint("size_bytes >= 0", name="uploaded_files_size_nonnegative"),
        CheckConstraint(
            "uploaded_at_turn >= 0", name="uploaded_files_turn_nonnegative"
        ),
    )


class EvidenceModel(Base):
    """Evidence collected during a case. One row per claim-anchored extract
    that the LLM produced via ``evidence_to_add`` during INVESTIGATING.

    Post-010 semantics (single creation path):
    - ``summary``: always-present short label for the extract
    - ``extract``: optional verbatim quote (the focused slice supporting
      the claim); may be NULL when the summary is self-contained
    - ``source_file_id``: links to the source file in ``uploaded_files``;
      NULL only when the extract was a verbatim system-output quote from
      the user's short chat message (``source_type='user_description'``).
      The ``evidence_source_invariant`` CHECK enforces this.

    Preprocessing artifacts (file summary, structural index, file-level
    data type, coverage timestamps) live on ``uploaded_files`` — they
    describe the file, not any claim about it."""

    __tablename__ = "evidence"

    evidence_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_file_id = Column(
        String(36),
        ForeignKey("uploaded_files.file_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Classification
    category = Column(String(50), nullable=False, index=True)
    source_type = Column(String(50), nullable=True)

    # What this evidence validates (milestone name or hypothesis ID).
    # Server default 'legacy' allows pre-009 rows to satisfy NOT NULL;
    # new writers must populate explicitly.
    primary_purpose = Column(String(100), nullable=False, server_default="legacy")

    # Free-form agent analysis of this evidence and its significance.
    analysis = Column(Text, nullable=True)

    # Processing mode used to extract this evidence:
    # triage | directed_analysis | semantic_search.
    processing_mode = Column(String(50), nullable=True)

    # Which milestones this evidence helped complete. TagsArray shape
    # (TEXT[] on PG, comma-encoded TEXT on SQLite); empty when none.
    advances_milestones = Column(TagsArray, nullable=True)

    # Two-field shape (see case-schema.md §4.3):
    # - summary: short label, ALWAYS present. The LLM writes this when it
    #   declares the evidence via evidence_to_add.
    # - extract: optional verbatim quote that supports the summary. NULL
    #   when the summary is self-contained.
    # File-level preprocessing artifacts (structural index, file summary)
    # live on uploaded_files, not here. source_file_id links to the source
    # file; storage_ref lives on uploaded_files.
    summary = Column(String(500), nullable=False)
    extract = Column(Text, nullable=True)

    # Investigation context
    is_primary = Column(Boolean, nullable=False, server_default="0")
    reliability_score = Column(Float, nullable=True)
    tags = Column(TagsArray, nullable=True)
    collected_at_turn = Column(Integer, nullable=True, index=True)
    coverage_start_ts = Column(DateTime(timezone=True), nullable=True)
    coverage_end_ts = Column(DateTime(timezone=True), nullable=True)

    # Lifecycle: True once vectorized into the case vector store.
    vectorized = Column(Boolean, nullable=False, server_default="0")

    # Who collected: user UUID or sentinel ('system' for automated
    # collection). Free-form VARCHAR per case_actions.triggered_by
    # precedent — value space is heterogeneous, FK would force
    # inventing sentinel users rows. Server default 'system' covers
    # pre-009 rows without losing them.
    collected_by = Column(String(50), nullable=False, server_default="system")

    evidence_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case = relationship("CaseModel", back_populates="evidence")

    __table_args__ = (
        CheckConstraint("LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"),
        # extract is nullable when the summary is self-contained. When
        # set, must not be whitespace-only — mirrored in Pydantic
        # Evidence._extract_not_empty_when_set for cross-layer defense.
        CheckConstraint(
            "extract IS NULL OR LENGTH(TRIM(extract)) > 0",
            name="evidence_extract_not_empty",
        ),
        # Source-tracking invariant (migration 010): every evidence row
        # has a source. A NULL source_file_id is permitted only when the
        # extract was a verbatim system-output quote from the user's
        # chat message — recognized via source_type='user_description'.
        CheckConstraint(
            "source_file_id IS NOT NULL OR source_type = 'user_description'",
            name="evidence_source_invariant",
        ),
        CheckConstraint(
            "reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)",
            name="evidence_reliability_range",
        ),
        Index("ix_evidence_case_is_primary", "case_id", "is_primary"),
        Index(
            "ix_evidence_coverage", "case_id", "coverage_start_ts", "coverage_end_ts"
        ),
        # PG: GIN over the TEXT[] tags column for fast membership queries.
        # SQLite: degrades to a btree on the TEXT column (harmless, unused).
        Index("ix_evidence_tags", "tags", postgresql_using="gin"),
    )


class EvidenceNeedModel(Base):
    """Demand-side counterpart to evidence rows: verification requirements
    the investigation has identified.

    Needs live in a flat pool on the case — not anchored to specific
    hypotheses. ``motivating_hypothesis_ids`` is stored as a JSON list
    (small, mutated as a unit, never queried by ID across cases) per
    evidence-needs-design.md §8.7. The hypothesis-evidence relationship
    proper is recorded through the existing ``hypothesis_evidence``
    junction at evidence-collection time.

    Lifecycle is LLM-driven (create / update / supersede via
    ``EvidenceNeedUpdate`` emissions); the engine enforces one
    deterministic rule (hypothesis-retirement supersession when the
    motivating list becomes empty AND purpose='causal_verification').

    See: docs/architecture/investigation-engine/evidence-needs-design.md
    """

    __tablename__ = "evidence_needs"

    need_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Demand-side classification (mirrors NeedPurpose enum).
    purpose = Column(String(30), nullable=False)

    # Surface-able text + LLM rationale for why this data would help.
    request_text = Column(String(500), nullable=False)
    rationale = Column(String(500), nullable=False)

    priority = Column(String(10), nullable=False, server_default="medium")
    state = Column(String(20), nullable=False, server_default="pending", index=True)

    # JSON list of hypothesis IDs that motivate this need. Empty list
    # means the need is motivated by the problem statement (symptom
    # needs). Stored as a blob — the list is small, never indexed, and
    # mutated as a unit by the engine on hypothesis-retirement events.
    motivating_hypothesis_ids = Column(JsonBlob, nullable=False, server_default="[]")

    # Required iff state='superseded'; NULL otherwise. Mirrors the
    # Pydantic model_validator on EvidenceNeed.
    superseded_reason = Column(String(500), nullable=True)

    created_at_turn = Column(Integer, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case = relationship("CaseModel", back_populates="evidence_needs")
    fulfillments = relationship(
        "EvidenceNeedFulfillmentModel",
        back_populates="need",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('symptom_verification', 'causal_verification')",
            name="evidence_needs_purpose_check",
        ),
        CheckConstraint(
            "priority IN ('high', 'medium', 'low')",
            name="evidence_needs_priority_check",
        ),
        CheckConstraint(
            "state IN ('pending', 'partially_met', 'fulfilled', 'superseded')",
            name="evidence_needs_state_check",
        ),
        # superseded_reason required iff state='superseded'. Two-way:
        # SUPERSEDED requires a reason, non-SUPERSEDED forbids it.
        CheckConstraint(
            "(state = 'superseded' AND superseded_reason IS NOT NULL)"
            " OR (state != 'superseded' AND superseded_reason IS NULL)",
            name="evidence_needs_superseded_reason_invariant",
        ),
        CheckConstraint(
            "LENGTH(TRIM(request_text)) > 0",
            name="evidence_needs_request_text_not_empty",
        ),
        CheckConstraint(
            "LENGTH(TRIM(rationale)) > 0",
            name="evidence_needs_rationale_not_empty",
        ),
        CheckConstraint(
            "created_at_turn >= 0",
            name="evidence_needs_created_at_turn_nonnegative",
        ),
        Index("ix_evidence_needs_case_state", "case_id", "state"),
        Index("ix_evidence_needs_case_purpose", "case_id", "purpose"),
    )


class EvidenceNeedFulfillmentModel(Base):
    """Junction: evidence_need ↔ evidence row.

    Records which evidence rows fulfill which needs. Many-to-many:
    a single need accumulates multiple fulfillments across DIAGNOSIS
    (presence evidence) and MITIGATION/TREATMENT (absence evidence);
    a single evidence row can fulfill multiple needs when the same
    data answers multiple hypotheses (cross-hypothesis sharing per
    evidence-needs-design.md §5.2).
    """

    __tablename__ = "evidence_need_fulfillment"

    need_id = Column(
        String(36),
        ForeignKey("evidence_needs.need_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id = Column(
        String(36),
        ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    linked_at_turn = Column(Integer, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    need = relationship("EvidenceNeedModel", back_populates="fulfillments")

    __table_args__ = (
        CheckConstraint(
            "linked_at_turn >= 0",
            name="evidence_need_fulfillment_linked_at_turn_nonnegative",
        ),
        Index("ix_evidence_need_fulfillment_evidence", "evidence_id"),
    )


class CaseEntityModel(Base):
    """Cross-evidence entity index. One row per
    (case, entity_type, entity_value, evidence) tuple. Enables single-indexed
    lookups like 'which evidence mentions IP 10.0.0.5 in this case?'."""

    __tablename__ = "case_entities"

    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type = Column(String(20), primary_key=True)
    entity_value = Column(String(255), primary_key=True)
    evidence_id = Column(
        String(36),
        ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
        primary_key=True,
    )
    mention_count = Column(Integer, nullable=False, server_default="1")
    in_error_context = Column(Boolean, nullable=False, server_default="0")
    first_seen_ts = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_case_entities_lookup", "case_id", "entity_type", "entity_value"),
        Index("idx_case_entities_by_evidence", "evidence_id"),
    )


class HypothesisModel(Base):
    """Hypothesis for root cause analysis. Evidence linkage lives in the
    `hypothesis_evidence` junction (not a JSON blob)."""

    __tablename__ = "hypotheses"

    hypothesis_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Causal-chain header (Two-Dimensional Hypothesis Methodology §9.1): a
    # hypothesis is a root->D path over the case's causal graph. NULL root while
    # the chain is still expanding (lazy). `path` is the ordered node_id list.
    root_node_id = Column(
        String(36),
        ForeignKey("causal_nodes.node_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    path = Column(JsonBlob, nullable=False, server_default="[]")

    statement = Column(Text, nullable=False)
    state = Column(String(20), nullable=False, server_default="captured", index=True)
    likelihood = Column(Numeric(3, 2), nullable=True, server_default="0.5")
    initial_likelihood = Column(Numeric(3, 2), nullable=True, server_default="0.5")
    category = Column(String(50), nullable=False, index=True)
    generation_mode = Column(String(20), nullable=False, server_default="systematic")
    rationale = Column(Text, nullable=True)
    retirement_reason = Column(Text, nullable=True)
    refutation_reason = Column(String(200), nullable=True)

    # Turn tracking
    generated_at_turn = Column(Integer, nullable=False, server_default="0")
    last_updated_turn = Column(Integer, nullable=False, server_default="0")
    last_progress_at_turn = Column(Integer, nullable=False, server_default="0")
    iterations_without_progress = Column(Integer, nullable=False, server_default="0")

    tested_at = Column(DateTime(timezone=True), nullable=True)
    concluded_at = Column(DateTime(timezone=True), nullable=True)

    created_by = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    updated_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    hypothesis_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    proposed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case = relationship("CaseModel", back_populates="hypotheses")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="hypotheses_statement_not_empty"
        ),
        CheckConstraint(
            "state IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired')",
            name="hypotheses_state_check",
        ),
        CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="hypotheses_likelihood_range",
        ),
    )


class HypothesisEvidenceModel(Base):
    """Junction: hypothesis ↔ evidence with relationship qualifier.
    Replaces the old `hypotheses.evidence_links` JSON blob."""

    __tablename__ = "hypothesis_evidence"

    hypothesis_id = Column(
        String(36),
        ForeignKey("hypotheses.hypothesis_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id = Column(
        String(36),
        ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relationship_type = Column(
        String(30), nullable=False
    )  # supports | refutes | related
    confidence = Column(Numeric(3, 2), nullable=True)
    linked_at_turn = Column(Integer, nullable=True)
    linked_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('supports', 'refutes', 'related')",
            name="hypothesis_evidence_relationship_check",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="hypothesis_evidence_confidence_range",
        ),
        Index("ix_hypothesis_evidence_evidence", "evidence_id"),
    )


class SolutionModel(Base):
    """Proposed and applied solution. May or may not link to a hypothesis;
    KB-resolved cases populate Solution from the matched runbook Cause's
    Mitigation/Resolution blocks without going through hypothesis
    formulation (see indicator-resolution.md + KB-Resolution Path)."""

    __tablename__ = "solutions"

    solution_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hypothesis_id = Column(
        String(36),
        ForeignKey("hypotheses.hypothesis_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Causal-graph linkage (methodology §7.4, §9.1): which node this solution
    # acts on, and which intervention quadrant it occupies.
    node_id = Column(
        String(36),
        ForeignKey("causal_nodes.node_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quadrant = Column(String(20), nullable=True)

    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=False)
    solution_type = Column(String(30), nullable=False, server_default="other")
    state = Column(String(20), nullable=False, server_default="proposed", index=True)
    risk_level = Column(String(20), nullable=True)
    estimated_effort = Column(String(50), nullable=True)
    immediate_action = Column(Text, nullable=True)
    longterm_fix = Column(Text, nullable=True)
    implementation_steps = Column(JsonBlob, nullable=True)
    commands = Column(JsonBlob, nullable=True)
    risks = Column(JsonBlob, nullable=True)

    # Sentinel-friendly actor columns. Free-form VARCHAR per the
    # case_actions.triggered_by precedent — Pydantic Solution uses
    # 'agent' as a valid value, which a FK to users could not store.
    proposed_by = Column(String(50), nullable=False, server_default="agent")
    applied_by = Column(String(50), nullable=True)

    # Verification metadata. ``verification_method`` and
    # ``effectiveness`` complete the audit trail the Pydantic model
    # has been carrying without storage.
    verification_method = Column(String(500), nullable=True)
    verification_evidence_id = Column(
        String(36),
        ForeignKey("evidence.evidence_id", ondelete="SET NULL"),
        nullable=True,
    )
    effectiveness = Column(Float, nullable=True)
    verification_result = Column(Text, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    solution_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    proposed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case = relationship("CaseModel", back_populates="solutions")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(description)) > 0", name="solutions_description_not_empty"
        ),
        CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected', 'implemented', 'verified')",
            name="solutions_state_check",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')",
            name="solutions_risk_level_check",
        ),
        CheckConstraint(
            "effectiveness IS NULL OR (effectiveness >= 0 AND effectiveness <= 1)",
            name="solutions_effectiveness_range",
        ),
        CheckConstraint(
            "quadrant IS NULL OR quadrant IN "
            "('remediation', 'defensive_fix', 'mitigation', 'loop_break')",
            name="solutions_quadrant_check",
        ),
    )


class CausalNodeModel(Base):
    """A node in the case's causal graph (Two-Dimensional Hypothesis
    Methodology §3/§9.1): the active problem (PROBLEM), an intermediate state,
    or a candidate root cause (ROOT). Node-scoped evidence lives in the
    `causal_node_evidence` junction (not a JSON blob)."""

    __tablename__ = "causal_nodes"

    node_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    statement = Column(Text, nullable=False)
    node_type = Column(String(20), nullable=False, index=True)
    node_state = Column(
        String(20), nullable=False, server_default="candidate", index=True
    )
    validation_method = Column(String(20), nullable=False, server_default="none")
    belief = Column(Numeric(3, 2), nullable=True, server_default="0.5")
    signature_consistent = Column(Boolean, nullable=False, server_default="1")
    actionable = Column(Boolean, nullable=False, server_default="0")
    category = Column(String(50), nullable=True, index=True)
    state_epoch = Column(Integer, nullable=False, server_default="0")

    # Turn tracking (stagnation / decay; advances on investigation turns only)
    generated_at_turn = Column(Integer, nullable=False, server_default="0")
    last_updated_turn = Column(Integer, nullable=False, server_default="0")
    last_progress_at_turn = Column(Integer, nullable=False, server_default="0")
    iterations_without_progress = Column(Integer, nullable=False, server_default="0")

    refutation_reason = Column(String(200), nullable=True)
    rationale = Column(Text, nullable=True)

    node_metadata = Column("metadata", JsonBlob, nullable=False, server_default="{}")

    proposed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    case = relationship("CaseModel", back_populates="causal_nodes")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="causal_nodes_statement_not_empty"
        ),
        CheckConstraint(
            "node_type IN ('problem', 'intermediate', 'root')",
            name="causal_nodes_node_type_check",
        ),
        CheckConstraint(
            "node_state IN ('candidate', 'validated', 'refuted', 'inconclusive')",
            name="causal_nodes_node_state_check",
        ),
        CheckConstraint(
            "validation_method IN ('none', 'empirical', 'deductive')",
            name="causal_nodes_validation_method_check",
        ),
        CheckConstraint(
            "belief IS NULL OR (belief >= 0 AND belief <= 1)",
            name="causal_nodes_belief_range",
        ),
        # Cross-column invariants mirroring the CausalNode Pydantic validators
        # (M4 / M1 / refutation pairing) so non-model write paths can't persist
        # a row the loader would reject. NOT actionable is dialect-safe.
        CheckConstraint(
            "node_state <> 'validated' OR validation_method <> 'none'",
            name="causal_nodes_validated_requires_method",
        ),
        CheckConstraint(
            "NOT (node_type = 'root' AND node_state = 'validated' AND NOT actionable)",
            name="causal_nodes_validated_root_actionable",
        ),
        CheckConstraint(
            "(node_state = 'refuted' AND refutation_reason IS NOT NULL) "
            "OR (node_state <> 'refuted' AND refutation_reason IS NULL)",
            name="causal_nodes_refutation_pairing",
        ),
        # At most one PROBLEM node (the active problem D) per case.
        Index(
            "uq_causal_nodes_one_problem_per_case",
            "case_id",
            unique=True,
            sqlite_where=text("node_type = 'problem'"),
            postgresql_where=text("node_type = 'problem'"),
        ),
    )


class CausalEdgeModel(Base):
    """A directed cause -> effect edge in the causal graph (methodology S1).
    Edges sharing (effect_node_id, and_group) are co-necessary (an AND-set, M7);
    a null/distinct and_group denotes an independent (OR) alternative cause."""

    __tablename__ = "causal_edges"

    edge_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cause_node_id = Column(
        String(36),
        ForeignKey("causal_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    effect_node_id = Column(
        String(36),
        ForeignKey("causal_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    and_group = Column(String(64), nullable=True)
    reasoning = Column(Text, nullable=True)
    created_at_turn = Column(Integer, nullable=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "cause_node_id <> effect_node_id", name="causal_edges_no_self_loop"
        ),
    )


class CausalNodeEvidenceModel(Base):
    """Junction: causal node ↔ evidence with a stance (methodology §9.1).
    The node-scoped counterpart of `hypothesis_evidence`."""

    __tablename__ = "causal_node_evidence"

    node_id = Column(
        String(36),
        ForeignKey("causal_nodes.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id = Column(
        String(36),
        ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stance = Column(String(20), nullable=False)  # supports | refutes | neutral
    stance_confidence = Column(Numeric(3, 2), nullable=True)
    reasoning = Column(Text, nullable=True)
    linked_at_turn = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "stance IN ('supports', 'refutes', 'neutral')",
            name="causal_node_evidence_stance_check",
        ),
        CheckConstraint(
            "stance_confidence IS NULL OR "
            "(stance_confidence >= 0 AND stance_confidence <= 1)",
            name="causal_node_evidence_confidence_range",
        ),
        Index("ix_causal_node_evidence_evidence", "evidence_id"),
    )


class CaseMessageModel(Base):
    """Conversation message within a case."""

    __tablename__ = "case_messages"

    message_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_number = Column(Integer, nullable=False, server_default="0")
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, nullable=True)
    message_metadata = Column("metadata", JsonBlob, nullable=False, server_default="{}")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    case = relationship("CaseModel", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(TRIM(content)) > 0", name="case_messages_content_not_empty"
        ),
        CheckConstraint("turn_number >= 0", name="case_messages_turn_nonnegative"),
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="case_messages_role_check"
        ),
        Index("ix_case_messages_case_turn", "case_id", "turn_number"),
        Index("ix_case_messages_case_created", "case_id", "created_at"),
    )


class CaseActionModel(Base):
    """Audit trail of phase transitions and dispositions on a case."""

    __tablename__ = "case_actions"

    transition_id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_state = Column(String(50), nullable=True)
    to_state = Column(String(50), nullable=False)
    reason = Column(Text, nullable=True)
    # Free-form actor identifier: user UUID or sentinel like "system",
    # "agent", "scheduler". Not a FK because the value space is heterogeneous;
    # a FK would force inventing a sentinel users row. CHECK can be added
    # later once the sentinel set stabilizes.
    triggered_by = Column(String(50), nullable=False)
    transition_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )
    transitioned_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    case = relationship("CaseModel", back_populates="actions")


class CaseTagModel(Base):
    """Free-form tag on a case."""

    __tablename__ = "case_tags"

    tag_id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tag = Column(String(50), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    case = relationship("CaseModel", back_populates="tags")

    __table_args__ = (
        UniqueConstraint("case_id", "tag", name="case_tags_unique"),
        CheckConstraint("LENGTH(TRIM(tag)) > 0", name="case_tags_tag_not_empty"),
        # Tag value cannot contain comma so the SQLite comma-separated tag
        # serialization round-trips losslessly.
        CheckConstraint("tag NOT LIKE '%,%'", name="case_tags_no_commas"),
    )


class CaseCheckpointModel(Base):
    """Immutable snapshot of a case at a specific turn (time-travel debugging)."""

    __tablename__ = "case_checkpoints"

    checkpoint_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_number = Column(Integer, nullable=False)
    case_snapshot = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    trigger = Column(String(50), nullable=False)
    checkpoint_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_case_checkpoints_case_turn", "case_id", "turn_number"),
        CheckConstraint(
            "LENGTH(TRIM(snapshot_hash)) > 0", name="case_checkpoints_hash_not_empty"
        ),
    )


class InvestigationSessionModel(Base):
    """Investigation session: a temporal grouping of agent executions within a
    case. The four-level cascade chain is
    Case → InvestigationSession → AgentExecution → AgentToolCall."""

    __tablename__ = "investigation_sessions"

    session_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    state = Column(String(32), nullable=False, server_default="active", index=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=False)
    total_duration_ms = Column(Integer, nullable=True)
    session_goal = Column(Text, nullable=True)
    findings_summary = Column(Text, nullable=True)
    total_token_usage = Column(Integer, nullable=False, server_default="0")
    total_agent_executions = Column(Integer, nullable=False, server_default="0")
    token_budget_limit = Column(Integer, nullable=True)
    session_metadata = Column("metadata", JsonBlob, nullable=False, server_default="{}")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    agent_executions = relationship(
        "AgentExecutionModel",
        back_populates="session",
        foreign_keys="AgentExecutionModel.session_id",
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'abandoned')",
            name="investigation_sessions_state_check",
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


class AgentExecutionModel(Base):
    """Agent execution: one LLM-driven action within an investigation session."""

    __tablename__ = "agent_executions"

    execution_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        String(36),
        ForeignKey("investigation_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    agent_type = Column(String(64), nullable=False, index=True)
    agent_model = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False, server_default="queued", index=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    execution_duration_ms = Column(Integer, nullable=True)

    prompt = Column(Text, nullable=True)
    response = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    token_usage = Column(JsonBlob, nullable=True)

    execution_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

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


class AgentToolCallModel(Base):
    """Tool invocation made during an agent execution."""

    __tablename__ = "agent_tool_calls"

    tool_call_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id = Column(
        String(36),
        ForeignKey("agent_executions.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name = Column(String(128), nullable=False, index=True)
    tool_input = Column(JsonBlob, nullable=True)
    tool_output = Column(JsonBlob, nullable=True)
    status = Column(String(32), nullable=False, server_default="pending", index=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

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


class ReportModel(Base):
    """Case-summary document (resolution_summary or closure_summary). Cascades
    with the case — these are case-history artifacts, not reusable knowledge.
    Reusable knowledge derived from cases lives in `knowledge_items`."""

    __tablename__ = "reports"

    report_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generated_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    report_type = Column(String(30), nullable=False)
    version = Column(Integer, nullable=False, server_default="1")
    is_current = Column(Boolean, nullable=False, server_default="1")
    linked_to_closure = Column(Boolean, nullable=False, server_default="0")
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    format = Column(String(20), nullable=False, server_default="markdown")
    generation_status = Column(String(20), nullable=False)
    generation_time_ms = Column(Integer, nullable=False)
    report_metadata = Column("metadata", JSON, nullable=True)

    generated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_reports_type_version", "case_id", "report_type"),
        CheckConstraint(
            "report_type IN ('resolution_summary', 'closure_summary')",
            name="reports_type_check",
        ),
        CheckConstraint("format IN ('markdown', 'html')", name="reports_format_check"),
        CheckConstraint(
            "generation_status IN ('generating', 'completed', 'failed')",
            name="reports_status_check",
        ),
        CheckConstraint("version >= 1 AND version <= 5", name="reports_version_check"),
        CheckConstraint(
            "generation_time_ms >= 0 AND generation_time_ms <= 120000",
            name="reports_gen_time_check",
        ),
    )


# ============================================================
# Knowledge Domain
# ============================================================


class KnowledgeItemModel(Base):
    """Knowledge base item (RAG corpus). Scope determines visibility:
    `personal` (one user), `team` (one team), `organization` (one workspace),
    `global` (platform-wide built-in runbooks)."""

    __tablename__ = "knowledge_items"

    item_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope = Column(
        String(20), nullable=False, server_default="organization", index=True
    )
    owner_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_suggestion_id = Column(String(36), nullable=True, index=True)

    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    item_type = Column(String(64), nullable=False, index=True)
    category = Column(String(128), nullable=True, index=True)
    tags = Column(TagsArray, nullable=True)

    # Vector storage. embedding_vector holds the BGE-M3 1024-dim vector
    # serialized as TEXT for cross-dialect compatibility; on PG with pgvector
    # this column can be promoted to VECTOR(1024) via a follow-up migration.
    embedding_model = Column(String(128), nullable=False, server_default="bge-m3")
    embedding_vector = Column(Text, nullable=True)
    embedding_version = Column(Integer, nullable=False, server_default="1")

    source_url = Column(String(2048), nullable=True)
    author = Column(String(255), nullable=True)
    language = Column(String(8), nullable=False, server_default="en")

    # Verification: 0=experimental, 1=community, 2=admin_verified
    verification_level = Column(Integer, nullable=False, server_default="0", index=True)
    verification_reason = Column(String(512), nullable=True)
    # Contract: a REAL users.user_id or NULL — never a sentinel string
    # (e.g. "system"). It is an enforced FK (foreign_keys=ON since #378),
    # so a non-user value fails the insert. Platform/system-verified trust
    # belongs in verification_level (e.g. COMMUNITY), not here.
    verified_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)

    view_count = Column(Integer, nullable=False, server_default="0")
    helpful_count = Column(Integer, nullable=False, server_default="0")
    not_helpful_count = Column(Integer, nullable=False, server_default="0")
    last_retrieved_at = Column(DateTime(timezone=True), nullable=True, index=True)
    is_published = Column(Boolean, nullable=False, server_default="1", index=True)

    knowledge_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('personal', 'team', 'organization', 'global')",
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
        Index("ix_knowledge_items_tags", "tags", postgresql_using="gin"),
    )


class KnowledgeSuggestionModel(Base):
    """Pending KB suggestion extracted from a case. Survives case deletion via
    SET NULL on case_id (the suggestion's content stays useful even after the
    source case is gone)."""

    __tablename__ = "knowledge_suggestions"

    suggestion_id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    knowledge_item_id = Column(
        String(36),
        ForeignKey("knowledge_items.item_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status = Column(
        String(32), nullable=False, server_default="pending_review", index=True
    )

    suggested_title = Column(String(512), nullable=False)
    suggested_content = Column(Text, nullable=False)
    suggested_type = Column(
        String(64), nullable=False, server_default="troubleshooting_guide"
    )

    extracted_by = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extracted_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    include_messages = Column(Boolean, nullable=False, server_default="1")
    include_evidence = Column(Boolean, nullable=False, server_default="1")

    pii_scan_status = Column(
        String(32), nullable=False, server_default="not_scanned", index=True
    )
    pii_scan_result = Column(JsonBlob, nullable=True)
    pii_remediated_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    pii_remediated_at = Column(DateTime(timezone=True), nullable=True)

    source_case_title = Column(String(512), nullable=True)
    message_count = Column(Integer, nullable=False, server_default="0")
    evidence_count = Column(Integer, nullable=False, server_default="0")

    reviewed_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    suggestion_metadata = Column(
        "metadata", JsonBlob, nullable=False, server_default="{}"
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

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


class ConversionJobModel(Base):
    """Document-to-runbook conversion job. `case_id` is nullable: most
    conversions are case-less doc uploads; `convert-from-case` flow sets case_id."""

    __tablename__ = "conversion_jobs"

    id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    team_id = Column(
        String(36),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
    )
    case_id = Column(
        String(36),
        ForeignKey("cases.case_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ON DELETE RESTRICT: the source file IS this job's provenance. You cannot
    # delete the upload while a conversion job (or any of its drafts) still
    # references it; the job records "this exact file processed at this exact
    # time" for audit. Delete the conversion job first, then the upload.
    source_file_id = Column(
        String(36),
        ForeignKey("uploaded_files.file_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    scope = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, server_default="processing")
    source_type = Column(String(20), nullable=False, server_default="document")
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

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed', 'cancelled')",
            name="conversion_jobs_status_check",
        ),
        CheckConstraint(
            "scope IN ('personal', 'team', 'organization', 'global')",
            name="conversion_jobs_scope_check",
        ),
        CheckConstraint(
            "source_type IN ('document', 'case')",
            name="conversion_jobs_source_type_check",
        ),
    )


class ConversionDraftModel(Base):
    """Individual runbook draft generated from a conversion job."""

    __tablename__ = "conversion_drafts"

    id = Column(String(36), primary_key=True)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversion_id = Column(
        String(36),
        ForeignKey("conversion_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_item_id = Column(
        String(36),
        ForeignKey("knowledge_items.item_id", ondelete="SET NULL"),
        nullable=True,
    )
    verified_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    runbook_id = Column(String(100), nullable=False)
    title = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, server_default="draft")
    source_type = Column(String(20), nullable=False, server_default="document")
    document_type = Column(String(50), nullable=True, server_default="runbook")
    domain = Column(String(50), nullable=True)
    service = Column(String(100), nullable=True)
    severity = Column(String(20), nullable=True)
    tags = Column(TagsArray, nullable=True)

    validation_passed = Column(Boolean, nullable=False, server_default="1")
    validation_errors = Column(JSON, nullable=True)
    validation_warnings = Column(JSON, nullable=True)
    quality_score = Column(Numeric(5, 1), nullable=True)
    quality_details = Column(JSON, nullable=True)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)

    conversion_job = relationship("ConversionJobModel", back_populates="drafts")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'verified', 'discarded')",
            name="conversion_drafts_status_check",
        ),
        CheckConstraint(
            "severity IS NULL OR severity IN ('low', 'medium', 'high', 'critical')",
            name="conversion_drafts_severity_check",
        ),
        Index("ix_conversion_drafts_tags", "tags", postgresql_using="gin"),
    )


# ============================================================
# Config Domain
# ============================================================


class ConfigOverrideModel(Base):
    """Dashboard-applied runtime configuration overrides. Hot-reloaded into
    runtime settings; takes precedence over .env values. Cloud mode only.

    Scope is broader than LLM (llm-configuration-design.md Phase 2): ``category``
    groups settings ('llm', 'feature', 'operational', 'observability', ...) and
    ``source`` records provenance ('env-seed' seeded from .env vs 'admin' set
    explicitly via the dashboard) so the admin UI can show which is active."""

    __tablename__ = "config_overrides"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    category = Column(String(50), nullable=False, server_default="llm")
    source = Column(String(20), nullable=False, server_default="admin")
    updated_by = Column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
