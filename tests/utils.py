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
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        KnowledgeItem,
        KnowledgeItemType,
    )

# Default enterprise UUID — mirrors SingleTenantProvider.DEFAULT_ENTERPRISE_ID
# and migration 006's seeded row. Tests that build orgs/users without a
# specific enterprise context anchor to this row.
DEFAULT_TEST_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


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


def install_org_autoseed(sync_session) -> None:
    """Auto-create OrganizationModel rows for any new tenanted ORM object.

    Phase 9 added FK on tenanted tables. This hook seeds the parent org row
    inside the same flush so FK constraints succeed without churning each test
    to call seed_organizations() manually. Uses Core INSERT (not session.add)
    so the rows materialize in the current transaction before child INSERTs;
    objects added via session.add() inside before_flush defer to the next
    flush, which is too late to satisfy the FK.

    NOTE: This hook only fires for ORM-mediated writes (via session flush).
    Tests that exercise raw-SQL repositories like ``SQLiteCaseRepository``
    bypass the ORM flush path; those tests should call
    ``seed_organizations(session, [...])`` explicitly before the first save,
    or wrap the case repository with the ``seed_orgs_for_repo`` helper.

    Call once per AsyncSession with `session.sync_session` as the argument.
    """
    from sqlalchemy import event, insert, select

    from faultmaven.infrastructure.persistence.models import (
        EnterpriseModel,
        OrganizationModel,
    )

    @event.listens_for(sync_session, "before_flush")
    def _seed_referenced_orgs(session, flush_context, instances):
        org_ids: set[str] = set()
        for obj in session.new:
            if isinstance(obj, OrganizationModel):
                continue
            oid = getattr(obj, "organization_id", None)
            if oid:
                org_ids.add(oid)
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
            # Ensure the default enterprise exists so the orgs.enterprise_id
            # FK target is satisfied.
            default_ent = session.get(EnterpriseModel, DEFAULT_TEST_ENTERPRISE_ID)
            if default_ent is None:
                session.execute(
                    insert(EnterpriseModel),
                    [
                        {
                            "enterprise_id": DEFAULT_TEST_ENTERPRISE_ID,
                            "name": "Default Test Enterprise",
                            "slug": "default-test",
                        }
                    ],
                )
            session.execute(
                insert(OrganizationModel),
                [
                    {
                        "organization_id": oid,
                        "enterprise_id": DEFAULT_TEST_ENTERPRISE_ID,
                        "name": f"Test Org {oid}",
                        "slug": oid,
                    }
                    for oid in missing
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
        organization_id=organization_id or generate_org_id(),
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
        self.user_watermarks: dict[str, tuple[int, int]] = {}

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        self.revoked[jti] = ttl

    async def is_revoked(self, jti: str) -> bool:
        return jti in self.revoked

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: int, ttl: int
    ) -> None:
        self.user_watermarks[user_id] = (revoked_at, ttl)

    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        entry = self.user_watermarks.get(user_id)
        return entry is not None and issued_at <= entry[0]

    async def cleanup_expired(self) -> int:
        return 0
