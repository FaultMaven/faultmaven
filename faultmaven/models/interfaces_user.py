"""User, organization, and team management interfaces.

This module defines the interface contracts for enterprise user management,
following FaultMaven's interface-based dependency injection pattern.

Implemented by:
- PostgreSQLEnterpriseRepository
- PostgreSQLOrganizationRepository
- PostgreSQLTeamRepository
- PostgreSQLUserRepository (enhanced)

Hierarchy (ADR-017 D1): **Enterprise ⊃ {accounts, organizations, teams}** — not a
chain. The enterprise ISOLATES (RLS keys on ``enterprise_id``; nothing crosses an
enterprise line). An organization BILLS: it is a cost centre inside an
enterprise, with no role in visibility. A team SHARES, by consent, inside one
enterprise, and may span organizations. Single-tenant deployments (standalone)
get one default enterprise and one default team, and **no organization row** —
nothing is billed there.

Cross-layer parity:
- ``Enterprise.name``, ``Organization.name``, ``Team.name`` mirror DB
  ``LENGTH(TRIM(...)) > 0`` CHECKs (enterprises_name_not_empty,
  organizations_name_not_empty, teams_name_not_empty).
- ``Enterprise.slug``, ``Organization.slug`` mirror DB
  ``LENGTH(slug) > 0`` (no TRIM, by design).
- ``Role.scope`` is typed as :class:`RoleScope` to mirror DB roles_scope_check.

This is the single canonical Pydantic family for the tenancy DB rows
(enterprises, organizations, teams, and their members). Repositories and
tenancy providers implement/consume these interfaces and models.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

# Aliased on import: ``Permission`` is already taken in this module by the
# ``permissions`` table row model below. The enum is the authority *vocabulary*;
# the row model is one seeded instance of it.
from faultmaven.models.rbac import Permission as PermissionEnum

# ============================================================================
# Enums
# ============================================================================


class OrgPlanTier(str, Enum):
    """Organization subscription plan levels.

    Plan tier now lives on the Enterprise tier (`enterprises.plan_tier`).
    This enum is retained for the Organization domain model's read-side
    fallback until callers migrate to read plan_tier off the parent
    enterprise.

    Note: the top tier is ``BUSINESS`` (not "enterprise") so a billing level
    never collides with the Enterprise tenant entity or ``RoleScope.ENTERPRISE``.
    """

    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"


class EnterprisePlanTier(str, Enum):
    """Enterprise subscription plan tiers (see enterprises.plan_tier).

    Top tier is ``BUSINESS`` (not "enterprise"): a billing level must not
    collide with the Enterprise tenant entity or ``RoleScope.ENTERPRISE``.
    """

    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"


class RoleScope(str, Enum):
    """RBAC role scope levels. Mirrors the DB roles_scope_check CHECK."""

    SYSTEM = "system"
    ENTERPRISE = "enterprise"
    ORGANIZATION = "organization"
    TEAM = "team"


class AuditEventType(str, Enum):
    """User audit event types."""

    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    PASSWORD_CHANGED = "password_changed"
    ACCOUNT_CREATED = "account_created"
    ROLE_ASSIGNED = "role_assigned"
    # Distinct from ROLE_ASSIGNED rather than one event with a direction flag in
    # `details`: `event_type` is the indexed, queried column, and "show me every
    # revocation" must not depend on parsing a JSON blob (fm#1050).
    ROLE_REMOVED = "role_removed"
    CASE_SHARED = "case_shared"
    # Ownership moving between principals, distinct from CASE_SHARED (which
    # widens visibility without changing the owner). Its own indexed value
    # for the same reason ROLE_REMOVED is not a flag on ROLE_ASSIGNED:
    # "which cases changed hands" must not require parsing `details`.
    CASE_REASSIGNED = "case_reassigned"
    KB_DOCUMENT_SHARED = "kb_document_shared"
    TEAM_CREATED = "team_created"


class AuditCategory(str, Enum):
    """Audit event categories."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    DATA_ACCESS = "data_access"
    ADMINISTRATION = "administration"
    SECURITY = "security"


# ============================================================================
# Models
# ============================================================================


class Enterprise(BaseModel):
    """Top-tier tenant. Holds SSO/SAML config, billing, plan tier, and
    enterprise-wide knowledge scope. Contains organizations.
    """

    enterprise_id: str
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    plan_tier: EnterprisePlanTier = EnterprisePlanTier.FREE
    max_members: int = 5
    max_cases: Optional[int] = None
    billing_email: Optional[str] = None
    #: The verified email domain this enterprise is the tenant for, case-folded
    #: (ADR-017 D3), or ``None`` for a personal enterprise — a consumer-mail
    #: account gets an enterprise of its own and no domain claims it.
    domain: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        """Mirror of the DB enterprises_name_not_empty CHECK: name must
        not be whitespace-only. Pydantic ``min_length=1`` accepts a single
        space; the DB ``LENGTH(TRIM(name)) > 0`` rejects it. Same rule, two
        layers — neither bypassable independently."""
        if not v.strip():
            raise ValueError("name must not be whitespace-only")
        return v


class Organization(BaseModel):
    """Organization (customer tenant) model. Lives under an enterprise."""

    organization_id: str
    enterprise_id: Optional[str] = None
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    description: Optional[str] = None
    plan_tier: OrgPlanTier = OrgPlanTier.FREE
    max_members: int = 5
    max_cases: Optional[int] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    #: Mirrors the ``organizations.is_active`` column. Distinct from
    #: ``deleted_at``: a deactivated organization still exists (and its data
    #: with it) but must not accept logins — the SSO org-mapping path refuses
    #: to land a user in one (#869).
    is_active: bool = True
    #: Operator override for the per-UTC-day investigation-turn cap
    #: (ADR-016 D5.3). Three-valued because the policy it overrides is itself
    #: conditional: ``None`` = no override (a personal tenant takes the
    #: deployment default, a company tenant is uncapped), ``0`` = explicitly
    #: uncapped, ``N > 0`` = capped at N turns per UTC day. Carried on the
    #: domain object rather than read off the ORM by the enforcement, so the
    #: cap resolves through the same repository every other organization read
    #: goes through — and inherits its ``deleted_at`` filter.
    daily_turn_cap: Optional[int] = Field(default=None, ge=0)
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        """Mirror of the DB organizations_name_not_empty CHECK: name must
        not be whitespace-only. Pydantic ``min_length=1`` accepts a single
        space; the DB ``LENGTH(TRIM(name)) > 0`` rejects it. Same rule, two
        layers — neither bypassable independently."""
        if not v.strip():
            raise ValueError("name must not be whitespace-only")
        return v


class OrganizationMember(BaseModel):
    """User membership in organization."""

    user_id: str
    organization_id: str
    role_id: str  # References roles.role_id
    joined_at: datetime
    last_active_at: Optional[datetime] = None


class Team(BaseModel):
    """Team — the sharing unit (ADR-017 D4).

    Parented by the ENTERPRISE, not by an organization: a team may span cost
    centres, and its members must be in the same enterprise. It references no
    organization at all.
    """

    team_id: str
    enterprise_id: str
    name: str = Field(min_length=1)
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        """Mirror of the DB teams_name_not_empty CHECK: name must not be
        whitespace-only. Pydantic ``min_length=1`` accepts a single space;
        the DB ``LENGTH(TRIM(name)) > 0`` rejects it. Same rule, two layers
        — neither bypassable independently."""
        if not v.strip():
            raise ValueError("name must not be whitespace-only")
        return v


class TeamMember(BaseModel):
    """User membership in team."""

    user_id: str
    team_id: str
    team_role: Optional[str] = None  # 'lead', 'member', or custom
    joined_at: datetime


class TeamInvitationStatus(str, Enum):
    """The four states an offer to join a team can be in.

    Mirrors the DB ``team_invitations_status_check`` CHECK at the domain layer.
    Same rule, two layers — neither bypassable independently.

    ``REVOKED`` is reached from two directions: the team admin withdrawing the
    offer and the invitee declining it. They are one *state* — the offer is off
    the table and grants nothing — and the row tells them apart by whether
    ``revoked_by`` is the inviter or the invitee. A fifth value would have said
    the same thing while widening the CHECK every reader parses.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class TeamInvitation(BaseModel):
    """An offer to join a team, and the consent record that answers it (D4).

    A pending invitation grants **nothing**: it is the thing that keeps a
    stranger from pulling a colleague into a team's view without a word
    (ADR-017 D4). Membership is created only by the invitee's own accept.

    ``email`` is always present and always ``strip().lower()``-normalised —
    matching ``func.lower(users.email)``, which is the index the account lookup
    uses; see ``normalize_invitation_email``. ``invited_user_id`` is the account
    that address resolved to and is ``None`` until it has one. An
    address with no account yet can be invited, and the invitation resolves
    when that address signs up **and lands in the same enterprise** (D4). An
    address that signs up into a different enterprise never resolves — the
    invitation stays pending until it expires, because nothing crosses an
    enterprise line (D2).
    """

    invitation_id: str
    enterprise_id: str
    team_id: str
    #: Lower-cased and trimmed by every writer, never case-folded: the key has
    #: to match ``func.lower(users.email)`` or the account lookup misses. The
    #: comparison is exact — an address is a key here, and two spellings of one
    #: address must not be two invitations.
    email: str
    invited_user_id: Optional[str] = None
    invited_by: Optional[str] = None
    status: TeamInvitationStatus = TeamInvitationStatus.PENDING
    created_at: datetime
    expires_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    revoked_at: Optional[datetime] = None

    def is_expired(self, now: datetime) -> bool:
        """Whether this offer has run out, judged at ``now``.

        Expiry is **lazy**: no sweeper walks the table, so a row can carry
        ``status='pending'`` past its ``expires_at`` and it is the read and the
        accept that notice. Reading it from the row rather than from a stored
        status is what makes the two agree without a job in between.

        An invitation with no ``expires_at`` never expires. That is not the
        shape the API mints — every invitation it creates carries one — but a
        row written by an older path or by hand must not be read as expired the
        instant it is seen.
        """
        if self.expires_at is None:
            return False
        deadline = self.expires_at
        if deadline.tzinfo is None:
            # SQLite hands back naive datetimes; the rule is UTC either way.
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= now


class Role(BaseModel):
    """RBAC role definition.

    ``scope`` is typed as :class:`RoleScope` to mirror the DB
    ``roles_scope_check`` CHECK at the domain layer. Same rule, two
    layers — neither bypassable independently.
    """

    role_id: str
    name: str
    description: Optional[str] = None
    scope: RoleScope
    is_system_role: bool = False
    created_at: datetime


class Permission(BaseModel):
    """RBAC permission definition."""

    permission_id: str
    resource: str  # 'cases', 'knowledge_base', 'teams', etc.
    action: str  # 'read', 'write', 'delete', 'manage'
    description: Optional[str] = None


class UserAuditLog(BaseModel):
    """User audit log entry."""

    audit_id: int
    user_id: str
    event_type: AuditEventType
    event_category: AuditCategory
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    session_id: Optional[str] = None
    enterprise_id: Optional[str] = None
    organization_id: Optional[str] = None
    event_at: datetime
    success: bool = True


# ============================================================================
# Repository Interfaces
# ============================================================================


class IEnterpriseRepository(ABC):
    """Interface for enterprise (top-tier tenant) persistence operations."""

    @abstractmethod
    async def create_enterprise(self, enterprise: Enterprise) -> Enterprise:
        """Create a new enterprise."""

    @abstractmethod
    async def get_enterprise(self, enterprise_id: str) -> Optional[Enterprise]:
        """Get enterprise by ID. Returns None if not found or soft-deleted."""

    @abstractmethod
    async def get_enterprise_by_slug(self, slug: str) -> Optional[Enterprise]:
        """Get enterprise by slug. Returns None if not found or soft-deleted."""

    @abstractmethod
    async def update_enterprise(self, enterprise: Enterprise) -> bool:
        """Update enterprise. Returns True if a row was updated."""

    @abstractmethod
    async def find_live_by_domain(self, domain: str) -> Optional[Enterprise]:
        """The LIVE enterprise for an email domain, or ``None``. Reads only.

        The half of :meth:`get_or_create_for_domain` that writes nothing, so a
        caller can find out whether admitting this login would CREATE a tenant
        before it decides whether the login may be admitted at all. Without it
        the sign-up path had to write first and could only refuse afterwards,
        leaving a live enterprise for the company's domain behind every refusal
        — a row every later org-less sign-up from that domain then resolves to.

        Case-folded like the writer, and scoped to live rows like the partial
        unique index, so the two agree on what "already exists" means.
        """

    @abstractmethod
    async def get_or_create_for_domain(
        self, *, domain: str, name: str, slug: str
    ) -> Enterprise:
        """The enterprise for an email domain, creating it if it is the first.

        The sign-up derivation of ADR-017 D3: every address at a non-consumer
        domain lands in one enterprise, and the first account from that domain
        is what brings it into existence. Being first confers nothing — the
        enterprise has no administrator until a domain claim is verified (D7).

        Keyed on ``enterprises.domain`` among LIVE rows, which is exactly the
        scope of its partial unique index, so a retired enterprise neither
        blocks the next sign-up nor is handed back to it.
        """


class IOrganizationRepository(ABC):
    """Interface for organization data persistence operations."""

    @abstractmethod
    async def create_organization(self, org: Organization) -> Organization:
        """Create a new organization.

        Args:
            org: Organization object to create

        Returns:
            Created organization with generated ID
        """
        pass

    @abstractmethod
    async def get_organization(self, organization_id: str) -> Optional[Organization]:
        """Get organization by ID.

        Args:
            organization_id: Organization identifier

        Returns:
            Organization if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug.

        Args:
            slug: Organization slug (URL-friendly identifier)

        Returns:
            Organization if found, None otherwise
        """
        pass

    @abstractmethod
    async def update_organization(self, org: Organization) -> bool:
        """Update organization.

        Args:
            org: Organization object with updates

        Returns:
            True if update was successful
        """
        pass

    @abstractmethod
    async def delete_organization(self, organization_id: str) -> bool:
        """Soft delete organization.

        Args:
            organization_id: Organization identifier

        Returns:
            True if deletion was successful
        """
        pass

    @abstractmethod
    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to.

        Args:
            user_id: User identifier

        Returns:
            List of organizations
        """
        pass

    @abstractmethod
    async def add_member(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Add user to organization with role.

        Args:
            organization_id: Organization identifier
            user_id: User identifier
            role_id: Role to assign

        Returns:
            True if member was added successfully
        """
        pass

    @abstractmethod
    async def remove_member(self, organization_id: str, user_id: str) -> bool:
        """Remove user from organization.

        Args:
            organization_id: Organization identifier
            user_id: User identifier

        Returns:
            True if member was removed successfully
        """
        pass

    @abstractmethod
    async def update_member_role(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Update user's role in organization.

        Args:
            organization_id: Organization identifier
            user_id: User identifier
            role_id: New role to assign

        Returns:
            True if role was updated successfully
        """
        pass

    @abstractmethod
    async def list_organization_members(
        self, organization_id: str
    ) -> List[OrganizationMember]:
        """List all members of an organization.

        Args:
            organization_id: Organization identifier

        Returns:
            List of organization members
        """
        pass

    @abstractmethod
    async def get_member_role(
        self, organization_id: str, user_id: str
    ) -> Optional[str]:
        """Get user's role in organization.

        Args:
            organization_id: Organization identifier
            user_id: User identifier

        Returns:
            Role ID if user is member, None otherwise
        """
        pass

    @abstractmethod
    async def user_has_permission(
        self, user_id: str, organization_id: str, permission: Union[PermissionEnum, str]
    ) -> bool:
        """Check if user has permission in organization.

        Args:
            user_id: User identifier
            organization_id: Organization identifier
            permission: A :class:`~faultmaven.models.rbac.Permission` member, or
                its ``resource:action`` value (e.g. ``'cases:write'``)

        Returns:
            True if user has permission; False for a permission that cannot be
            parsed — this check fails closed.
        """
        pass


class TeamNameTakenError(Exception):
    """A live team in this enterprise already carries that name.

    Raised by the repository rather than returned, because the caller has
    nothing useful to do with the distinction except turn it into a refusal —
    and the alternative (let the ``IntegrityError`` escape) is a 500 for an
    ordinary, user-caused collision. Narrow on purpose: it names ONE constraint,
    so a different violation still surfaces as itself rather than being
    reported as a duplicate name.
    """


class LeaveOutcome(str, Enum):
    """What :meth:`ITeamRepository.leave_team` did, decided under a row lock.

    The last-admin rule is decided inside the repository — unusually, and for
    one reason: it is a read-then-write over the roster, and read-then-write is
    exactly what two admins leaving at the same instant defeat. Both would pass
    a check made outside the transaction and the team would be left with no
    admin at all, which no endpoint can undo because there is no promote route.
    Deciding it under the same lock that performs the removal is the only
    version of the rule that is true.
    """

    #: The member was removed; the team remains.
    LEFT = "left"
    #: The member was the last one; the team is soft-deleted and its pending
    #: invitations revoked in the same transaction (they would otherwise name a
    #: team nobody can see).
    LEFT_AND_RETIRED = "left_and_retired"
    #: Refused: the leaver is the team's only admin and others remain.
    LAST_ADMIN = "last_admin"
    #: The team does not exist in this enterprise, or the caller is not in it.
    ABSENT = "absent"


class AcceptOutcome(str, Enum):
    """What :meth:`ITeamRepository.accept_invitation` did, in one transaction.

    The accept is the one operation on this surface that writes *two* rows — the
    consent record and the membership it creates — and the two must land or
    neither must. Ordering them differently only moves which half is left
    behind: stamping first spends a one-shot token the retry then cannot use;
    upserting first leaves a membership nobody consented to when the offer turns
    out to have been withdrawn. Neither ordering is safe, so there is no
    ordering — there is a transaction.
    """

    #: Both rows written: the invitation is `accepted` and the membership exists.
    ACCEPTED = "accepted"
    #: The invitation was not pending when the transaction reached it (answered,
    #: withdrawn or expired). **Nothing was written**, membership included.
    NOT_PENDING = "not_pending"
    #: The invitation, or its team, is not in this enterprise; or the account
    #: cannot be a member of that team. Nothing was written.
    ABSENT = "absent"


class ITeamRepository(ABC):
    """Interface for team data persistence operations."""

    @abstractmethod
    async def create_team(self, team: Team) -> Team:
        """Create a new team, with no members.

        The bootstrap form, used by the single-tenant default-team seeding.
        Consent-formed teams go through :meth:`create_team_with_admin`, which
        writes the creator's membership in the same transaction.

        Args:
            team: Team object to create

        Returns:
            Created team

        Raises:
            TeamNameTakenError: a live team in that enterprise has that name
        """
        pass

    @abstractmethod
    async def create_team_with_admin(
        self, team: Team, admin_user_id: str, team_role: str
    ) -> Optional[Team]:
        """Create a team and its creator's membership as ONE transaction.

        Two writes and a compensating third is not the same thing: the
        compensation can itself fail, and until it runs (or if it does not) the
        enterprise holds a team nobody is in — invisible to every read here,
        which are all gated on membership, and holding its name against the
        partial unique index.

        Args:
            team: Team object to create
            admin_user_id: The creator, written as a member in the same
                transaction
            team_role: The role to stamp on that membership

        Returns:
            The created team, or ``None`` when the creator cannot be a member of
            it (anchored to another enterprise, or the account does not
            resolve) — in which case nothing is written at all.

        Raises:
            TeamNameTakenError: a live team in that enterprise has that name
        """
        pass

    @abstractmethod
    async def get_team(self, enterprise_id: str, team_id: str) -> Optional[Team]:
        """Get a team by id, within an enterprise.

        The ``enterprise_id`` is a parameter rather than a predicate the caller
        remembers to apply: on this port a missing tenant predicate is then a
        signature error, not a matter of discipline. RLS covers the deployed
        path, but the rule must also hold on SQLite and on any owner-role
        connection, and a probe cannot tell "scoped" from "happened not to
        cross" when the scope is implicit.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier

        Returns:
            Team if it exists in that enterprise and is not soft-deleted,
            otherwise None — the two are deliberately indistinguishable
        """
        pass

    @abstractmethod
    async def get_team_with_members(
        self, enterprise_id: str, team_id: str
    ) -> tuple[Optional[Team], List[TeamMember]]:
        """The team and its roster, in one session.

        Every team-addressed operation needs both — the team to check the
        tenant, the roster to decide membership and the admin role — and asking
        three times opened three sessions to answer one question.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier

        Returns:
            ``(team, members)``; ``(None, [])`` when the team is absent from
            that enterprise
        """
        pass

    @abstractmethod
    async def get_team_names(self, enterprise_id: str, team_ids: List[str]) -> dict:
        """Map team ids to names, for ids in ``enterprise_id``. One query.

        Serves the invitee's own list, where the caller is not a member of the
        teams being named and so cannot use the membership-gated reads. Absent
        and out-of-enterprise ids are simply missing from the mapping.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_ids: The ids to resolve

        Returns:
            ``{team_id: name}`` for every live team of that enterprise in the
            list
        """
        pass

    @abstractmethod
    async def update_team(self, enterprise_id: str, team: Team) -> bool:
        """Update team, within an enterprise.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team: Team object with updates

        Returns:
            True if update was successful
        """
        pass

    @abstractmethod
    async def leave_team(
        self, enterprise_id: str, team_id: str, user_id: str, admin_role: str
    ) -> "LeaveOutcome":
        """Remove ``user_id`` from ``team_id``, deciding the last-admin rule.

        One transaction, holding a row lock on the team where the dialect has
        one, because the rule it enforces is a read-then-write over the roster:
        two admins leaving at the same instant both pass a check made outside
        the lock, and the team is left permanently unadministrable.

        Retires the team when the leaver was its last member, and revokes that
        team's pending invitations in the same transaction — an offer to a team
        nobody can see can be neither accepted nor declined.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier
            user_id: The member leaving
            admin_role: The value of ``team_members.team_role`` that counts as
                an admin. Passed in rather than hardcoded so the vocabulary
                stays owned by the domain layer, which is where the rest of it
                lives.

        Returns:
            What happened, as a :class:`LeaveOutcome`
        """
        pass

    @abstractmethod
    async def list_enterprise_teams(self, enterprise_id: str) -> List[Team]:
        """List all teams in an enterprise.

        Args:
            enterprise_id: Enterprise identifier

        Returns:
            List of teams
        """
        pass

    @abstractmethod
    async def list_user_teams(self, user_id: str) -> List[Team]:
        """List the teams a user belongs to (full ``Team`` objects, with names).

        The object-returning sibling of ``list_all_user_team_ids`` — same
        membership resolution (JOIN ``team_members`` through the RLS-tenanted
        ``teams`` table, excluding soft-deleted teams), so under the caller's
        enterprise RLS context it returns only teams in that enterprise. Used by
        the ``GET /teams`` read path (team picker + id→name resolution).

        Args:
            user_id: User identifier

        Returns:
            List of teams (empty when the user belongs to none)
        """
        pass

    @abstractmethod
    async def add_member(
        self,
        enterprise_id: str,
        team_id: str,
        user_id: str,
        team_role: Optional[str] = None,
    ) -> bool:
        """Add user to team, within an enterprise.

        The tenant predicate is a parameter here for a sharper reason than on
        the reads: a write that addresses a row by bare id does not merely
        *observe* another tenant's data, it changes it. Every write on this port
        carries the enterprise for that reason, so a missing predicate is a
        signature error rather than a matter of discipline.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier
            user_id: User identifier
            team_role: Optional team-specific role ('lead', 'member')

        Returns:
            True if member was added successfully
        """
        pass

    @abstractmethod
    async def list_team_members(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamMember]:
        """List all members of a team, within an enterprise.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier

        Returns:
            List of team members (empty when the team is absent from that
            enterprise)
        """
        pass

    @abstractmethod
    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        """List every team id a user belongs to (KB scope resolution).

        Lightweight id-only projection used to build the team arm of a
        principal's KB read scope (``build_kb_scope_filter``). Returns ids
        only — no full ``Team`` objects — because the sole consumer needs a
        membership set, not team metadata.

        Isolation posture: implementations MUST resolve membership by joining
        ``team_members`` through the ``teams`` table (which carries
        ``enterprise_id`` and is RLS-tenanted), so that under the limited
        ``faultmaven_app`` role a cross-enterprise membership row fails closed.
        ``team_members`` carries no tenant column of its own — it is a pure
        ``(user_id, team_id)`` join — and its RLS policy reaches the key by that
        same hop, so the join IS the isolation boundary. A database trigger
        (``team_members_same_enterprise``) additionally refuses a member whose
        own enterprise is not the team's. See ADR-017 D1/D4.

        Args:
            user_id: User identifier

        Returns:
            List of team_id strings (empty when the user has no memberships —
            the standalone/self-hosted case, where team collaboration is inert).
        """
        pass

    # -- invitations: the consent that forms a team (ADR-017 D4) ------------ #
    #
    # On this interface rather than on an ``ITeamInvitationRepository`` of its
    # own because team *membership* already lives here: an invitation is the
    # consent record for exactly the membership ``add_member`` writes, the two
    # are written by one service through one sessionless wrapper, and an accept
    # touches both in sequence. Splitting them would put a single decision
    # behind two ports for no boundary either side needs.

    @abstractmethod
    async def create_invitation(self, invitation: TeamInvitation) -> TeamInvitation:
        """Persist a new invitation, or return the live one that beat it.

        The caller has already decided the invitation is legitimate (the domain
        rule in ``TeamService.invite``); this only writes it. ``email`` must
        arrive lower-cased, matching the form every reader compares.

        ``ix_team_invitations_pending_unique`` admits one PENDING offer per
        address per team, so two admins inviting the same address at the same
        instant race. The loser **re-reads and returns the winner's row** rather
        than raising: "inviting an address that already has a live offer returns
        that offer" is the documented behaviour of this endpoint, and a 500 for
        two people doing the same reasonable thing at once is not.

        Args:
            invitation: The invitation to store

        Returns:
            The stored invitation, or the live offer that already existed
        """
        pass

    @abstractmethod
    async def get_invitation(
        self, enterprise_id: str, invitation_id: str
    ) -> Optional[TeamInvitation]:
        """Get one invitation, scoped to an enterprise.

        The ``enterprise_id`` predicate is explicit rather than left to RLS for
        the reason ``add_member`` compares anchors in Python: this is the read
        that decides whether a caller may act on an id they supplied, and it
        must hold on SQLite (which has no RLS) exactly as it holds under the
        limited ``faultmaven_app`` role.

        Args:
            enterprise_id: The enterprise the caller is bound to
            invitation_id: Invitation identifier

        Returns:
            The invitation, or None when it does not exist in that enterprise
            (the two are deliberately indistinguishable — see ADR-017 D2)
        """
        pass

    @abstractmethod
    async def find_pending_invitation(
        self, enterprise_id: str, team_id: str, email: str
    ) -> Optional[TeamInvitation]:
        """The live offer for ``email`` on ``team_id``, if there is one.

        Backs the idempotent re-invite: inviting an address that already has a
        pending offer returns that offer instead of minting a second row.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier
            email: The address, lower-cased as every writer stores it

        Returns:
            The pending invitation, or None
        """
        pass

    @abstractmethod
    async def list_team_invitations(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamInvitation]:
        """Every invitation ever issued for a team, newest first.

        Not filtered by status: the team admin's view is the record of who was
        offered a place and what became of the offer.

        Args:
            enterprise_id: The enterprise the caller is bound to
            team_id: Team identifier

        Returns:
            List of invitations (empty when none)
        """
        pass

    @abstractmethod
    async def list_invitations_for_invitee(
        self, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The PENDING invitations addressed to one account.

        Addressed two ways, because an invitation may predate the account:
        by ``invited_user_id`` once it resolved, and by ``email`` while it has
        not. Both arms are confined to ``enterprise_id`` — an invitation is
        answerable only by an account inside the enterprise that issued it.

        Args:
            enterprise_id: The enterprise the caller is bound to
            user_id: The caller's account id
            email: The caller's address, lower-cased as it is stored

        Returns:
            Pending invitations, newest first (empty when none)
        """
        pass

    @abstractmethod
    async def accept_invitation(
        self,
        enterprise_id: str,
        invitation_id: str,
        user_id: str,
        team_role: str,
        at: datetime,
    ) -> tuple["AcceptOutcome", Optional[Team]]:
        """Stamp the invitation accepted AND write the membership. One transaction.

        The consent that forms a team writes two rows, and the surface's central
        invariant — *a withdrawn offer grants nothing* — is a statement about
        both of them together. Two separate commits cannot make that statement
        in either order:

        * stamp first, upsert second: a failed upsert leaves the offer spent and
          the invitee with no membership, and no retry can help because the row
          that would have authorised it is gone;
        * upsert first, stamp second: a revoke landing in between leaves a
          **real membership** behind while the caller is told 409 — a member of
          a team nobody consented to admit.

        So it is one transaction, taking a row lock on the invitation first, in
        the shape :meth:`create_team_with_admin` and :meth:`leave_team` already
        use. A concurrent revoke either lands wholly before it (the pending
        predicate then matches nothing and NOTHING is written) or waits.

        Args:
            enterprise_id: The enterprise the caller is bound to
            invitation_id: Invitation identifier
            user_id: The accepting account
            team_role: The role to stamp on the new membership
            at: Acceptance timestamp

        Returns:
            ``(outcome, team)`` — the team only when the outcome is
            :attr:`AcceptOutcome.ACCEPTED`. Every other outcome wrote nothing.
        """
        pass

    @abstractmethod
    async def mark_invitation_revoked(
        self, enterprise_id: str, invitation_id: str, by_user_id: str, at: datetime
    ) -> bool:
        """Stamp an invitation revoked, but only if it is still pending.

        One method for both endings — the admin withdrawing the offer and the
        invitee declining it. ``by_user_id`` is what tells them apart later.

        Args:
            enterprise_id: The enterprise the caller is bound to
            invitation_id: Invitation identifier
            by_user_id: Who ended it
            at: Timestamp

        Returns:
            True when this call was the one that ended it
        """
        pass

    @abstractmethod
    async def expire_invitations(
        self, enterprise_id: str, invitation_ids: List[str]
    ) -> List[str]:
        """Stamp pending invitations expired, and report WHICH ones moved.

        Expiry is lazy — there is no sweeper — so this is called by the read or
        the accept that first notices ``expires_at`` has passed. Idempotent: a
        row already answered is not pending and the UPDATE skips it.

        Takes a list rather than one id because a list read settles every
        elapsed row it saw, and doing that one commit at a time made the cost of
        reading a stale mailbox linear in how stale it was.

        **Returns the ids it actually moved, not a count**, and the caller must
        use that rather than assuming its whole candidate list was stamped. A
        row accepted or revoked between the caller's read and this UPDATE is
        skipped by the pending predicate — reporting it as expired anyway told
        the caller 410 for an invitation that had in fact been accepted, which
        is the very "depends who read first" inconsistency lazy expiry was made
        consistent to remove.

        Args:
            enterprise_id: The enterprise the caller is bound to
            invitation_ids: Invitation identifiers (an empty list is a no-op)

        Returns:
            The ids this call moved to ``expired``
        """
        pass

    @abstractmethod
    async def resolve_invitations_for_account(
        self, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """Stamp ``user_id`` on every unresolved pending invitation for ``email``.

        The sign-up hook (ADR-017 D4, open item 2). An invitation may be issued
        to an address with no account; it resolves when that address signs up
        **and lands in this enterprise**. Confined to ``enterprise_id`` for that
        reason: an address that signs up somewhere else must leave the offer
        unresolved, to expire where it was issued.

        Idempotent by construction — it only touches rows whose
        ``invited_user_id`` is still NULL — so a login that runs it twice, or a
        retry after a failure, changes nothing the first run did not.

        Args:
            enterprise_id: The enterprise the account just anchored to
            email: The account's address, lower-cased as it is stored
            user_id: The account id to stamp

        Returns:
            How many invitations were resolved (0 is the ordinary answer)
        """
        pass


class IAuditRepository(ABC):
    """Interface for audit log persistence operations."""

    @abstractmethod
    async def log_event(
        self,
        user_id: str,
        event_type: AuditEventType,
        event_category: AuditCategory,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        success: bool = True,
    ) -> bool:
        """Log an audit event.

        Args:
            user_id: User who performed the action
            event_type: Type of event
            event_category: Event category
            resource_type: Type of resource affected
            resource_id: ID of resource affected
            details: Additional event details
            ip_address: Client IP address
            user_agent: Client user agent
            session_id: Session identifier
            organization_id: Organization context
            success: Whether action succeeded

        Returns:
            True if event was logged successfully
        """
        pass

    @abstractmethod
    async def get_user_audit_log(
        self, user_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for a user.

        Args:
            user_id: User identifier
            limit: Maximum results to return
            offset: Pagination offset

        Returns:
            List of audit log entries
        """
        pass

    @abstractmethod
    async def get_enterprise_audit_log(
        self, enterprise_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for an enterprise.

        Args:
            enterprise_id: Enterprise identifier
            limit: Maximum results to return
            offset: Pagination offset

        Returns:
            List of audit log entries
        """
        pass
