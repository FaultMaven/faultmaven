"""API Request/Response Models (TASK-014)

Purpose: Pydantic models for FastAPI request validation and response serialization.

This module provides:
- Request and response models for investigation sessions
- Admin user, LLM configuration and config-status models

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from faultmaven.models.investigation_session import SessionState

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
    enterprise_id: str
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
            enterprise_id=session.enterprise_id,
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
# Admin User Management Models (TASK-019)
# ============================================================


class AdminUserListItem(BaseModel):
    """One account in the operator's account list.

    Under ``TENANT_PROVIDER=multi`` the list spans every enterprise; the
    operator administers only the accounts of their own enterprise, which
    ``manageable`` marks.
    """

    user_id: str
    enterprise_id: str = Field(
        ..., description="The enterprise the account is anchored to."
    )
    email: str
    full_name: str
    roles: List[str] = Field(
        ...,
        description=(
            "The account's organization-scoped roles. Reported only for an "
            "account the operator can manage (`manageable`); on a row outside "
            "the operator's enterprise the list is empty, which means 'not "
            "reported', not 'holds no role'."
        ),
    )
    account_kind: Literal["individual", "service"] = Field(
        ...,
        description=(
            "'individual' for a person, 'service' for an integration's service "
            "account."
        ),
    )
    service_channel: Optional[str] = Field(
        None,
        description=(
            "Which integration a service account serves (for example "
            "'slack'); null for a person."
        ),
    )
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    manageable: bool = Field(
        ...,
        description=(
            "Whether the operator can administer this account (deactivate, "
            "activate, change its roles). True for every account in the "
            "operator's own enterprise — every account under single-tenancy. "
            "False for an account in another enterprise, which the "
            "administration routes answer with 404. Per-target refusals still "
            "apply on a manageable row: an operator cannot deactivate or "
            "re-role their own account."
        ),
    )

    model_config = ConfigDict(from_attributes=True)


class AdminUserListResponse(BaseModel):
    """Admin user list response with pagination.

    ``total`` counts every account matching the filters that the list ranges
    over — every enterprise under ``TENANT_PROVIDER=multi``.
    """

    users: List[AdminUserListItem]
    total: int
    limit: int
    offset: int


class UserDetailResponse(BaseModel):
    """Detailed user information (admin only)."""

    user_id: str
    enterprise_id: str
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
    selected_model_priced: Optional[bool] = Field(
        None,
        description=(
            "Whether selected_model has a rate in the cost table. False means "
            "this provider's calls report $0 spend. None when no model is "
            "resolved yet (provider not initialized)."
        ),
    )
    error_message: Optional[str] = None
    health: str = Field(
        default="unknown", description="HEALTHY, DEGRADED, UNHEALTHY, or UNKNOWN"
    )
    avg_latency_ms: float = 0.0


class LLMRoleRouting(BaseModel):
    """Resolved routing for one capability role.

    ``primary_provider`` alone does not describe what is running: three roles
    ship pinned to a provider of their own and stay put when the anchor moves,
    and the rest ship unset and follow it. A page showing only the anchor
    reports a configuration that omits load-bearing routing (#1206).
    """

    model_config = ConfigDict(protected_namespaces=())

    role: str = Field(
        description=(
            "Capability role: chat, multimodal, synthesis, classifier, code, "
            "da, knowledge, structured_output"
        )
    )
    provider: str = Field(description="Provider this role's calls are routed to")
    model: str = Field(
        description=(
            "Model this role runs on; empty string when that provider has no "
            "model configured"
        )
    )
    provider_source: str = Field(
        description=(
            "Where the provider came from: 'env-default' (this role's own key "
            "is set), 'admin-override' (dashboard-written), or 'inherited' "
            "(no role key — follows CHAT_PROVIDER and moves with it)"
        )
    )
    model_source: str = Field(
        description=(
            "Where the model came from: 'env-default', 'admin-override', or "
            "'unset' (no model configured for this provider)"
        )
    )
    provider_key: str = Field(
        description="Environment key carrying the provider decision, e.g. CLASSIFIER_PROVIDER"
    )
    model_key: str = Field(
        description=(
            "Environment key carrying the model decision, e.g. "
            "GEMINI_CLASSIFIER_MODEL or GEMINI_MODEL; empty when unset. A "
            "per-task key does not move when the provider's model is changed"
        )
    )
    provider_initialized: bool = Field(
        description=(
            "The named provider was built by the registry. False means its "
            "credential is missing, the routing is inert, and this role's "
            "calls fall back to fallback_chain"
        )
    )


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
    role_routing: List[LLMRoleRouting] = Field(
        default_factory=list,
        description=(
            "Resolved (provider, model) per capability role, with provenance. "
            "Read-only: role routing is set in the environment and is not in "
            "the dashboard's override allowlist."
        ),
    )
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
            "enterprises provisioned per rolling hour, deployment-wide. It "
            "bounds provisioning only; tenants that already exist sign in "
            "regardless."
        )
    )
    tenant_daily_turn_cap: int = Field(
        description=(
            "TENANT_DAILY_TURN_CAP — investigation turns an account in NO "
            "organization may take per UTC day before further turns are "
            "refused with 429. The deployment DEFAULT only: an organization is "
            "uncapped, a single-tenant deployment is never capped, and a "
            "per-organization override set with fm-set-turn-cap beats this "
            "value."
        )
    )


class EnvConfigStatusResponse(BaseModel):
    """Read-only environment configuration status for dashboard display."""

    auth_mode: str = Field(description="'local' or 'oauth'")
    deployment: str = Field(
        description="'standalone' or 'cloud' — from DEPLOYMENT_MODE (ADR-004)"
    )
    db_backend: str = Field(
        description=(
            "'sqlite' or 'postgresql' — the dialect of the database engine the "
            "running process built; 'not initialized' before it has built one"
        )
    )
    session_storage: str = Field(
        description=(
            "'redis' or 'fakeredis (inmemory)' — the Redis client the session "
            "store actually uses, not the configured one; 'not initialized' "
            "before the composition root has set it"
        )
    )
    vector_storage: str = Field(
        description=(
            "What the running process's KB and evidence ChromaDB clients talk to: "
            "'chromadb (server)', 'chromadb (persistent, split: kb + evidence)', "
            "'disabled' when neither was built, or a per-client breakdown when "
            "they differ"
        )
    )
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
