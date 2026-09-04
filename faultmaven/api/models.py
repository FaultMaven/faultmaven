"""API Request/Response Models (TASK-014)

Purpose: Pydantic models for FastAPI request validation and response serialization.

This module provides:
- Request models for case, session, and evidence operations
- Response models for API responses
- Error response models for consistent error handling

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from faultmaven.models.investigation_session import SessionState

# Import from contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (
    CaseSeverity,
    CaseState,
    EvidenceArtifactType,
    InvestigationProgress,
)

# ============================================================
# Case Models
# ============================================================


class CaseCreateRequest(BaseModel):
    """Request model for creating a case."""

    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field(..., min_length=1)
    severity: CaseSeverity
    metadata: Optional[Dict[str, Any]] = None


class CaseUpdateRequest(BaseModel):
    """Request model for updating a case."""

    title: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = Field(None, min_length=1)
    severity: Optional[CaseSeverity] = None
    state: Optional[CaseState] = None
    assigned_to: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class CaseResponse(BaseModel):
    """Response model for a case."""

    case_id: str
    organization_id: str
    reporter_user_id: str
    title: str
    description: str
    severity: CaseSeverity
    state: CaseState
    progress: Optional[InvestigationProgress] = None
    assigned_to: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    resolution: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(
        cls, case: Any, severity: Optional[CaseSeverity] = None
    ) -> "CaseResponse":
        """Create CaseResponse from domain Case model.

        Args:
            case: Domain Case object
            severity: Optional severity override (extracted from metadata)

        Returns:
            CaseResponse instance
        """
        # Extract severity from problem_verification or metadata
        case_severity = severity
        if case_severity is None:
            if hasattr(case, "problem_verification") and case.problem_verification:
                try:
                    case_severity = CaseSeverity.from_string(
                        case.problem_verification.severity
                    )
                except (ValueError, AttributeError):
                    case_severity = CaseSeverity.MEDIUM
            else:
                case_severity = CaseSeverity.MEDIUM

        # Get resolution from closure_reason if available
        resolution = None
        if hasattr(case, "closure_reason") and case.closure_reason:
            resolution = case.closure_reason

        return cls(
            case_id=case.case_id,
            organization_id=case.organization_id,
            reporter_user_id=case.user_id,
            title=case.title,
            description=case.description,
            severity=case_severity,
            state=case.state,
            progress=getattr(case, "progress", None),
            assigned_to=getattr(case, "assigned_to", None),
            created_at=case.created_at,
            updated_at=case.updated_at,
            closed_at=getattr(case, "closed_at", None),
            resolution=resolution,
            metadata=getattr(case, "metadata", None),
        )


class CaseListResponse(BaseModel):
    """Response model for case list."""

    items: List[CaseResponse]
    total: int
    limit: int
    offset: int


# ============================================================
# Session Models
# ============================================================


class SessionCreateRequest(BaseModel):
    """Request model for creating investigation session."""

    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None


class SessionUpdateRequest(BaseModel):
    """Request model for updating session."""

    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None


class InvestigationSessionResponse(BaseModel):
    """Response model for investigation session."""

    session_id: str
    case_id: str
    user_id: str
    organization_id: str
    state: SessionState
    started_at: datetime
    ended_at: Optional[datetime] = None
    last_activity_at: datetime
    total_duration_ms: Optional[int] = None
    session_goal: Optional[str] = None
    findings_summary: Optional[str] = None
    total_token_usage: int
    total_agent_executions: int
    token_budget_limit: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, session: Any) -> "InvestigationSessionResponse":
        """Create InvestigationSessionResponse from domain InvestigationSession model.

        Args:
            session: Domain InvestigationSession object

        Returns:
            InvestigationSessionResponse instance
        """
        return cls(
            session_id=session.session_id,
            case_id=session.case_id,
            user_id=session.user_id,
            organization_id=session.organization_id,
            state=session.state,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last_activity_at=session.last_activity_at,
            total_duration_ms=session.total_duration_ms,
            session_goal=session.session_goal,
            findings_summary=session.findings_summary,
            total_token_usage=session.total_token_usage,
            total_agent_executions=session.total_agent_executions,
            token_budget_limit=session.token_budget_limit,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class SessionListResponse(BaseModel):
    """Response model for session list."""

    items: List[InvestigationSessionResponse]
    total: int
    limit: int
    offset: int


# ============================================================
# Evidence Models
# ============================================================


class EvidenceUploadRequest(BaseModel):
    """Request model for evidence upload (multipart form).

    Note: This model is used for documentation purposes.
    The actual upload uses FastAPI Form parameters.
    """

    evidence_type: EvidenceArtifactType
    description: Optional[str] = None
    is_primary: bool = False
    metadata: Optional[Dict[str, Any]] = None


class EvidenceUpdateRequest(BaseModel):
    """Request model for updating evidence."""

    description: Optional[str] = None
    is_primary: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


# EvidenceResponse / EvidenceListResponse removed (2026-05): both classes
# were dead code referencing dropped Evidence attributes (original_filename,
# evidence_type, mime_type, file_size, user_id) — none of which survive on
# the post-redesign Evidence model. Verified by grep: no consumer imported
# either class. Evidence is exposed to the API via the case-detail aggregate
# (case_ui_adapter), not via a standalone evidence-list endpoint.


# ============================================================
# Error Models
# ============================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: Optional[str] = None
    status_code: int


class ValidationErrorResponse(BaseModel):
    """Validation error response with field-level details."""

    error: str = "Validation Error"
    detail: Optional[str] = None
    status_code: int = 400
    errors: Optional[List[Dict[str, Any]]] = None


# ============================================================
# Admin User Management Models (TASK-019)
# ============================================================


class AdminUserListItem(BaseModel):
    """User list item for admin endpoints (with full info)."""

    user_id: str
    organization_id: str
    email: str
    full_name: str
    roles: List[str]
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminUserListResponse(BaseModel):
    """Admin user list response with pagination."""

    users: List[AdminUserListItem]
    total: int
    limit: int
    offset: int


class UserDetailResponse(BaseModel):
    """Detailed user information (admin only)."""

    user_id: str
    organization_id: str
    email: str
    full_name: str
    roles: List[str]
    permissions: List[str]
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class UserStatusResponse(BaseModel):
    """User activation/deactivation response."""

    user_id: str
    is_active: bool
    updated_at: datetime
    message: str


class RoleAssignmentRequest(BaseModel):
    """Role assignment request."""

    role: str = Field(
        ...,
        pattern="^(admin|member|viewer)$",
        description="Role to assign (admin, member, or viewer)",
    )


class RoleAssignmentResponse(BaseModel):
    """Role assignment response."""

    user_id: str
    roles: List[str]
    updated_at: datetime
    message: str


class OrganizationUserListItem(BaseModel):
    """User list item for organization user list (limited info)."""

    user_id: str
    email: str
    full_name: str
    roles: List[str]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class OrganizationUserListResponse(BaseModel):
    """Organization user list response with pagination."""

    users: List[OrganizationUserListItem]
    total: int
    limit: int
    offset: int


# ============================================================
# LLM Configuration Models (Dashboard Phase 1a)
# ============================================================


class LLMProviderDetail(BaseModel):
    """Individual LLM provider status for dashboard display."""

    name: str
    display_name: str
    enabled: bool = Field(
        description="Provider is initialized and in the fallback chain"
    )
    connected: bool = Field(description="Provider responded to last health check")
    has_api_key: bool = Field(description="API key is configured (value never exposed)")
    state: str = Field(
        default="not_configured",
        description="Provider lifecycle state: not_configured, configured, or active",
    )
    models: List[str] = Field(default_factory=list)
    selected_model: Optional[str] = Field(
        None, description="Currently active model for this provider"
    )
    available_models: List[str] = Field(
        default_factory=list,
        description="Models the user can choose from for this provider",
    )
    error_message: Optional[str] = None
    health: str = Field(
        default="unknown", description="HEALTHY, DEGRADED, UNHEALTHY, or UNKNOWN"
    )
    avg_latency_ms: float = 0.0


class LLMConfigResponse(BaseModel):
    """LLM configuration and provider status response."""

    deployment: str = Field(description="Deployment mode: 'standalone' or 'cloud'")
    config_readonly: bool = Field(
        description="True in standalone mode (config managed via .env file)"
    )
    primary_provider: str
    strict_mode: bool
    fallback_chain: List[str]
    providers: Dict[str, LLMProviderDetail]
    config_sources: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Provenance per overridable setting key: 'admin-override' (set via "
            "the dashboard, stored in the DB) or 'env-default' (.env / seed). "
            "Always 'env-default' in standalone (no DB overrides)."
        ),
    )
    timestamp: datetime


class LLMConnectionTestRequest(BaseModel):
    """Request to test an LLM provider connection."""

    provider: str = Field(
        ..., description="Provider name to test (e.g. 'anthropic', 'openai')"
    )


class LLMConnectionTestResponse(BaseModel):
    """Result of an LLM provider connection test."""

    model_config = ConfigDict(protected_namespaces=())

    provider: str
    connected: bool
    response_time_ms: int = 0
    error_message: Optional[str] = None
    model_used: Optional[str] = None
    timestamp: datetime


class LLMConfigUpdateRequest(BaseModel):
    """Request to update LLM configuration."""

    primary_provider: Optional[str] = Field(
        None, description="New primary provider name"
    )
    fallback_chain: Optional[List[str]] = Field(
        None, description="New fallback chain order"
    )
    provider_name: Optional[str] = Field(
        None, description="Provider to update API key or model for"
    )
    api_key: Optional[str] = Field(
        None, description="New API key value for the specified provider"
    )
    model: Optional[str] = Field(
        None,
        description="Model to use for the specified provider (requires provider_name)",
    )


class LLMConfigUpdateResponse(BaseModel):
    """Response after updating LLM configuration."""

    updated_keys: List[str] = Field(description="Config keys that were updated")
    message: str
    timestamp: datetime


# ============================================================
# Environment Configuration Status Models (Dashboard Phase 1a)
# ============================================================


class FeatureStatus(BaseModel):
    """Status of an optional feature that depends on configuration."""

    enabled: bool = Field(description="Feature is active and usable")
    has_api_key: bool = Field(
        default=False, description="Required API key is configured"
    )
    description: str = Field(default="", description="Brief explanation of the feature")
    config_hint: str = Field(
        default="",
        description="What the user needs to set to enable this feature",
    )


class PersonalTenantLimitsStatus(BaseModel):
    """The three settings that bound self-service sign-up, at their effective
    values.

    Reported as VALUES rather than as ``features`` entries. Two of the three are
    numbers, which ``FeatureStatus`` has nowhere to put, and the ``features``
    contract is stricter than this: ``enabled`` there must report a runtime
    EFFECT (#1234), which "did you set this knob" is not. They sit here with
    ``auth_mode`` and ``pii_redaction_enabled``, whose claim is the same one —
    this is the configuration the process is running with.

    Reporting them at all is the ``first_party_consent_skip`` argument (#1234)
    applied to configuration: all three are silent by construction. A
    deployment with self-service sign-up off refuses org-less identities with
    the same message it would give a misconfigured IdP; a deployment at its
    hourly provisioning ceiling refuses the same way; and a personal tenant at
    its daily turn cap gets a usage-allowance message that names no setting.
    None of the three appears in ``/health``, and a startup log line has rolled
    out of ``kubectl logs`` long before anyone asks.
    """

    sso_jit_personal_tenant_enabled: bool = Field(
        description=(
            "SSO_JIT_PERSONAL_TENANT_ENABLED — whether an SSO identity with no "
            "IdP organization may provision a personal tenant on its first "
            "sign-in, i.e. whether self-service sign-up is open. Multi-tenant "
            "(Cloud) deployments only: a single-tenant deployment has one "
            "organization and never reaches the branch this gates."
        )
    )
    sso_jit_personal_tenant_max_per_hour: int = Field(
        description=(
            "SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR — the ceiling on NEW personal "
            "tenants provisioned per rolling hour, deployment-wide. It bounds "
            "provisioning only; tenants that already exist sign in regardless."
        )
    )
    tenant_daily_turn_cap: int = Field(
        description=(
            "TENANT_DAILY_TURN_CAP — investigation turns a PERSONAL tenant may "
            "take per UTC day before further turns are refused with 429. The "
            "deployment DEFAULT only: a company organization is uncapped, a "
            "single-tenant deployment is never capped, and a per-organization "
            "override set with fm-set-turn-cap beats this value."
        )
    )


class EnvConfigStatusResponse(BaseModel):
    """Read-only environment configuration status for dashboard display."""

    auth_mode: str = Field(description="'local' or 'oauth'")
    deployment: str = Field(
        description="'standalone' or 'cloud' — from DEPLOYMENT_MODE (ADR-004)"
    )
    db_backend: str = Field(description="'sqlite' or 'postgresql'")
    session_storage: str = Field(description="'inmemory' or 'redis'")
    vector_storage: str = Field(description="'inmemory' or 'chromadb'")
    llm_provider: str = Field(description="Primary LLM provider name")
    pii_redaction_enabled: bool
    rate_limit_enabled: bool = Field(
        description=(
            "Rate limiting middleware is installed on this deployment. Read "
            "from the running middleware stack rather than from configuration: "
            "no rate-limit setting exists, the protection presets decide by "
            "environment name, and no environment variable turns it off. A "
            "deployment reports false here only if protection setup raised and "
            "the development carve-out let it boot anyway."
        )
    )
    features: Dict[str, FeatureStatus] = Field(
        default_factory=dict,
        description="Optional features and their configuration status",
    )
    personal_tenant_limits: PersonalTenantLimitsStatus = Field(
        description=(
            "Effective values of the settings that bound self-service "
            "sign-up: whether an org-less SSO identity may provision a "
            "personal tenant, how many such tenants may be provisioned per "
            "hour deployment-wide, and how many investigation turns each one "
            "gets per UTC day."
        )
    )
    timestamp: datetime
