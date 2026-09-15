"""Declaring a field is not applying it — a guard over the case request surface.

This repository has shipped the same defect at least five times: a request field
or a query parameter that is DECLARED, ACCEPTED by validation, published in the
OpenAPI document, and then never reaches a query. The endpoint answers 200 with
an unfiltered list. Nothing raises, nothing logs, and the client believes it
filtered. faultmaven-dashboard#51 was the bill for one of them — a date picker
that did nothing for months and was eventually deleted as a lie.

The guard has two arms, because the two halves of the surface fail differently:

**Arm 1 — route query parameters.** ``include_archived`` is a *route* parameter,
not a model field, so a walker over ``model_fields`` never sees it. That is
exactly how it survived: it is declared on ``GET /api/v1/cases``, handed to
``CaseListFilter(...)``, and dropped there by Pydantic's default
``extra='ignore'`` because the model declares no such field. Arm 1 therefore
walks what FastAPI itself resolved as the route's query parameters
(``route.dependant.query_params`` — the artifact, not a re-reading of the
signature), calls the endpoint with a distinct value for every one of them, and
CAPTURES the filter model the route actually constructed. A parameter either
arrives on that model carrying the value that was sent, or it is classified.

**Arm 2 — request model fields.** Walking ``Model.model_fields`` and asserting
each field DISCRIMINATES: seed a repository, issue two requests differing in
that field alone, and require the answers to differ. ``total_count`` is asserted
alongside the page and is not optional — faultmaven#1409 established that a
predicate applied *after* the LIMIT disagrees with its own count, so "the page
changed" is not evidence the field reached the WHERE clause. A predicate must
move both; a pagination control must move the page and leave the total alone.

**Real repositories, never mocks.** A mocked repository has no WHERE clause, so
it cannot tell "the field reached the query" from "the field reached a function
signature" — which is the entire distinction under test. Every discriminating
pair runs against every repository that can be stood up in-process.

**Every declared thing is classified.** The registry below maps each parameter
and each field to a verdict. An unclassified one FAILS, with a message naming
the model, the thing, and what the author has to decide. That is the point: the
next person to add a field has to classify it rather than omit it, and a field
that is exempt says so out loud, with an issue link, where a reader will see it.

An EXEMPT entry asserts the defect is STILL THERE. When the underlying bug is
fixed the guard goes red and asks for the entry to be re-classified, so an
exemption cannot outlive the thing it excuses.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.routes import admin_cases
from faultmaven.infrastructure.persistence import database as database_module
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.api_models import CaseListFilter, CaseSearchRequest
from faultmaven.modules.case.api import routes as case_routes
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

pytestmark = [pytest.mark.unit, pytest.mark.api]


# ============================================================
# The seeded world these rules are written against
# ============================================================

SEED_OWNER = "owner"
SEED_ENTERPRISE = "ent_guard_0001"
#: Two teams with disjoint, non-empty share sets. The team pair is what makes
#: `team_id` provable: a filter that merely SHORT-CIRCUITS to empty (no
#: membership, no share repository) would also "discriminate" against an
#: unfiltered baseline, so the discriminating pair is two teams that both match
#: something. Two non-empty answers cannot be produced by a short circuit.
TEAM_SHARES: Mapping[str, list[str]] = {
    "team_a": ["case_000000000001"],
    "team_b": ["case_000000000002", "case_000000000003"],
}

_EPOCH = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _day(n: int) -> datetime:
    """The seeded instant ``n`` days after the corpus epoch. Always UTC-aware."""
    return _EPOCH + timedelta(days=n)


# ============================================================
# The registry
# ============================================================


class Verdict(str, Enum):
    """What a declared parameter or field is claimed to do."""

    #: Arm 1 — the route parameter arrives on the filter model it is fed into.
    REACHES = "reaches the filter model"
    #: Arm 2 — the field narrows the match set (page AND total_count move).
    NARROWS = "narrows the match set"
    #: Arm 2 — the field slices the match set (page moves, total_count does not).
    PAGES = "slices the match set"
    #: Either arm — declared, accepted, published, and applied to nothing.
    EXEMPT = "declared but unapplied"


#: An exemption names a GitHub issue, or admits in the registry itself that none
#: exists yet. There is no third option: an exemption whose provenance nobody
#: can look up is indistinguishable from an oversight, which is the failure mode
#: this whole file exists to end.
_ISSUE_REFERENCE = re.compile(r"^(#\d+|UNREPORTED: .+)$")


@dataclass(frozen=True)
class Rule:
    """One classification, with the evidence that supports it.

    ``baseline`` and ``variant`` are the discriminating pair: two requests that
    differ in exactly the classified field and in nothing else. That isolation
    is checked by :func:`test_the_registry_is_well_formed`, because a pair that
    varies two things proves nothing about either.

    ``dropped_by`` records repositories that accept the argument and ignore it,
    keyed by repository class name and valued by the note explaining the gap. It
    is an EXEMPT entry scoped to one implementation: on those repositories the
    pair is asserted to change nothing, so the gap stays pinned instead of being
    skipped into silence.
    """

    verdict: Verdict
    baseline: Mapping[str, Any] = field(default_factory=dict)
    variant: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    issue: str = ""
    dropped_by: Mapping[str, str] = field(default_factory=dict)


def reaches() -> Rule:
    """Arm 1: this query parameter must arrive on the route's filter model."""
    return Rule(verdict=Verdict.REACHES)


def narrows(
    baseline: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    dropped_by: Mapping[str, str] | None = None,
) -> Rule:
    """Arm 2: a predicate — both the page and ``total_count`` must move."""
    return Rule(
        verdict=Verdict.NARROWS,
        baseline=baseline,
        variant=variant,
        dropped_by=dropped_by or {},
    )


def pages(baseline: Mapping[str, Any], variant: Mapping[str, Any]) -> Rule:
    """Arm 2: a pagination control — the page moves, ``total_count`` must not.

    Deliberately *not* folded into :func:`narrows`. The count assertion exists
    to catch a predicate applied after the LIMIT (faultmaven#1409), and for a
    pagination control the correct answer is the opposite one: a ``limit`` that
    changed ``total_count`` would mean the count is the page length rather than
    the match count. One assertion cannot serve both, so the registry says which
    is meant.
    """
    return Rule(verdict=Verdict.PAGES, baseline=baseline, variant=variant)


def exempt(
    baseline: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    reason: str,
    issue: str,
) -> Rule:
    """Either arm: declared, accepted, and applied to nothing. Known defect."""
    return Rule(
        verdict=Verdict.EXEMPT,
        baseline=baseline,
        variant=variant,
        reason=reason,
        issue=issue,
    )


# -- Arm 1: GET /api/v1/cases -------------------------------------------------

LIST_ROUTE_RULES: Mapping[str, Rule] = {
    "state": reaches(),
    "source": reaches(),
    "team_id": reaches(),
    "created_after": reaches(),
    "created_before": reaches(),
    "limit": reaches(),
    "offset": reaches(),
    "include_empty": reaches(),
    "include_archived": exempt(
        {},
        {},
        reason=(
            "Declared on the route and passed into CaseListFilter(...), which "
            "declares no such field and sets no model_config — so Pydantic's "
            "default extra='ignore' drops it without a word, and no repository "
            "carries the predicate either. Accepted, published in "
            "docs/reference/api/openapi.json, and applied to nothing."
        ),
        issue="#1413",
    ),
}

# -- Arm 1: GET /api/v1/admin/cases -------------------------------------------

ADMIN_LIST_ROUTE_RULES: Mapping[str, Rule] = {
    "state": reaches(),
    "source": reaches(),
    "limit": reaches(),
    "offset": reaches(),
}

# -- Arm 2: CaseListFilter ----------------------------------------------------

LIST_FILTER_RULES: Mapping[str, Rule] = {
    "state": narrows({"state": CaseState.INQUIRY}, {"state": CaseState.INVESTIGATING}),
    "source": narrows(
        {"source": "copilot"},
        {"source": "slack"},
        dropped_by={
            "InMemoryCaseRepository": (
                "InMemoryCaseRepository.list accepts `source` and never reads "
                "it — the method filters on user_id, restrict_case_ids, state, "
                "include_empty and the creation-date window, and nothing else. "
                "This repository is not test-only: create_case_repository "
                "selects it whenever DATABASE_URL is unset or :memory:, so in "
                "that deployment `GET /api/v1/cases?source=slack` answers 200 "
                "with every case. UNREPORTED, found by this guard."
            )
        },
    ),
    "team_id": narrows({"team_id": "team_a"}, {"team_id": "team_b"}),
    "created_after": narrows(
        {"created_after": lambda: _day(2)}, {"created_after": lambda: _day(4)}
    ),
    "created_before": narrows(
        {"created_before": lambda: _day(3)}, {"created_before": lambda: _day(5)}
    ),
    "include_empty": narrows({"include_empty": True}, {"include_empty": False}),
    "limit": pages({"limit": 2}, {"limit": 4}),
    "offset": pages({"limit": 2, "offset": 0}, {"limit": 2, "offset": 2}),
    "user_id": exempt(
        {"user_id": SEED_OWNER},
        {"user_id": "a-stranger"},
        reason=(
            "Never read. CaseService.list_user_cases takes the principal as its "
            "own `user_id` argument and reads only state, source, team_id, "
            "limit, offset, include_empty, created_after and created_before off "
            "the filter; `grep -rn 'filters\\.user_id' faultmaven/` returns "
            "nothing. Internal only — CaseListFilter is not published in the "
            "OpenAPI document — but it is a settable field that changes no "
            "answer. UNREPORTED, found during the #1413/#1416 sweep."
        ),
        issue="UNREPORTED: needs an issue; see the PR that added this file",
    ),
    "organization_id": exempt(
        {"organization_id": "org_alpha"},
        {"organization_id": "org_beta"},
        reason=(
            "Never read; `grep -rn 'filters\\.organization_id' faultmaven/` "
            "returns nothing. Under ADR-017 the organization BILLS and is never "
            "a visibility predicate, so the field has no correct behaviour to "
            "implement — the honest fix is deletion, not a WHERE clause. "
            "UNREPORTED, found during the #1413/#1416 sweep."
        ),
        issue="UNREPORTED: needs an issue; see the PR that added this file",
    ),
}

# -- Arm 2: CaseSearchRequest -------------------------------------------------

SEARCH_REQUEST_RULES: Mapping[str, Rule] = {
    "query": narrows({"query": "widget"}, {"query": "gadget"}),
    "team_id": narrows(
        {"query": "widget", "team_id": "team_a"},
        {"query": "widget", "team_id": "team_b"},
    ),
    "limit": pages({"query": "widget", "limit": 1}, {"query": "widget", "limit": 3}),
    "state": exempt(
        {"query": "widget", "state": CaseState.INQUIRY},
        {"query": "widget", "state": CaseState.INVESTIGATING},
        reason=(
            "Declared on CaseSearchRequest and published in openapi.json, and "
            "never read. CaseService.search_cases calls repository.search("
            "query=, user_id=, limit=, shared_case_ids=, restrict_case_ids=) "
            "and looks at no other field; ICaseRepository.search declares no "
            "`state` parameter, so there is no query for it to reach."
        ),
        issue="#1416",
    ),
    "user_id": exempt(
        {"query": "widget", "user_id": SEED_OWNER},
        {"query": "widget", "user_id": "a-stranger"},
        reason=(
            "Declared, published in openapi.json, never read. The principal "
            "reaches search_cases as its own argument, taken from the "
            "authenticated user; this field is inert. (Which is the safe "
            "direction — a read field here would be a request-controlled "
            "scope — but inert and published is still a lie to the client.)"
        ),
        issue="#1416",
    ),
    "organization_id": exempt(
        {"query": "widget", "organization_id": "org_alpha"},
        {"query": "widget", "organization_id": "org_beta"},
        reason=(
            "Declared, published in openapi.json, never read. Under ADR-017 the "
            "organization bills and is never a visibility predicate."
        ),
        issue="#1416",
    ),
}


# ============================================================
# The seed corpus
# ============================================================


def _seed_case(
    index: int, *, title: str, state: CaseState, source: str, turn: int
) -> Case:
    """One seeded case. Built fresh per fixture — the in-memory repository
    stores the instance itself and applies optimistic concurrency to it, so a
    module-level corpus would carry version bumps between tests."""
    stamp = _day(index)
    extra: dict[str, Any] = {}
    if state is CaseState.INVESTIGATING:
        # INVESTIGATING is gated by Case's own validators: it needs a confirmed
        # problem statement and a commitment to investigate.
        extra["inquiry"] = InquiryData(
            proposed_problem_statement="seeded problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        )
    return Case(
        case_id=f"case_{index:012d}",
        user_id=SEED_OWNER,
        enterprise_id=SEED_ENTERPRISE,
        title=title,
        description="seeded by the declared-filters guard",
        state=state,
        source=source,
        current_turn=turn,
        created_at=stamp,
        updated_at=stamp,
        last_activity_at=stamp,
        **extra,
    )


def _corpus() -> list[Case]:
    """Five cases, shaped so every discriminating pair yields two NON-EMPTY and
    differently-sized answers.

    Both halves matter. Non-empty on both sides means a swallowed exception
    cannot masquerade as "the filter worked" — and ``CaseService.list_user_cases``
    really does swallow: its body ends in ``except Exception: return [], 0``.
    Different sizes means ``total_count`` has something to say; a 2-versus-2
    split would make the count assertion vacuous.
    """
    return [
        _seed_case(
            1, title="alpha widget", state=CaseState.INQUIRY, source="copilot", turn=3
        ),
        _seed_case(
            2, title="beta widget", state=CaseState.INQUIRY, source="copilot", turn=0
        ),
        _seed_case(
            3, title="gamma gadget", state=CaseState.INQUIRY, source="slack", turn=4
        ),
        _seed_case(
            4,
            title="delta gadget",
            state=CaseState.INVESTIGATING,
            source="api",
            turn=5,
        ),
        _seed_case(
            5,
            title="epsilon widget",
            state=CaseState.INVESTIGATING,
            source="copilot",
            turn=6,
        ),
    ]


class _StubTeamService:
    """The principal belongs to both seeded teams."""

    async def list_all_user_team_ids(self, user_id: str) -> list[str]:
        return list(TEAM_SHARES)


class _StubShareRepository:
    """The resource_shares table, reduced to the two rows this file needs.

    Stubbed rather than persisted because the share table is the auth module's,
    and what is under test here is whether the case query narrows on the ids it
    is handed — not how those ids are resolved.
    """

    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: list[str],
        enterprise_id: str,
    ) -> list[str]:
        resolved: list[str] = []
        for scope_id in scope_ids:
            resolved.extend(TEAM_SHARES.get(scope_id, []))
        return resolved

    async def list_scopes_for_resources(
        self, resource_type: str, resource_ids: list[str]
    ) -> dict[str, list[Any]]:
        return {}


def _service(repository: Any) -> CaseService:
    return CaseService(
        repository,
        team_service=_StubTeamService(),
        share_repository=_StubShareRepository(),
    )


# ============================================================
# The repositories under test
# ============================================================


async def _seed(repository: Any) -> None:
    for case in _corpus():
        await repository.save(case)


async def _install_tenancy_rows(connection: Any) -> None:
    """The enterprise and user rows ``cases`` carries foreign keys to.

    Needed wherever foreign keys are enforced — the application engine turns
    ``PRAGMA foreign_keys=ON`` on per connection. Written on both SQLite arms so
    the two are seeded identically rather than one of them depending on the
    pragma being off.
    """
    await connection.execute(
        text(
            "INSERT INTO enterprises (enterprise_id, name, slug) "
            "VALUES (:eid, 'Guard Enterprise', 'guard-enterprise')"
        ),
        {"eid": SEED_ENTERPRISE},
    )
    await connection.execute(
        text(
            "INSERT INTO users (user_id, enterprise_id, username, email, "
            "display_name) VALUES (:uid, :eid, :uid, :email, 'Seed Owner')"
        ),
        {"uid": SEED_OWNER, "eid": SEED_ENTERPRISE, "email": f"{SEED_OWNER}@test"},
    )


@asynccontextmanager
async def _in_memory_repository():
    repository = InMemoryCaseRepository()
    await _seed(repository)
    yield repository


@asynccontextmanager
async def _sqlite_repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await _install_tenancy_rows(connection)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            repository = SQLiteCaseRepository(session)
            await _seed(repository)
            yield repository
    finally:
        await engine.dispose()


@asynccontextmanager
async def _sessionless_repository(tmp_path):
    """The repository a standalone deployment actually runs.

    ``create_case_repository`` picks ``SessionlessCaseRepository`` whenever
    DATABASE_URL names a persistent database, and it resolves a session per
    call through the module-level engine — so exercising it means standing that
    engine up. It holds no predicate logic of its own; it forwards. Forwarding
    is precisely where this defect class lives, which is why it is worth a real
    round trip rather than a signature check.
    """
    previous_engine = database_module._engine
    previous_factory = database_module._session_factory
    database_module.reset_engine()
    url = f"sqlite+aiosqlite:///{tmp_path}/declared-filters-guard.db"
    try:
        engine = database_module.get_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await _install_tenancy_rows(connection)
        database_module.get_session_factory(url)
        repository = SessionlessCaseRepository()
        await _seed(repository)
        yield repository
    finally:
        await database_module.close_database()
        database_module._engine = previous_engine
        database_module._session_factory = previous_factory


REPOSITORY_ARMS = (
    "InMemoryCaseRepository",
    "SQLiteCaseRepository",
    "SessionlessCaseRepository",
)


@pytest.fixture(params=REPOSITORY_ARMS)
async def seeded_repository(request, tmp_path):
    """Every repository that can be stood up in-process, already seeded.

    ``PostgreSQLHybridCaseRepository`` is the fourth implementation and is NOT
    here: its SQL is PostgreSQL-only (``to_tsvector``/``ts_rank``, ``::jsonb``,
    ``FILTER (WHERE ...)``), so it needs a live server and belongs to the
    postgres-marked suite rather than to a unit test.
    """
    if request.param == "InMemoryCaseRepository":
        async with _in_memory_repository() as repository:
            yield request.param, repository
    elif request.param == "SQLiteCaseRepository":
        async with _sqlite_repository() as repository:
            yield request.param, repository
    else:
        async with _sessionless_repository(tmp_path) as repository:
            yield request.param, repository


# ============================================================
# Driving the two request surfaces
# ============================================================


@dataclass(frozen=True)
class Outcome:
    """What one request answered: the ids on the page, and the published total."""

    ids: frozenset[str]
    total: int | None


@dataclass(frozen=True)
class ModelSurface:
    """One request model, and the service call that carries it to a query.

    Driven at the SERVICE, not at the repository. That is the whole point:
    CaseSearchRequest.state reaches a repository that has a ``state`` column and
    is still never applied, because the service never reads it. A repository-level
    probe would report it green.
    """

    name: str
    model: type[BaseModel]
    rules: Mapping[str, Rule]
    run: Callable[[CaseService, BaseModel], Awaitable[Outcome]]
    publishes_total: bool
    no_total_because: str = ""


async def _run_list(service: CaseService, request: BaseModel) -> Outcome:
    summaries, total = await service.list_user_cases(SEED_OWNER, request)
    return Outcome(frozenset(s.case_id for s in summaries), total)


async def _run_search(service: CaseService, request: BaseModel) -> Outcome:
    summaries = await service.search_cases(request, SEED_OWNER)
    return Outcome(frozenset(s.case_id for s in summaries), None)


#: Guarding another request model is ONE entry here plus its rule table: name
#: the model, hand over the service call that carries it to a query, and say
#: whether that call publishes a total. Everything else — walking the fields,
#: failing on an unclassified one, running the pair against every repository —
#: is already generic. The rule table is the part that cannot be generated,
#: because deciding what each field is *supposed* to do is the work.
MODEL_SURFACES = (
    ModelSurface(
        name="GET /api/v1/cases → CaseListFilter",
        model=CaseListFilter,
        rules=LIST_FILTER_RULES,
        run=_run_list,
        publishes_total=True,
    ),
    ModelSurface(
        name="POST /api/v1/cases/search → CaseSearchRequest",
        model=CaseSearchRequest,
        rules=SEARCH_REQUEST_RULES,
        run=_run_search,
        publishes_total=False,
        no_total_because=(
            "This surface publishes no total. CaseService.search_cases returns "
            "List[CaseSummary] and discards the count the repository hands back, "
            "and POST /cases/search is declared response_model=List[CaseSummary] "
            "— CaseSearchResponse, which carries total_count, is unreferenced by "
            "the route. So the faultmaven#1409 evidence (a predicate applied "
            "after the LIMIT disagrees with its own count) cannot be gathered "
            "here at all: there is no count to disagree. Recorded rather than "
            "quietly skipped."
        ),
    ),
)


def _resolve(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve any callable in a rule payload.

    The creation-date bounds are computed from the corpus epoch, so they are
    stored as zero-argument callables rather than as values frozen at import.
    """
    return {k: (v() if callable(v) else v) for k, v in payload.items()}


# ============================================================
# Arm 1 — route query parameters
# ============================================================


def _route(router: Any, name: str) -> APIRoute:
    return next(r for r in router.routes if isinstance(r, APIRoute) and r.name == name)


class _CapturingListService:
    """Stands in for the case service to capture the filter the route built."""

    def __init__(self) -> None:
        self.filters: Any = None

    async def list_user_cases(self, user_id: str, filters: Any):
        self.filters = filters
        return [], 0

    async def list_all_cases(self, filters: Any):
        self.filters = filters
        return [], 0


class _StubPrincipal:
    user_id = SEED_OWNER
    username = SEED_OWNER
    email = f"{SEED_OWNER}@test"
    roles = ["platform_admin"]
    enterprise_id = SEED_ENTERPRISE
    organization_id = None


class _StubAuditRepository:
    """``record_operator_access`` turns any failure into a 503, so the operator
    route needs an audit sink that succeeds before it will reach its filter."""

    async def record_access(self, **kwargs: Any) -> None:
        return None


async def _capture_list_filter(probe: Mapping[str, Any]) -> BaseModel:
    service = _CapturingListService()
    await case_routes.list_cases(
        response=Response(),
        case_service=service,
        current_user=_StubPrincipal(),
        **_resolve(probe),
    )
    return service.filters


async def _capture_admin_list_filter(probe: Mapping[str, Any]) -> BaseModel:
    service = _CapturingListService()
    await admin_cases.list_all_cases(
        current_user=_StubPrincipal(),
        case_service=service,
        audit_repo=_StubAuditRepository(),
        **_resolve(probe),
    )
    return service.filters


@dataclass(frozen=True)
class RouteSurface:
    """One route, the filter model it feeds, and a probe value per parameter."""

    name: str
    route: APIRoute
    target_model: type[BaseModel]
    rules: Mapping[str, Rule]
    probe: Mapping[str, Any]
    capture: Callable[[Mapping[str, Any]], Awaitable[BaseModel]]


#: Same shape for a route: one entry naming the route, the filter model it
#: feeds, a probe value per declared parameter, and the call that captures what
#: the endpoint built.
ROUTE_SURFACES = (
    RouteSurface(
        name="GET /api/v1/cases",
        route=_route(case_routes.router, "list_cases"),
        target_model=CaseListFilter,
        rules=LIST_ROUTE_RULES,
        probe={
            # Every value is distinct from the parameter's own default, so
            # "arrived" cannot be confused with "defaulted to the same thing".
            "state": CaseState.INVESTIGATING,
            "source": "slack",
            "team_id": "team_a",
            "created_after": lambda: _day(1),
            "created_before": lambda: _day(9),
            "limit": 7,
            "offset": 3,
            "include_empty": False,
            "include_archived": True,
        },
        capture=_capture_list_filter,
    ),
    RouteSurface(
        name="GET /api/v1/admin/cases",
        route=_route(admin_cases.router, "list_all_cases"),
        target_model=CaseListFilter,
        rules=ADMIN_LIST_ROUTE_RULES,
        probe={
            "state": CaseState.INVESTIGATING,
            "source": "slack",
            "limit": 7,
            "offset": 3,
        },
        capture=_capture_admin_list_filter,
    ),
)


def _route_ids(surface: RouteSurface) -> list[str]:
    return [p.name for p in surface.route.dependant.query_params]


@pytest.mark.parametrize("surface", ROUTE_SURFACES, ids=lambda s: s.name)
def test_every_declared_query_parameter_is_classified(surface: RouteSurface) -> None:
    """A query parameter nobody classified is the defect, not a gap in the test.

    The names come from ``route.dependant.query_params`` — FastAPI's own
    resolution of the endpoint signature, which is the same thing that decides
    what appears in the OpenAPI document. Reading the signature ourselves would
    be reconstructing the artifact instead of capturing it.
    """
    declared = set(_route_ids(surface))
    unclassified = sorted(declared - set(surface.rules))
    assert not unclassified, (
        f"{surface.name} declares query parameter(s) {unclassified} that this "
        f"guard has no verdict for.\n"
        f"Decide, in tests/unit/modules/case/"
        f"test_declared_filters_reach_the_query.py:\n"
        f"  • it reaches {surface.target_model.__name__} and a query → add "
        f'`"<name>": reaches()` to this route\'s rule table AND a discriminating '
        f"pair to that model's rule table;\n"
        f"  • it is declared and applied to nothing → add "
        f'`"<name>": exempt(..., reason=..., issue="#NNNN")`.\n'
        f"Omitting it is how include_archived (#1413) survived: accepted, "
        f"published, and filtering nothing."
    )

    stale = sorted(set(surface.rules) - declared)
    assert not stale, (
        f"{surface.name} no longer declares {stale}, but this guard still "
        f"classifies them. Delete the stale rule(s)."
    )


@pytest.mark.parametrize("surface", ROUTE_SURFACES, ids=lambda s: s.name)
async def test_declared_query_parameters_reach_the_filter_model(
    surface: RouteSurface,
) -> None:
    """Call the route; CAPTURE the filter it built; check each parameter arrived.

    Captured, never reconstructed. The assertion is made against the very
    ``CaseListFilter`` instance the endpoint handed to the service, so it cannot
    be satisfied by a model this test built from its own assumptions.
    """
    captured = await surface.capture(surface.probe)
    assert isinstance(captured, surface.target_model), (
        f"{surface.name} did not construct a {surface.target_model.__name__}; "
        f"got {type(captured).__name__}. The rule tables describe a model this "
        f"route no longer feeds."
    )
    sent = _resolve(surface.probe)

    for name in _route_ids(surface):
        rule = surface.rules[name]
        if rule.verdict is Verdict.REACHES:
            assert hasattr(captured, name), (
                f"{surface.name}?{name}= is declared, accepted and published, "
                f"and does not exist on {surface.target_model.__name__} — so "
                f"Pydantic's default extra='ignore' discarded it silently. "
                f"Either declare the field on the model and apply it, or "
                f"classify the parameter exempt with an issue."
            )
            assert getattr(captured, name) == sent[name], (
                f"{surface.name}?{name}= reached "
                f"{surface.target_model.__name__} carrying "
                f"{getattr(captured, name)!r}, but {sent[name]!r} was sent. The "
                f"route is rewriting the value on its way to the filter."
            )
        else:
            # An EXEMPT parameter asserts the defect is STILL THERE, so that
            # fixing it turns this red and forces the exemption to be retired
            # rather than left standing over a bug that no longer exists.
            assert not hasattr(captured, name), (
                f"{surface.name}?{name}= now reaches "
                f"{surface.target_model.__name__}, but this guard still "
                f"classifies it EXEMPT ({rule.issue}). If {rule.issue} is "
                f'fixed, re-classify it: `"{name}": reaches()` here, plus a '
                f"discriminating pair in the {surface.target_model.__name__} "
                f"rule table so the field is proved to reach a query and not "
                f"merely a constructor."
            )


async def test_the_filter_model_silently_swallows_an_undeclared_parameter() -> None:
    """The mechanism itself, captured once.

    ``include_archived`` does not die at the route, in validation, or at the
    repository — it dies HERE, in a constructor that accepts anything and keeps
    only what it declared. Pinning the mechanism separately from the instance
    means the next field to hit it is recognised rather than rediscovered.
    """
    assert "include_archived" not in CaseListFilter.model_fields
    assert CaseListFilter.model_config == {}, (
        "CaseListFilter now sets model_config; if that config is "
        "extra='forbid', the silent-drop mechanism is gone and #1413 has a "
        "louder failure mode than this guard assumes."
    )

    # No exception. That is the defect: an unknown keyword is not an error.
    built = CaseListFilter(include_archived=True)

    assert not hasattr(built, "include_archived")
    assert "include_archived" not in built.model_dump()
    assert built.model_extra in (None, {})


# ============================================================
# Arm 2 — request model fields
# ============================================================


def _arm2_cases() -> list[tuple[ModelSurface, str]]:
    return [(surface, name) for surface in MODEL_SURFACES for name in surface.rules]


@pytest.mark.parametrize("surface", MODEL_SURFACES, ids=lambda s: s.model.__name__)
def test_every_request_model_field_is_classified(surface: ModelSurface) -> None:
    """A field nobody classified fails. That is the entire point of the file."""
    declared = set(surface.model.model_fields)
    unclassified = sorted(declared - set(surface.rules))
    assert not unclassified, (
        f"{surface.model.__name__} declares field(s) {unclassified} that this "
        f"guard has no verdict for.\n"
        f"Decide, in tests/unit/modules/case/"
        f"test_declared_filters_reach_the_query.py:\n"
        f"  • it reaches a query → `narrows(baseline, variant)` (page AND "
        f"total_count move) or `pages(baseline, variant)` (page moves, "
        f"total_count does not);\n"
        f"  • it is declared and applied to nothing → `exempt(baseline, "
        f'variant, reason=..., issue="#NNNN")`.\n'
        f"A field with no verdict is exactly CaseSearchRequest.state (#1416): "
        f"declared, validated, published, read by nobody."
    )

    stale = sorted(set(surface.rules) - declared)
    assert not stale, (
        f"{surface.model.__name__} no longer declares {stale}, but this guard "
        f"still classifies them. Delete the stale rule(s)."
    )


@pytest.mark.parametrize(
    "surface,field_name",
    _arm2_cases(),
    ids=lambda v: v if isinstance(v, str) else v.model.__name__,
)
def test_the_registry_is_well_formed(surface: ModelSurface, field_name: str) -> None:
    """A discriminating pair must isolate ONE field.

    A pair that varies two things proves nothing about either — it is the same
    error as the defect under test, one level up.
    """
    rule = surface.rules[field_name]

    baseline = _resolve(rule.baseline)
    variant = _resolve(rule.variant)
    assert set(baseline) == set(variant), (
        f"{surface.model.__name__}.{field_name}: the discriminating pair sets "
        f"different keys on each side ({sorted(baseline)} vs "
        f"{sorted(variant)}), so what changed is not isolated."
    )
    differing = {k for k in baseline if baseline[k] != variant[k]}
    assert differing == {field_name}, (
        f"{surface.model.__name__}.{field_name}: the discriminating pair "
        f"differs in {sorted(differing)}. It must differ in {field_name!r} and "
        f"nothing else, or the answer it produces is not evidence about "
        f"{field_name!r}."
    )

    for repository_name, note in rule.dropped_by.items():
        assert repository_name in REPOSITORY_ARMS, (
            f"{surface.model.__name__}.{field_name} records a gap on "
            f"{repository_name!r}, which is not a repository this guard runs."
        )
        assert note, f"{repository_name} gap recorded with no explanation."


def _every_rule() -> list[tuple[str, str, Rule]]:
    """Every classification in the file — both arms — as (where, what, rule)."""
    rows: list[tuple[str, str, Rule]] = []
    for route_surface in ROUTE_SURFACES:
        rows += [
            (route_surface.name, name, rule)
            for name, rule in route_surface.rules.items()
        ]
    for model_surface in MODEL_SURFACES:
        rows += [
            (model_surface.model.__name__, name, rule)
            for name, rule in model_surface.rules.items()
        ]
    return rows


@pytest.mark.parametrize(
    "where,what,rule", _every_rule(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_exemption_cites_something_lookupable(
    where: str, what: str, rule: Rule
) -> None:
    """An exemption without provenance is indistinguishable from an oversight.

    Covers BOTH arms, because a route parameter can be exempted just as a model
    field can — ``include_archived`` is one — and an unciteable exemption is the
    same failure wherever it is written.
    """
    if rule.verdict is not Verdict.EXEMPT:
        return

    assert rule.reason, f"{where}.{what} is exempt with no reason."
    assert _ISSUE_REFERENCE.match(rule.issue), (
        f"{where}.{what} is exempt citing {rule.issue!r}. Cite a '#NNNN' issue, "
        f"or admit 'UNREPORTED: ...' in the registry itself — an exemption "
        f"nobody can look up is indistinguishable from an oversight."
    )


async def _verdict_failures(
    service: CaseService,
    repository_name: str,
    surface: ModelSurface,
    field_name: str,
) -> list[str]:
    """Run one field's discriminating pair and report what it contradicts."""
    rule = surface.rules[field_name]
    baseline = await surface.run(service, surface.model(**_resolve(rule.baseline)))
    variant = await surface.run(service, surface.model(**_resolve(rule.variant)))

    where = f"{surface.name} / {field_name} on {repository_name}"
    gap_note = rule.dropped_by.get(repository_name)
    failures: list[str] = []

    page_moved = baseline.ids != variant.ids
    claims_a_difference = (
        rule.verdict in (Verdict.NARROWS, Verdict.PAGES) and not gap_note
    )

    if claims_a_difference:
        # Both sides non-empty. `list_user_cases` ends in
        # `except Exception: return [], 0`, so an empty answer means "no rows"
        # and "this blew up" at once — and an empty-versus-something pair would
        # satisfy the difference check on a crash.
        if not baseline.ids or not variant.ids:
            failures.append(
                f"{where}: the discriminating pair answered empty on one side "
                f"(baseline={sorted(baseline.ids)}, "
                f"variant={sorted(variant.ids)}). The seed corpus must make "
                f"both sides non-empty, or a swallowed exception reads as a "
                f"working filter."
            )
        elif not page_moved:
            failures.append(
                f"{where} is declared, accepted and applied to NOTHING: two "
                f"requests differing only in {field_name!r} returned the same "
                f"{len(baseline.ids)} case(s).\n"
                f"    baseline: {dict(rule.baseline)}\n"
                f"    variant:  {dict(rule.variant)}\n"
                f"  Either push the field into the repository query, or "
                f"classify it exempt with an issue link so it is a known lie "
                f"rather than a silent one."
            )

    # The count is only interesting once the page HAS moved: where it did not,
    # "applied to nothing" is already reported above and a second line about
    # total_count is noise about the same fact.
    if rule.verdict is Verdict.NARROWS and not gap_note and page_moved:
        if surface.publishes_total:
            if baseline.total == variant.total:
                failures.append(
                    f"{where} changed the page but NOT total_count "
                    f"({baseline.total} both times). A predicate applied after "
                    f"the LIMIT moves the page and leaves the count describing "
                    f"a different set — faultmaven#1409. The predicate belongs "
                    f"in the same WHERE clause as the COUNT."
                )
        elif baseline.total is not None or variant.total is not None:
            # Declared, not quietly skipped: this surface has no count to
            # disagree with, and that is itself recorded in the registry.
            failures.append(
                f"{where}: this surface is registered as publishing no total, "
                f"but one arrived. Set publishes_total=True and assert on it — "
                f"the count is the only evidence that separates a WHERE clause "
                f"from a post-LIMIT filter. ({surface.no_total_because})"
            )

    if (
        rule.verdict is Verdict.PAGES
        and surface.publishes_total
        and baseline.total != variant.total
    ):
        failures.append(
            f"{where} is a pagination control and moved total_count "
            f"({baseline.total} → {variant.total}). The total is the match "
            f"count, not the page length; a total that follows the window "
            f"cannot drive has_more."
        )

    changed = page_moved or baseline.total != variant.total

    if rule.verdict is Verdict.EXEMPT and changed:
        failures.append(
            f"{where} now changes the answer, but this guard still classifies "
            f"it EXEMPT ({rule.issue}: {rule.reason})\n"
            f"  If that issue is fixed, re-classify the field — `narrows(...)` "
            f"or `pages(...)` — and delete the exemption. An exemption must not "
            f"outlive the defect it excuses."
        )

    if gap_note and changed:
        failures.append(
            f"{where} now applies the field, but this guard still records a gap "
            f"on {repository_name}:\n    {gap_note}\n"
            f"  If it has been implemented, delete the dropped_by entry."
        )

    return failures


@pytest.mark.parametrize("surface", MODEL_SURFACES, ids=lambda s: s.model.__name__)
async def test_every_declared_field_reaches_a_query(
    seeded_repository, surface: ModelSurface
) -> None:
    """Two requests differing in one field must answer differently — or be exempt.

    Run against a REAL repository. A mock has no WHERE clause, so it cannot
    distinguish a field that reached the query from one that reached a function
    signature, and that distinction is the only thing this test is about.

    Every field of the model is walked in ONE test rather than parametrised per
    field, for two reasons. Standing a repository up is the expensive part and
    is incidental to the question — paying it once per model instead of once per
    field is most of this file's runtime. And a break here is usually a break in
    several fields at once (a repository that stopped forwarding, a service that
    stopped reading the filter), so reporting every one of them together is what
    an author actually needs to see.
    """
    repository_name, repository = seeded_repository
    service = _service(repository)

    failures: list[str] = []
    for field_name in surface.rules:
        failures.extend(
            await _verdict_failures(service, repository_name, surface, field_name)
        )

    assert not failures, (
        f"{len(failures)} declared field(s) contradict their classification:\n\n"
        + "\n\n".join(f"• {f}" for f in failures)
    )


# ============================================================
# The creation-date window, under a process clock that is not UTC
# ============================================================

TIMEZONES = ("UTC", "Pacific/Auckland", "America/Los_Angeles", "Asia/Kolkata")


@pytest.fixture
def process_timezone(request, monkeypatch):
    """Move the PROCESS clock, not just the test's arithmetic.

    A test written in UTC passes in CI and ships wrong behaviour to everyone
    else. The bounds are instants, so the property below must hold under any
    local clock — which is a claim about the code, and only provable by moving
    the clock the code runs under.
    """
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
@pytest.mark.parametrize("process_timezone", TIMEZONES, indirect=True)
async def test_creation_bounds_are_instants_under_any_process_timezone(
    seeded_repository, process_timezone
) -> None:
    """Assert the PROPERTY, not a verdict frozen in one timezone.

    The window is half-open, ``[created_after, created_before)``, over instants.
    Expected sets are derived from the seeded instants by that rule at assertion
    time, so the test cannot pass by agreeing with an assumption it also made.

    The second half is the other face of the same property: two spellings of one
    instant are one bound. On SQLite ``cases.created_at`` is adapter-rendered
    TEXT compared lexicographically, so an un-normalised offset suffix returns
    different rows for the same moment — and the route's own documentation
    invites the offset spelling, which is what makes this a property rather than
    a curiosity.
    """
    repository_name, repository = seeded_repository
    service = _service(repository)
    corpus = _corpus()

    for offset in range(0, 7):
        bound = _day(offset)

        expected_after = {c.case_id for c in corpus if c.created_at >= bound}
        seen = await _run_list(service, CaseListFilter(created_after=bound))
        assert seen.ids == frozenset(expected_after), (
            f"created_after={bound.isoformat()} under TZ={process_timezone} on "
            f"{repository_name}: expected {sorted(expected_after)}, got "
            f"{sorted(seen.ids)}. The lower bound is INCLUSIVE and is an "
            f"instant — the process clock must not enter into it."
        )
        assert seen.total == len(expected_after)

        expected_before = {c.case_id for c in corpus if c.created_at < bound}
        seen_before = await _run_list(service, CaseListFilter(created_before=bound))
        assert seen_before.ids == frozenset(expected_before), (
            f"created_before={bound.isoformat()} under TZ={process_timezone} on "
            f"{repository_name}: expected {sorted(expected_before)}, got "
            f"{sorted(seen_before.ids)}. The upper bound is EXCLUSIVE."
        )
        assert seen_before.total == len(expected_before)

        # Asia/Kolkata is a half-hour offset and Pacific/Auckland is the far
        # side of the date line: between them they break any bound that is
        # secretly a date, and any comparison that is secretly lexicographic.
        for zone in ("Asia/Kolkata", "Pacific/Auckland"):
            as_local = bound.astimezone(ZoneInfo(zone))
            assert as_local == bound  # the same instant, spelled differently
            local_answer = await _run_list(
                service, CaseListFilter(created_after=as_local)
            )
            assert local_answer == seen, (
                f"{repository_name} under TZ={process_timezone}: "
                f"created_after={bound.isoformat()} and the same instant "
                f"written {as_local.isoformat()} returned different rows "
                f"({sorted(seen.ids)} vs {sorted(local_answer.ids)})."
            )
