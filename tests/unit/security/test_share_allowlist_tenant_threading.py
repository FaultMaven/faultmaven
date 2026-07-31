"""Every consumer of the share allowlist supplies a tenant key (#879).

``IShareRepository.list_resource_ids`` now takes a REQUIRED ``organization_id``
and fails closed without one. The repository-level predicate is pinned in
``tests/unit/infrastructure/persistence/test_share_repository.py``; this module
pins the other half — that each consumer sources a *correct* tenant key rather
than whatever happens to be in hand, and that a consumer with no tenant resolves
to the empty allowlist instead of skipping the arm's guard.

Companion rule: ``docs/architecture/security/rbac.md`` — "Tenant-Scoped
Resolution".
"""

import contextvars
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import (
    get_current_org_id,
    get_current_tenant_id,
    set_current_org_id,
    usable_tenant_id,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    resolve_shared_kb_ids,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

ORG_A = "org-alpha-11111111"
ORG_B = "org-beta-22222222"

_SINGLE = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_SINGLE,
)
_MULTI = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_MULTI,
)


def _share_repo(returns=("kb-shared",)):
    repo = MagicMock()
    repo.list_resource_ids = AsyncMock(return_value=list(returns))
    return repo


# =============================================================================
# resolve_shared_kb_ids — the KB arm's resolver
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B, STANDALONE_ORG_ID, "org-3"])
async def test_resolver_passes_the_callers_org_through_unchanged(org):
    """Sweep several tenants: whatever org comes in is the org the query carries.

    Pinned under ``single`` explicitly — the sentinel is a legitimate tenant
    there, and leaving the provider to ambient configuration would make this
    case's outcome depend on the environment rather than on the rule.
    """
    repo = _share_repo()
    with _SINGLE:
        assert await resolve_shared_kb_ids(repo, ["team-1"], org) == ["kb-shared"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=org,
    )


@pytest.mark.security
@pytest.mark.asyncio
async def test_resolver_refuses_the_sentinel_under_multi():
    """The sentinel is not a tenant under multi, so the KB team arm collapses.

    Both callers can hand this resolver a sentinel: ``MilestoneEngine`` passes
    ``case.organization_id``, which ``create_case`` stamps from the *total*
    ``get_current_org_id``, and ``KnowledgeService.search_documents`` passes the
    requester's claim. Neither may become the SQL predicate under multi.
    """
    repo = _share_repo()
    with _MULTI:
        assert await resolve_shared_kb_ids(repo, ["team-1"], STANDALONE_ORG_ID) == []
    repo.list_resource_ids.assert_not_awaited()


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B])
async def test_resolver_still_queries_for_a_real_tenant_under_multi(org):
    """The positive half: refusing the sentinel is not refusing everything."""
    repo = _share_repo()
    with _MULTI:
        assert await resolve_shared_kb_ids(repo, ["team-1"], org) == ["kb-shared"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=org,
    )


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_org", ["", None])
async def test_resolver_without_an_org_returns_empty_and_never_queries(falsy_org):
    """Fail closed: the team arm collapses, leaving personal ∪ global — both of
    which are keyed on the caller's own ids."""
    repo = _share_repo()
    assert await resolve_shared_kb_ids(repo, ["team-1"], falsy_org) == []
    repo.list_resource_ids.assert_not_awaited()


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "team_ids,org",
    [(None, ORG_A), ([], ORG_A), (["team-1"], ""), ([], "")],
)
async def test_resolver_short_circuits_on_any_missing_input(team_ids, org):
    repo = _share_repo()
    assert await resolve_shared_kb_ids(repo, team_ids, org) == []
    repo.list_resource_ids.assert_not_awaited()


# =============================================================================
# KnowledgeService.search_documents — the QA retrieval path
# =============================================================================


def _knowledge_service(share_repo, vector_store):
    tracer = MagicMock()
    tracer.trace = MagicMock(
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    )
    return KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(asanitize=AsyncMock(side_effect=lambda q: q)),
        tracer=tracer,
        vector_store=vector_store,
        share_repository=share_repo,
        # Required since #899; the allowlist paths under test never reach it.
        db_session_factory=MagicMock(),
    )


class _RecordingVectorStore:
    def __init__(self):
        self.wheres = []

    async def search(self, collection_name, query, k=5, where=None):
        self.wheres.append(where)
        return []


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B, "org-3"])
async def test_search_documents_scopes_the_share_lookup_to_the_requesting_user(org):
    """The org must come from the REQUESTER, not from the share rows themselves."""
    repo = _share_repo()
    service = _knowledge_service(repo, _RecordingVectorStore())
    user = MagicMock(user_id="u1", organization_id=org)

    await service.search_documents("crashloop", user=user, team_ids=["team-1"])

    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=org,
    )


@pytest.mark.security
@pytest.mark.asyncio
async def test_search_documents_still_scopes_by_owner_when_the_team_arm_collapses():
    """An org-less requester loses the team arm but keeps the owner/global arms,
    so the filter is narrower — never wider."""
    repo = _share_repo()
    store = _RecordingVectorStore()
    service = _knowledge_service(repo, store)
    user = MagicMock(user_id="u1", organization_id=None)

    await service.search_documents("crashloop", user=user, team_ids=["team-1"])

    repo.list_resource_ids.assert_not_awaited()
    where = store.wheres[0]
    assert {"owner_id": "u1"} in where["$or"]
    assert not any("parent_document_id" in cond for cond in where["$or"])


# =============================================================================
# CaseService — the case read allowlist
# =============================================================================


def _case_service(share_repo, teams=("team-1",)):
    team_service = AsyncMock()
    team_service.list_all_user_team_ids = AsyncMock(return_value=list(teams))
    return CaseService(
        case_repository=AsyncMock(),
        team_service=team_service,
        share_repository=share_repo,
    )


@pytest.fixture
def restore_org_context():
    previous = get_current_org_id()
    yield
    set_current_org_id(previous)


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B, STANDALONE_ORG_ID])
async def test_case_allowlist_uses_the_request_bound_tenant(org, restore_org_context):
    """The org is the request's, the same value the case write path stamps —
    swept across tenants so a hard-coded default cannot pass."""
    set_current_org_id(org)
    repo = _share_repo(["case-shared"])
    service = _case_service(repo)

    assert await service._resolve_shared_case_ids("u1") == ["case-shared"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="case",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=org,
    )


@pytest.mark.security
@pytest.mark.asyncio
async def test_case_allowlist_without_a_tenant_resolves_empty(restore_org_context):
    set_current_org_id("")
    repo = _share_repo(["case-shared"])
    service = _case_service(repo)

    assert await service._resolve_shared_case_ids("u1") == []
    repo.list_resource_ids.assert_not_awaited()


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B])
async def test_team_filter_facet_uses_the_request_bound_tenant(
    org, restore_org_context
):
    """The filter-by-team facet resolves through the same predicate — an arm
    left unscoped would reopen exactly what the allowlist closes."""
    set_current_org_id(org)
    repo = _share_repo(["case-1"])
    service = _case_service(repo, teams=("team-1",))

    assert await service._resolve_team_filter_case_ids("u1", "team-1") == ["case-1"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="case",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=org,
    )


@pytest.mark.security
@pytest.mark.asyncio
async def test_team_filter_facet_without_a_tenant_resolves_empty(restore_org_context):
    set_current_org_id("")
    repo = _share_repo(["case-1"])
    service = _case_service(repo, teams=("team-1",))

    assert await service._resolve_team_filter_case_ids("u1", "team-1") == []
    repo.list_resource_ids.assert_not_awaited()


# =============================================================================
# The sentinel is not a tenant under multi — the guard that makes the guard real
# =============================================================================
#
# ``get_current_org_id`` is TOTAL: the contextvar's default is the Standalone
# sentinel, so it never returns a falsy value and ``if not org`` behind it is
# unreachable code. The tests above set the contextvar to "" to exercise the
# guard — a state the running system cannot actually reach. These pin the state
# it CAN reach: an execution context that never bound a tenant, under
# ``TENANT_PROVIDER=multi``, where the sentinel identifies the deployment and is
# not an organization. Reading through ``get_current_tenant_id`` is what turns
# the dead guard into a live one.


@pytest.mark.security
def test_an_unbound_execution_context_reads_as_the_standalone_sentinel():
    """The premise of every test below: 'never bound' and 'bound to the sentinel'
    are the same read, because the sentinel is the contextvar's default. A fresh
    ``Context`` is a genuinely unbound one — nothing has set the var in it."""
    assert contextvars.Context().run(get_current_org_id) == STANDALONE_ORG_ID


@pytest.mark.security
@pytest.mark.parametrize("org", [ORG_A, ORG_B, "org-3"])
def test_a_real_org_is_a_usable_tenant_under_either_provider(org):
    with _SINGLE:
        assert usable_tenant_id(org) == org
    with _MULTI:
        assert usable_tenant_id(org) == org


@pytest.mark.security
@pytest.mark.parametrize("falsy_org", ["", None])
def test_no_org_is_never_a_usable_tenant(falsy_org):
    with _SINGLE:
        assert usable_tenant_id(falsy_org) is None
    with _MULTI:
        assert usable_tenant_id(falsy_org) is None


@pytest.mark.security
def test_the_sentinel_is_a_tenant_under_single_and_not_under_multi():
    """The whole rule, in one assertion pair — the same predicate the API layer's
    ``require_actor_organization`` refuses on, shared so the two cannot drift."""
    with _SINGLE:
        assert usable_tenant_id(STANDALONE_ORG_ID) == STANDALONE_ORG_ID
    with _MULTI:
        assert usable_tenant_id(STANDALONE_ORG_ID) is None


@pytest.mark.security
def test_the_context_read_applies_the_same_rule(restore_org_context):
    set_current_org_id(STANDALONE_ORG_ID)
    with _SINGLE:
        assert get_current_tenant_id() == STANDALONE_ORG_ID
    with _MULTI:
        assert get_current_tenant_id() is None


@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unbound_context_under_multi_collapses_the_case_allowlist(
    restore_org_context,
):
    """A background task that did not inherit the request context reads as the
    sentinel. Under multi that is not a tenant, so the shared-cases arm collapses
    to empty — it must NOT query with the sentinel as the org predicate, which
    would be the sentinel used as a tenant."""
    set_current_org_id(STANDALONE_ORG_ID)  # == the unbound default, pinned above
    repo = _share_repo(["case-shared"])
    service = _case_service(repo)

    with _MULTI:
        assert await service._resolve_shared_case_ids("u1") == []
    repo.list_resource_ids.assert_not_awaited()


@pytest.mark.security
@pytest.mark.asyncio
async def test_the_sentinel_is_the_tenant_for_the_case_allowlist_under_single(
    restore_org_context,
):
    """Positive half: in a Standalone deployment the sentinel IS the one tenant,
    so refusing it everywhere would break single-tenant listing outright."""
    set_current_org_id(STANDALONE_ORG_ID)
    repo = _share_repo(["case-shared"])
    service = _case_service(repo)

    with _SINGLE:
        assert await service._resolve_shared_case_ids("u1") == ["case-shared"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="case",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=STANDALONE_ORG_ID,
    )


@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unbound_context_under_multi_collapses_the_team_filter_facet(
    restore_org_context,
):
    set_current_org_id(STANDALONE_ORG_ID)
    repo = _share_repo(["case-1"])
    service = _case_service(repo, teams=("team-1",))

    with _MULTI:
        assert await service._resolve_team_filter_case_ids("u1", "team-1") == []
    repo.list_resource_ids.assert_not_awaited()


@pytest.mark.security
@pytest.mark.asyncio
async def test_the_sentinel_is_the_tenant_for_the_team_filter_facet_under_single(
    restore_org_context,
):
    set_current_org_id(STANDALONE_ORG_ID)
    repo = _share_repo(["case-1"])
    service = _case_service(repo, teams=("team-1",))

    with _SINGLE:
        assert await service._resolve_team_filter_case_ids("u1", "team-1") == ["case-1"]
    repo.list_resource_ids.assert_awaited_once_with(
        resource_type="case",
        scope_type="team",
        scope_ids=["team-1"],
        organization_id=STANDALONE_ORG_ID,
    )
