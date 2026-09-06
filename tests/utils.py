"""Centralized test utilities.

Provides common utilities for test ID generation, test data creation,
and other shared test infrastructure.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional
from uuid import uuid4

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)

if TYPE_CHECKING:  # static analysis only — see make_org_knowledge_item
    from starlette.requests import Request

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        KnowledgeItem,
        KnowledgeItemType,
    )

# Default enterprise UUID — mirrors SingleTenantProvider.DEFAULT_ENTERPRISE_ID
# and migration 006's seeded row. Tests that build orgs/users without a
# specific enterprise context anchor to this row.
DEFAULT_TEST_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


def reset_settings_singleton() -> None:
    """Clear the settings singleton on the *live* settings module.

    Deliberately late-bound. A module-level
    ``from faultmaven.config.settings import reset_settings`` captures the
    function belonging to whichever module object existed when the test module
    was imported. Anything that drops ``faultmaven.config.settings`` from
    ``sys.modules`` puts a *second* module object in the process, each with its
    own ``_settings_instance``: the captured function then clears a singleton
    nothing reads, while production code — which re-imports by name on every
    call — keeps handing out the other module's stale cached settings. Looking
    the module up through ``sys.modules`` at call time cannot drift that way.
    """
    import faultmaven.config.settings as settings_module

    settings_module.reset_settings()


def get_live_settings():
    """Return the settings from the *live* settings module.

    The read-side counterpart to :func:`reset_settings_singleton`, and
    late-bound for the same reason: a captured ``get_settings`` can answer from
    a different module object than the one production code reads.
    """
    import faultmaven.config.settings as settings_module

    return settings_module.get_settings()


# Fields carrying the single-source JWT token expiry (#888). Field names, not env
# names — the env names are derived from their declared aliases below.
JWT_EXPIRY_FIELDS = ("jwt_access_token_expire_minutes", "jwt_refresh_token_expire_days")

# The env spellings that have addressed those fields and no longer do, each
# mapped to the current name that replaces it. Stated INDEPENDENTLY of
# ``RETIRED_JWT_EXPIRY_ENV_NAMES`` on purpose: this is the historical record, and
# the guard's rejection cases are parametrised from it so that dropping a name
# from the production map turns those cases RED rather than silently deleting
# them. A test whose coverage is defined by the thing under test cannot detect
# that thing shrinking. The two are pinned equal by
# ``test_jwt_expiry_env_units.py``.
RETIRED_JWT_EXPIRY_SPELLINGS = {
    # Pre-#832: the original unsuffixed ``validation_alias`` pair, before the
    # names were made to carry their unit.
    "JWT_ACCESS_TOKEN_EXPIRY": "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRY": "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
    # Pre-#888: the EXPIRE spelling, which bound the duplicate declaration on the
    # security half by field name.
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
}


def jwt_expiry_env_names() -> tuple[str, ...]:
    """Every env name that has ever addressed the JWT expiry fields (#888).

    Tests about *which names bind* must clear all of them first, or an ambient
    ``.env`` decides the outcome. The list is DERIVED rather than restated in
    each test module: the current pair comes from the ``validation_alias``
    actually declared on ``AuthSettings``, so a future rename updates every
    fixture at once instead of leaving hardcoded tuples that quietly stop
    covering the names they are named for.

    The retired names are the union of the production map and
    :data:`RETIRED_JWT_EXPIRY_SPELLINGS`, so the clear stays complete even if the
    production map loses an entry — the fixture must not stop clearing a name
    just because the guard stopped rejecting it.

    Late-bound for the same reason as :func:`get_live_settings` — the constants
    are read off the *live* settings module at call time.
    """
    import faultmaven.config.settings as settings_module

    current = tuple(
        str(settings_module.AuthSettings.model_fields[field].validation_alias)
        for field in JWT_EXPIRY_FIELDS
    )
    retired = tuple(settings_module.RETIRED_JWT_EXPIRY_ENV_NAMES) + tuple(
        RETIRED_JWT_EXPIRY_SPELLINGS
    )
    return tuple(dict.fromkeys(current + retired))


async def seed_default_enterprise(session) -> None:
    """Insert the default enterprise row used as parent for test orgs/users.

    Idempotent: returns silently if the row already exists.
    """
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    existing = await session.get(EnterpriseModel, DEFAULT_TEST_ENTERPRISE_ID)
    if existing is not None:
        return
    session.add(
        EnterpriseModel(
            enterprise_id=DEFAULT_TEST_ENTERPRISE_ID,
            name="Default Test Enterprise",
            slug="default-test",
        )
    )
    await session.commit()


async def seed_users(
    session,
    user_ids: Iterable[str],
    enterprise_id: Optional[str] = None,
) -> None:
    """Insert user rows so FK-bound tests can reference them.

    `cases.user_id` is a nullable FK to `users.user_id` with
    `ondelete="SET NULL"`. With PRAGMA foreign_keys=ON, hand-crafted user IDs
    that don't exist in `users` fail the FK. Seed minimal valid stubs.

    `users.enterprise_id` is NOT NULL (FK to `enterprises.enterprise_id`).
    The default enterprise row is created automatically; pass an explicit
    `enterprise_id` to anchor users elsewhere.

    The UserModel has unique constraints on `username` and `email`; we derive
    both from the user_id to avoid collisions in batch.
    """
    from faultmaven.infrastructure.persistence.models import UserModel

    enterprise_id = enterprise_id or DEFAULT_TEST_ENTERPRISE_ID
    await seed_default_enterprise(session)

    seen_usernames: set[str] = set()
    seen_emails: set[str] = set()
    for user_id in user_ids:
        existing = await session.get(UserModel, user_id)
        if existing is not None:
            continue
        username = user_id
        suffix = 0
        while username in seen_usernames:
            suffix += 1
            username = f"{user_id}-{suffix}"
        seen_usernames.add(username)
        email = f"{username}@test.local"
        while email in seen_emails:
            suffix += 1
            email = f"{username}-{suffix}@test.local"
        seen_emails.add(email)
        session.add(
            UserModel(
                user_id=user_id,
                enterprise_id=enterprise_id,
                username=username,
                email=email,
                display_name=f"Test User {user_id}",
                hashed_password="x" * 60,  # bcrypt-shaped placeholder
            )
        )
    await session.commit()


async def seed_enterprises(session, enterprise_ids: Iterable[str]) -> None:
    """Insert enterprise rows so FK-bound tests can reference them.

    The enterprise analogue of :func:`seed_organizations`, and the seam ADR-017
    needs: once the enterprise is the isolation boundary, a probe has to be able
    to build two of them without inventing an organization to hang them off.

    ``enterprises.slug`` is unique among live rows (migration 052's partial
    index) and is derived here from the enterprise id, which is the table's
    PRIMARY KEY — so two slugs in one batch can only collide if two ids did,
    which the key already forbids. That is why there is no de-duplication loop
    here and there is one in ``seed_organizations``: an organization's slug is
    unique per ENTERPRISE, so distinct org ids in different enterprises really
    can collide on it. Idempotent per id: an enterprise that already exists is
    left alone rather than re-stamped.
    """
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    for enterprise_id in enterprise_ids:
        existing = await session.get(EnterpriseModel, enterprise_id)
        if existing is not None:
            continue
        session.add(
            EnterpriseModel(
                enterprise_id=enterprise_id,
                name=f"Test Enterprise {enterprise_id}",
                slug=enterprise_id,
            )
        )
    await session.commit()


async def seed_organizations(
    session,
    org_ids: Iterable[str],
    enterprise_id: Optional[str] = None,
) -> None:
    """Insert organization rows so FK-bound tests can reference them.

    `cases.organization_id` is a NOT NULL FK to `organizations`. Tests that
    hand-craft org IDs must seed the matching organization row first to
    satisfy the constraint when PRAGMA foreign_keys=ON.

    `organizations.enterprise_id` is NOT NULL. The default enterprise row
    is created automatically; pass an explicit `enterprise_id` to anchor
    orgs elsewhere.
    """
    from faultmaven.infrastructure.persistence.models import OrganizationModel

    enterprise_id = enterprise_id or DEFAULT_TEST_ENTERPRISE_ID
    await seed_default_enterprise(session)

    seen_slugs: set[str] = set()
    for org_id in org_ids:
        existing = await session.get(OrganizationModel, org_id)
        if existing is not None:
            continue
        # Slugs are unique within an enterprise; derive from org_id but
        # ensure no collision in batch.
        slug = org_id
        suffix = 0
        while slug in seen_slugs:
            suffix += 1
            slug = f"{org_id}-{suffix}"
        seen_slugs.add(slug)
        session.add(
            OrganizationModel(
                organization_id=org_id,
                enterprise_id=enterprise_id,
                name=f"Test Org {org_id}",
                slug=slug,
            )
        )
    await session.commit()


async def seed_teams(
    session,
    team_ids: Iterable[str],
    enterprise_id: Optional[str] = None,
) -> None:
    """Insert team rows so share- and membership-bound tests can reference them.

    A team is parented by its ENTERPRISE (ADR-017 D4): it carries no
    ``organization_id``, and it may span organizations. ``team_members`` reaches
    its tenant key by one hop through ``teams.enterprise_id``, so a membership
    row is only visible once its team exists in the bound enterprise — which is
    why seeding the team is the thing a membership test cannot skip.

    ``teams`` is unique on ``(enterprise_id, name)``; the name is derived from
    the team id, which is the table's PRIMARY KEY, so two names in one batch can
    only collide if two ids did. Idempotent per id.
    """
    from faultmaven.infrastructure.persistence.models import TeamModel

    enterprise_id = enterprise_id or DEFAULT_TEST_ENTERPRISE_ID
    await seed_default_enterprise(session)

    for team_id in team_ids:
        existing = await session.get(TeamModel, team_id)
        if existing is not None:
            continue
        session.add(
            TeamModel(
                team_id=team_id,
                enterprise_id=enterprise_id,
                name=f"Test Team {team_id}",
            )
        )
    await session.commit()


def install_org_autoseed(sync_session) -> None:
    """Auto-create the parent ENTERPRISE and organization rows a flush needs.

    Every tenanted table carries ``enterprise_id NOT NULL`` with an FK
    (ADR-017), and most carry a nullable ``organization_id`` FK beside it. This
    hook seeds whichever parents a pending object references, inside the same
    flush, so FK constraints succeed without churning each test to call
    ``seed_enterprises`` / ``seed_organizations`` by hand. Uses Core INSERT (not
    ``session.add``) so the rows materialize in the current transaction before
    the child INSERTs; objects added via ``session.add()`` inside
    ``before_flush`` defer to the next flush, which is too late to satisfy the
    FK.

    **The enterprise arm is seeded from the referenced ids, not only from the
    default.** Before ADR-017 the only enterprise a test could reference was the
    default one, so ensuring that single row was enough; now every tenanted
    object names an enterprise of its own choosing, and seeding only the default
    would fail the FK for every test that picks its own — which is most of them.

    NOTE: This hook only fires for ORM-mediated writes (via session flush).
    Tests that exercise raw-SQL repositories like ``SQLiteCaseRepository``
    bypass the ORM flush path; those tests should call ``seed_enterprises`` /
    ``seed_organizations`` explicitly before the first save, or wrap the case
    repository with the ``seed_orgs_for_repo`` helper.

    Call once per AsyncSession with `session.sync_session` as the argument.
    """
    from sqlalchemy import event, insert, select

    from faultmaven.infrastructure.persistence.models import (
        EnterpriseModel,
        OrganizationModel,
    )

    @event.listens_for(sync_session, "before_flush")
    def _seed_referenced_tenancy(session, flush_context, instances):
        enterprise_ids: set[str] = set()
        org_ids: set[str] = set()
        for obj in session.new:
            if not isinstance(obj, EnterpriseModel):
                eid = getattr(obj, "enterprise_id", None)
                if eid:
                    enterprise_ids.add(eid)
            if not isinstance(obj, OrganizationModel):
                oid = getattr(obj, "organization_id", None)
                if oid:
                    org_ids.add(oid)

        # An organization needs an enterprise to hang off, so the default is
        # required whenever one is being seeded.
        if org_ids:
            enterprise_ids.add(DEFAULT_TEST_ENTERPRISE_ID)

        if enterprise_ids:
            existing_enterprises = {
                row[0]
                for row in session.execute(
                    select(EnterpriseModel.enterprise_id).where(
                        EnterpriseModel.enterprise_id.in_(enterprise_ids)
                    )
                ).all()
            }
            missing_enterprises = enterprise_ids - existing_enterprises
            if missing_enterprises:
                session.execute(
                    insert(EnterpriseModel),
                    [
                        {
                            "enterprise_id": eid,
                            "name": f"Test Enterprise {eid}",
                            # The slug is unique among live rows, so it has to
                            # be derived from the id rather than shared.
                            "slug": eid,
                        }
                        for eid in sorted(missing_enterprises)
                    ],
                )

        if not org_ids:
            return
        existing = {
            row[0]
            for row in session.execute(
                select(OrganizationModel.organization_id).where(
                    OrganizationModel.organization_id.in_(org_ids)
                )
            ).all()
        }
        missing = org_ids - existing
        if missing:
            session.execute(
                insert(OrganizationModel),
                [
                    {
                        "organization_id": oid,
                        "enterprise_id": DEFAULT_TEST_ENTERPRISE_ID,
                        "name": f"Test Org {oid}",
                        "slug": oid,
                    }
                    for oid in sorted(missing)
                ],
            )


def generate_case_id() -> str:
    """Generate a valid case ID matching the pattern ^case_[a-f0-9]{12}$."""
    return f"case_{uuid4().hex[:12]}"


def generate_item_id() -> str:
    """Generate a valid knowledge item ID."""
    return f"ki_{uuid4().hex[:12]}"


def generate_org_id() -> str:
    """Generate a valid organization ID."""
    return f"org_{uuid4().hex[:12]}"


def generate_evidence_id() -> str:
    """Generate a valid evidence ID."""
    return f"ev_{uuid4().hex[:12]}"


def generate_user_id() -> str:
    """Generate a valid user ID."""
    return f"user_{uuid4().hex[:12]}"


def generate_session_id() -> str:
    """Generate a valid session ID."""
    return f"sess_{uuid4().hex[:12]}"


def generate_agent_execution_id() -> str:
    """Generate a valid agent execution ID."""
    return f"agex_{uuid4().hex[:12]}"


def generate_investigation_session_id() -> str:
    """Generate a valid investigation session ID."""
    return f"is_{uuid4().hex[:12]}"


def make_org_knowledge_item(
    item_id: Optional[str] = None,
    organization_id: Optional[str] = None,
    title: str = "Sample Knowledge Item",
    content: str = "This is sample content for the knowledge item.",
    item_type: "Optional[KnowledgeItemType]" = None,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    embedding_vector: Optional[List[float]] = None,
    is_published: bool = True,
    view_count: int = 0,
    helpful_count: int = 0,
    not_helpful_count: int = 0,
    created_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> "KnowledgeItem":
    """Build a valid org-owned KnowledgeItem for repository-level tests.

    This is the single definition of an org-owned sample item. The repository
    unit tests, the integration suite and the benchmarks all construct through
    here, so a change to the model's tenancy invariants is made in one place
    rather than in three copies that can drift apart.

    Scope is TEAM, an org-owned tier:

    * GLOBAL is the org-free platform tier (#770) and must NOT carry an
      ``organization_id`` (mirrors the ``knowledge_items_global_org_check``
      constraint). It is also the dataclass default, so an item built without
      an explicit scope but *with* an org id fails construction outright.
    * PERSONAL would require an ``owner_id`` backed by a real ``users`` row
      under the FK-on fixtures.

    Suites whose subject *is* the scope rule construct ``KnowledgeItem``
    directly and vary scope themselves — the model's own contract tests
    (``tests/unit/models/test_knowledge_item.py``) and the inventory
    visibility tests (``tests/unit/modules/knowledge/test_documents_inventory.py``).
    They must not route through this helper, which pins one scope by design.

    The knowledge model is imported here rather than at module scope. This
    module is imported by 21 test modules, most of them only for the
    ``generate_*_id`` helpers, and ``faultmaven.modules.knowledge``'s package
    ``__init__`` eagerly pulls in the FastAPI routes and the service layer:
    importing it at module scope took ``import tests.utils`` from 0.40s /
    334 modules to 18.00s / 5472 modules, and dragged in torch. ``item_type``
    defaults to None for the same reason — evaluating the enum member in the
    signature would force the import at def time.
    """
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        KnowledgeItem,
        KnowledgeItemType,
        KnowledgeScope,
    )

    if item_type is None:
        item_type = KnowledgeItemType.TROUBLESHOOTING_GUIDE

    return KnowledgeItem(
        item_id=item_id or generate_item_id(),
        enterprise_id=organization_id or generate_org_id(),
        scope=KnowledgeScope.TEAM,
        title=title,
        content=content,
        item_type=item_type,
        category=category,
        tags=tags or [],
        embedding_vector=embedding_vector,
        is_published=is_published,
        view_count=view_count,
        helpful_count=helpful_count,
        not_helpful_count=not_helpful_count,
        created_at=created_at or datetime.now(timezone.utc),
        metadata=metadata,
    )


def bridge_flat_hypotheses_to_graph(case) -> None:
    """Test helper: project a case's flat hypotheses into degenerate root->D
    chains (the retired Option-1 bridge, removed from production in PR B2c).

    Each not-yet-chained ``Hypothesis`` becomes a ROOT node (the hypothesis
    statement) -> the single PROBLEM node ``D``, carrying the hypothesis's
    evidence on the root and left ``CANDIDATE``. Idempotent; no-op until a
    problem statement exists to anchor ``D``. Kept here (not in production) as a
    compact way to stand up a known degenerate-chain graph for engine-lane tests
    (promote/demote) and the orphan-resolution stub path. Heavy imports are
    deferred so importing this module stays cheap for non-graph tests.
    """
    from faultmaven.core.investigation.causal_graph import seed_problem_node
    from faultmaven.modules.case.contracts import (
        CausalEdge,
        CausalNode,
        NodeEvidenceLink,
        NodeType,
    )

    problem_node = seed_problem_node(case)
    if problem_node is None:
        return  # nothing to anchor on yet
    d_id = problem_node.node_id

    for hyp in case.hypotheses.values():
        if hyp.root_node_id:
            continue
        root = CausalNode(
            statement=hyp.statement[:500],
            node_type=NodeType.ROOT,
            category=hyp.category,
            generated_at_turn=hyp.generated_at_turn,
            evidence_links=[
                NodeEvidenceLink(
                    evidence_id=link.evidence_id,
                    stance=link.stance,
                    reasoning=link.reasoning,
                    stance_confidence=link.stance_confidence,
                )
                for link in hyp.evidence_links
            ],
        )
        case.causal_nodes[root.node_id] = root
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=root.node_id,
                effect_node_id=d_id,
                created_at_turn=hyp.generated_at_turn,
            )
        )
        hyp.root_node_id = root.node_id
        hyp.path = [root.node_id, d_id]


class InMemoryRevocationStore(ITokenRevocationStore):
    """In-memory implementation of the full token-revocation contract.

    Records what production Redis would: revoked JTIs with their TTLs, and
    per-user revocation watermarks. Tests may inspect ``revoked`` and
    ``user_watermarks`` directly.

    Subclasses ``ITokenRevocationStore`` on purpose. The #767 bug was a store
    that silently did not implement the interface — the resulting
    AttributeError was swallowed by the fail-open request-path check — so a
    duck-typed double is exactly the shape to avoid here: adding an interface
    method must break these tests loudly at instantiation instead of quietly
    reading as "not revoked".
    """

    def __init__(self) -> None:
        self.revoked: dict[str, int] = {}
        self.user_watermarks: dict[str, tuple[float, int]] = {}

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        self.revoked[jti] = ttl

    async def is_revoked(self, jti: str) -> bool:
        return jti in self.revoked

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: float, ttl: int
    ) -> None:
        self.user_watermarks[user_id] = (revoked_at, ttl)

    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        entry = self.user_watermarks.get(user_id)
        return entry is not None and issued_at <= int(entry[0])

    async def clear_user_revocation_if_before(
        self, user_id: str, instant: float
    ) -> bool:
        entry = self.user_watermarks.get(user_id)
        if entry is None or not entry[0] < instant:
            return False
        del self.user_watermarks[user_id]
        return True

    async def cleanup_expired(self) -> int:
        return 0


async def asgi_request(app, method: str, path: str, **kwargs):
    """Issue one request against an ASGI app over httpx's in-process transport.

    Used by the API-layer regression modules instead of
    ``fastapi.testclient.TestClient``, which creates a fresh event loop per
    request; async fakeredis-backed infrastructure elsewhere in this suite then
    raises "bound to a different event loop", surfacing as a confusing
    unrelated error rather than as the behaviour under test.

    (Multipart uploads are the documented exception — TESTING_STANDARDS.md
    calls for ``TestClient`` there.)
    """
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


# ---------------------------------------------------------------------------
# JWT forging for AuthService verification tests (#853)
# ---------------------------------------------------------------------------
#
# ``AuthService`` mints nothing. It used to carry a second, independent signing
# surface — ``generate_access_token`` / ``generate_refresh_token`` /
# ``generate_token_pair`` — that took a subject id and an ``organization_id``
# string and signed them verbatim, bypassing ``resolve_organization_claim``
# (#850). It reached no route and was removed in #853; ``IJWTTokenGenerator`` is
# the one mint path.
#
# Its verification, revocation and identity-extraction half is very much alive
# (the auth middleware, the tenant binder and the optional-auth dependency all
# call it), and testing it needs tokens whose claims the test controls —
# including claims no legitimate mint would ever emit. These helpers forge such
# tokens directly, in test code, signed with whatever key the service under test
# verifies against.
#
# They are deliberately NOT a mint: they are unreachable from production, take no
# account object, and make no claim to be safe. Nothing in ``faultmaven/`` may
# call them. A production caller that needs a token uses ``IJWTTokenGenerator``.
#
# **They MUST mirror the claim set the live generators emit.** A forger is a
# stand-in for production, and a stand-in that emits a shape production never
# emits makes every test built on it agree about a token no deployment will ever
# see. The concrete trap this rule exists for: the removed mint emitted
# ``permissions`` and `AuthenticatedUser.from_jwt_claims` reads ``permissions``,
# but both live generators emit ``scopes`` and no ``permissions`` at all — so in
# production ``AuthenticatedUser.permissions`` is always ``[]``. Forging
# ``permissions`` would keep `require_permission` (defined, exported, wired to no
# route today) green in every test and 403 on the first route that adopts it.
# `test_token_forger_shape_parity.py` pins the key sets equal; keep both in step.


#: The scopes both live generators put in every access token, verbatim
#: (`jwt_token_generator.py`, HS256 and RS256 `generate_access_token`).
LIVE_ACCESS_TOKEN_SCOPES = [
    "openid",
    "profile",
    "email",
    "cases:read",
    "cases:write",
    "knowledge:read",
]


def sign_claims_for(auth_service, claims: Dict[str, Any]) -> str:
    """Sign an arbitrary claim dict with the key ``auth_service`` verifies with.

    Mirrors the service's own key selection (RS256 private key when configured,
    otherwise the HS256 secret) so a forged token passes signature validation
    and the test can exercise what comes after it.
    """
    import jwt as _jwt

    algorithm = auth_service._algorithm
    if algorithm == "RS256" and auth_service._private_key:
        key = auth_service._private_key
    else:
        key = auth_service._settings.security.jwt_secret_key.get_secret_value()
    return _jwt.encode(claims, key, algorithm=algorithm)


def forge_access_token(
    auth_service,
    *,
    user_id: str,
    organization_id: str,
    email: str,
    roles: List[str],
    username: Optional[str] = None,
    auth_mode: Optional[str] = None,
    scopes: Optional[List[str]] = None,
    expires_in_minutes: Optional[int] = None,
) -> str:
    """Forge an access token ``auth_service.verify_token`` accepts.

    The claim set is the live generators' access payload, key for key. There is
    deliberately **no** ``permissions`` claim and deliberately **is** a ``scopes``
    one: that is what production emits, so that is what tests must see.

    ``auth_mode`` follows the same split the generators hardcode — the HS256
    generator is the local one and stamps ``"local"``, the RS256 generator is the
    OAuth one and stamps ``"oauth"`` — derived here from the algorithm the
    service verifies with. ``username`` defaults to ``user_id``; the live mint
    takes ``user.username``, so pass it explicitly when a test reads that claim.

    Unlike the live mint, the organization claim is whatever the caller passes:
    forging exists to control claims, including ones
    ``resolve_organization_claim`` would never produce.
    """
    from datetime import timedelta

    if expires_in_minutes is None:
        expires_in_minutes = auth_service._settings.auth.jwt_access_token_expire_minutes
    if auth_mode is None:
        auth_mode = "oauth" if auth_service._algorithm == "RS256" else "local"

    now = datetime.now(timezone.utc)
    return sign_claims_for(
        auth_service,
        {
            "sub": user_id,
            "username": user_id if username is None else username,
            "email": email,
            "organization_id": organization_id,
            "roles": roles,
            "scopes": (
                list(LIVE_ACCESS_TOKEN_SCOPES) if scopes is None else list(scopes)
            ),
            "exp": int((now + timedelta(minutes=expires_in_minutes)).timestamp()),
            "iat": int(now.timestamp()),
            "iss": auth_service._settings.security.jwt_issuer,
            "aud": auth_service._settings.security.jwt_audience,
            "jti": str(uuid4()),
            "type": "access",
            "auth_mode": auth_mode,
        },
    )


def forge_refresh_token(
    auth_service,
    *,
    user_id: str,
    organization_id: str,
    expires_in_days: Optional[int] = None,
) -> str:
    """Forge a refresh token ``auth_service.verify_token`` accepts."""
    from datetime import timedelta

    if expires_in_days is None:
        expires_in_days = auth_service._settings.auth.jwt_refresh_token_expire_days

    now = datetime.now(timezone.utc)
    return sign_claims_for(
        auth_service,
        {
            "sub": user_id,
            "organization_id": organization_id,
            "iss": auth_service._settings.security.jwt_issuer,
            "aud": auth_service._settings.security.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(days=expires_in_days)).timestamp()),
            "jti": str(uuid4()),
            "type": "refresh",
        },
    )


def request_with_authorization(authorization: Optional[str] = None) -> "Request":
    """A real ``Request`` carrying (or omitting) an ``Authorization`` header.

    The auth and tenant dependencies read the header off the request rather
    than declaring it as a ``Header(...)`` parameter — declaring it made
    FastAPI attach a 422 response to every operation that depends on them,
    including ones with no parameters at all (#880 residual). Tests that
    exercise those dependencies directly therefore need a request object, and
    building a genuine one keeps them honest: a stand-in with a ``.headers``
    dict would pass even if the production code started relying on something
    else the real type provides.
    """
    from starlette.requests import Request

    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": headers,
        }
    )
