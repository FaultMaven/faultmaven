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
walks ``get_flat_params(route.dependant)`` — the call FastAPI's own OpenAPI
generator makes, so it is exactly the published parameter list rather than a
re-reading of the signature — calls the endpoint with a distinct value for every
one of them, and CAPTURES the filter model the route actually constructed. A
parameter either arrives on that model carrying the value that was sent, or it
is classified.

**Arm 2 — request model fields.** Walking ``Model.model_fields`` and asserting
each field DISCRIMINATES: seed a repository, issue two requests differing in
that field alone, and require the answers to differ. ``total_count`` is asserted
alongside the page and is not optional — faultmaven#1409 established that a
predicate applied *after* the LIMIT disagrees with its own count, so "the page
changed" is not evidence the field reached the WHERE clause. A predicate must
move both; a pagination control must move the page and leave the total alone.

**The seam between the arms is asserted, not assumed.** Arm 1 proves a parameter
reaches a CONSTRUCTOR; arm 2 proves a field reaches a QUERY — but only through
the one service method it drives. Believing those two compose requires the route
and the arm-2 surface to be talking about the same reader, and for
``GET /api/v1/admin/cases`` they were not: its reader is ``list_all_cases``,
a different method from the ``list_user_cases`` arm 2 drove, and it could have
stopped passing ``state`` and ``source`` to the repository with every test here
still green. So each route names its ``service_method``, each model surface names
the one it drives, and a test requires every ``reaches()`` parameter to be proved
by a surface over its own route's reader.

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
exemption cannot outlive the thing it excuses. The same holds for a
per-repository gap, and both must cite an issue.

**A process-zone matrix is decoration unless a NAIVE bound goes through it.**
Both normalisers read an aware value with ``astimezone(timezone.utc)``, which
consults no clock — so every aware bound in this file answers the same under
every ``TZ``, and the matrix around them proved nothing. Measured: breaking
``to_utc`` into the process-local reading left all of it green. What reads the
system clock is ``astimezone`` on a NAIVE value, so the promise the route
actually publishes — "a value without one is read as UTC" — is the one that can
be tested, and testing it is what makes the zones live. The test also asserts
its own discriminating power, so the matrix cannot quietly go inert again.

**An empty answer proves nothing, whatever the verdict.**
``CaseService.list_user_cases`` ends in ``except Exception: return [], 0``, so
``([], 0)`` means "no rows" and "this blew up" at once. That makes an empty pair
useless as evidence of a DIFFERENCE — and equally useless as evidence of the
SAMENESS an exemption claims, which is the more dangerous direction: two
identical empty answers confirm an exemption perfectly while measuring nothing.
Every verdict therefore requires both sides to match something first, and the
timezone bounds are all interior to the seeded corpus for the same reason.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import Response
from fastapi.dependencies.utils import get_flat_params
from fastapi.params import ParamTypes
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine, text
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


#: Bounds strictly INSIDE the seeded range (the corpus occupies days 1-5), so
#: both halves of the half-open window match something. A bound at or outside
#: an end makes one expectation empty, and ``frozenset()`` / ``0`` is also what
#: ``CaseService.list_user_cases``' blanket ``except Exception: return [], 0``
#: answers for a repository that raised — so the assertion would be satisfied by
#: a crash. The timezone test re-checks this against the corpus rather than
#: trusting the constant to have been updated alongside it.
_INTERIOR_BOUNDS = (2, 3, 4, 5)


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
class Gap:
    """One repository that accepts an argument and ignores it.

    An EXEMPT entry scoped to a single implementation. On the repositories it
    names the discriminating pair is asserted to change NOTHING, so the gap
    stays pinned — visible in the registry and red the day it is implemented —
    instead of being skipped into silence. It cites an issue for the same reason
    a whole-field exemption does.
    """

    reason: str
    issue: str


@dataclass(frozen=True)
class Rule:
    """One classification, with the evidence that supports it.

    ``baseline`` and ``variant`` are the discriminating pair: two requests that
    differ in exactly the classified field and in nothing else. That isolation
    is checked by :func:`test_the_registry_is_well_formed`, because a pair that
    varies two things proves nothing about either.

    ``dropped_by`` maps a repository class name to the :class:`Gap` describing
    why that one implementation drops the argument.
    """

    verdict: Verdict
    baseline: Mapping[str, Any] = field(default_factory=dict)
    variant: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    issue: str = ""
    dropped_by: Mapping[str, Gap] = field(default_factory=dict)


# -- Arm 1 constructors: a route parameter is EITHER carried or dropped -------
#
# The two arms have separate constructors on purpose. They share the ``Rule``
# type but not their vocabulary, and a route table has no discriminating pair to
# give — a parameter either lands on the filter model or it does not. When one
# ``exempt()`` served both, the route entries had to invent an empty ``({}, {})``
# pair to satisfy a positional signature, and nothing stopped an arm-2
# constructor from being pasted into a route table, where the arm-1 ``else``
# branch would read it as an exemption and assert the exact inverse of its
# intent. Separate constructors make that a collection-time error instead.


def reaches() -> Rule:
    """Arm 1: this query parameter must arrive on the route's filter model."""
    return Rule(verdict=Verdict.REACHES)


def route_exempt(*, reason: str, issue: str) -> Rule:
    """Arm 1: declared on the route, accepted, published — and dropped."""
    return Rule(verdict=Verdict.EXEMPT, reason=reason, issue=issue)


#: Which constructors a route table may use. Enforced, because the two tables
#: sit next to each other and a copy-paste between them is silent otherwise.
ROUTE_VERDICTS = (Verdict.REACHES, Verdict.EXEMPT)
FIELD_VERDICTS = (Verdict.NARROWS, Verdict.PAGES, Verdict.EXEMPT)


# -- Arm 2 constructors: a model field must DISCRIMINATE ----------------------


def narrows(
    baseline: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    dropped_by: Mapping[str, Gap] | None = None,
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


def field_exempt(
    baseline: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    reason: str,
    issue: str,
) -> Rule:
    """Arm 2: declared, accepted, and applied to nothing. Known defect.

    Still carries a pair, because the exemption is *asserted*: the two requests
    must answer identically. An exemption is a claim about behaviour, and a
    claim needs a measurement like any other.
    """
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
    "include_archived": route_exempt(
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

#: Named, because BOTH ``CaseListFilter`` surfaces hit it: ``list_user_cases``
#: and ``list_all_cases`` reach the same ``repository.list`` underneath, so the
#: gap belongs to the repository rather than to either route.
_IN_MEMORY_DROPS_SOURCE = Gap(
    reason=(
        "InMemoryCaseRepository.list accepts `source` and never reads it — the "
        "method filters on user_id, restrict_case_ids, state, include_empty and "
        "the creation-date window, and nothing else. This repository is not "
        "test-only: create_case_repository selects it whenever DATABASE_URL is "
        "unset or :memory:, so in that deployment `GET /api/v1/cases?"
        "source=slack` answers 200 with every case."
    ),
    issue="#1424",
)

LIST_FILTER_RULES: Mapping[str, Rule] = {
    "state": narrows({"state": CaseState.INQUIRY}, {"state": CaseState.INVESTIGATING}),
    "source": narrows(
        {"source": "copilot"},
        {"source": "slack"},
        dropped_by={"InMemoryCaseRepository": _IN_MEMORY_DROPS_SOURCE},
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
    "user_id": field_exempt(
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
    "organization_id": field_exempt(
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

# -- Arm 2: CaseListFilter, as GET /api/v1/admin/cases drives it --------------
#
# A SECOND surface over the same model, because the admin route's reader is a
# DIFFERENT method: ``CaseService.list_all_cases``, not ``list_user_cases``.
# Arm 1 proves the route's parameters land on a ``CaseListFilter``; only a
# surface driven by the method that route actually calls can prove they reach a
# query. Without this, ``list_all_cases`` could stop passing ``state`` and
# ``source`` to the repository and every test in this file would stay green —
# the exact defect the file exists to catch, on a route it names.
#
# ``covers`` is narrow because the admin route DECLARES only these four
# parameters. The rest of ``CaseListFilter`` is unreachable on this path — not
# accepted-and-dropped — so there is nothing published for it to lie about, and
# those fields are guarded on the ``list_user_cases`` surface where they ARE
# reachable.

ADMIN_LIST_FILTER_RULES: Mapping[str, Rule] = {
    "state": narrows({"state": CaseState.INQUIRY}, {"state": CaseState.INVESTIGATING}),
    "source": narrows(
        {"source": "copilot"},
        {"source": "slack"},
        dropped_by={"InMemoryCaseRepository": _IN_MEMORY_DROPS_SOURCE},
    ),
    "limit": pages({"limit": 2}, {"limit": 4}),
    "offset": pages({"limit": 2, "offset": 0}, {"limit": 2, "offset": 2}),
}


# -- Arm 2: CaseSearchRequest -------------------------------------------------

SEARCH_REQUEST_RULES: Mapping[str, Rule] = {
    "query": narrows({"query": "widget"}, {"query": "gadget"}),
    "team_id": narrows(
        {"query": "widget", "team_id": "team_a"},
        {"query": "widget", "team_id": "team_b"},
    ),
    "limit": pages({"query": "widget", "limit": 1}, {"query": "widget", "limit": 3}),
    "state": field_exempt(
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
    "user_id": field_exempt(
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
    "organization_id": field_exempt(
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


def _build_schema_template(path: Path) -> None:
    """Create the whole schema, plus the tenancy rows, ONCE into ``path``.

    Built with a SYNC engine deliberately. ``Base.metadata.create_all`` issues
    DDL for 41 tables, and aiosqlite hops to a worker thread per statement:
    measured at ~0.7s through the async driver against 0.139s synchronously, for
    identical output. The file is then copied per test (about a millisecond),
    which is also why the SQLite arm became file-backed rather than
    ``:memory:`` — a ``:memory:`` database lives in its engine's pooled
    connection, so it cannot be built once and reused, and a cache keyed on the
    URL would never hit.

    Isolation gets STRONGER, not weaker: every test now owns a private file, so
    no row a test writes can reach another. The property under test is the WHERE
    clause, not the storage medium, and the Sessionless arm was already
    file-backed.

    The tenancy rows are the parents ``cases`` carries foreign keys to. They are
    needed wherever foreign keys are enforced — the application engine sets
    ``PRAGMA foreign_keys=ON`` per connection — and writing them into the
    template keeps both SQLite arms seeded identically rather than leaving one
    of them depending on the pragma being off.
    """
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:eid, 'Guard Enterprise', 'guard-enterprise')"
                ),
                {"eid": SEED_ENTERPRISE},
            )
            connection.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, "
                    "email, display_name) "
                    "VALUES (:uid, :eid, :uid, :email, 'Seed Owner')"
                ),
                {
                    "uid": SEED_OWNER,
                    "eid": SEED_ENTERPRISE,
                    "email": f"{SEED_OWNER}@test",
                },
            )
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def sqlite_schema_template(tmp_path_factory) -> Path:
    """The prebuilt database every SQLite-backed arm starts from."""
    template = tmp_path_factory.mktemp("declared-filters-guard") / "template.db"
    _build_schema_template(template)
    return template


@asynccontextmanager
async def _in_memory_repository():
    repository = InMemoryCaseRepository()
    await _seed(repository)
    yield repository


@asynccontextmanager
async def _sqlite_repository(template: Path, tmp_path: Path):
    database = tmp_path / "sqlite-arm.db"
    shutil.copyfile(template, database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            repository = SQLiteCaseRepository(session)
            await _seed(repository)
            yield repository
    finally:
        await engine.dispose()


@asynccontextmanager
async def _sessionless_repository(template: Path, tmp_path: Path):
    """The repository a standalone deployment actually runs.

    ``create_case_repository`` picks ``SessionlessCaseRepository`` whenever
    DATABASE_URL names a persistent database, and it resolves a session per
    call through the module-level engine — so exercising it means standing that
    engine up. It holds no predicate logic of its own; it forwards. Forwarding
    is precisely where this defect class lives, which is why it is worth a real
    round trip rather than a signature check.
    """
    database = tmp_path / "sessionless-arm.db"
    shutil.copyfile(template, database)
    previous_engine = database_module._engine
    previous_factory = database_module._session_factory
    database_module.reset_engine()
    url = f"sqlite+aiosqlite:///{database}"
    engine = None
    try:
        engine = database_module.get_engine(url)
        database_module.get_session_factory(url)
        repository = SessionlessCaseRepository()
        await _seed(repository)
        yield repository
    finally:
        # RESTORE FIRST, dispose second, and hold the engine locally to dispose
        # it. `close_database()` awaits `engine.dispose()`, which can raise —
        # and doing that before the restore would leave the module globals bound
        # to a tmp SQLite file that pytest then deletes, for every later test in
        # this worker. The cleanup of a borrowed global must not be able to fail
        # halfway and keep the loan.
        database_module._engine = previous_engine
        database_module._session_factory = previous_factory
        if engine is not None:
            await engine.dispose()


#: PROCESS clocks. One control and one of each sign, which is what a naive bound
#: can actually distinguish: a positive offset makes a process-local misreading
#: land EARLIER in UTC, a negative one LATER, and only the side it lands on
#: decides whether a seeded instant is crossed. Pacific/Auckland is deliberately
#: NOT here — its date-line property is delivered by the ``as_local`` SPELLING
#: sub-assertion inside every parametrization, which does not depend on the
#: process clock, so running it as a process zone as well was duplication.
#: (America/Los_Angeles's 2026 DST switch is 8 March, one day outside the seeded
#: window, so nothing here exercises an offset changing mid-window.)
TIMEZONES = ("UTC", "Asia/Kolkata", "America/Los_Angeles")

#: Hours-of-day for the NAIVE bounds, chosen either side of the seeded 12:00
#: instants so that one of them is crossed by a positive-offset misreading and
#: the other by a negative one. The test asserts that at least one of them is
#: genuinely discriminating under each non-UTC zone, so this pair cannot quietly
#: stop doing its job.
_NAIVE_BOUND_HOURS = (6, 15)


@pytest.fixture
def process_timezone(request, monkeypatch):
    """Move the PROCESS clock, not just the test's arithmetic.

    A test written in UTC passes in CI and ships wrong behaviour to everyone
    else. The bounds are instants, so the property they support must hold under
    any local clock — a claim about the code, and only provable by moving the
    clock the code runs under.

    Every test gets this fixture, because ``seeded_repository`` depends on it;
    only the ones that parametrize it indirectly actually move the clock. That
    is what puts it strictly BEFORE the seeding, so the writes happen under the
    requested zone too. Unparametrized, ``request.param`` does not exist and the
    fixture leaves the clock exactly where it found it.
    """
    zone = getattr(request, "param", None)
    if zone is None:
        yield None
        return
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    yield zone
    monkeypatch.undo()
    time.tzset()


REPOSITORY_ARMS = (
    "InMemoryCaseRepository",
    "SQLiteCaseRepository",
    "SessionlessCaseRepository",
)


@dataclass(frozen=True)
class SeededRepository:
    """A seeded repository, and the process clock it was seeded UNDER."""

    name: str
    repository: Any
    #: ``time.tzname`` as it stood while the rows were written. The timezone
    #: tests assert on this: it is the only evidence that the WRITE half of the
    #: round trip ran under the clock they claim to be testing.
    tzname_at_seed: tuple[str, ...]


@pytest.fixture(params=REPOSITORY_ARMS)
async def seeded_repository(
    request, tmp_path, process_timezone, sqlite_schema_template
):
    """Every repository that can be stood up in-process, already seeded.

    It depends on ``process_timezone`` — a real dependency edge, not a hope
    about signature order. pytest builds a fixture's dependencies before the
    fixture, so this guarantees the clock is already moved when the rows are
    WRITTEN. Listing the two side by side in a test's signature does not: it
    left every INSERT running under the original clock, which is precisely the
    half that matters, because ``created_at`` is adapter-rendered into SQLite
    TEXT on the way in and that is what makes the comparison lexicographic.

    ``PostgreSQLHybridCaseRepository`` is the fourth implementation and is NOT
    here: its SQL is PostgreSQL-only (``to_tsvector``/``ts_rank``, ``::jsonb``,
    ``FILTER (WHERE ...)``), so it needs a live server and belongs to the
    postgres-marked suite rather than to a unit test.
    """
    if request.param == "InMemoryCaseRepository":
        async with _in_memory_repository() as repository:
            yield SeededRepository(request.param, repository, time.tzname)
    elif request.param == "SQLiteCaseRepository":
        async with _sqlite_repository(sqlite_schema_template, tmp_path) as repository:
            yield SeededRepository(request.param, repository, time.tzname)
    else:
        async with _sessionless_repository(
            sqlite_schema_template, tmp_path
        ) as repository:
            yield SeededRepository(request.param, repository, time.tzname)


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
    """One request model, ONE service method, and the rules for that pairing.

    Driven at the SERVICE, not at the repository. That is the whole point:
    ``CaseSearchRequest.state`` reaches a repository that has a ``state`` column
    and is still never applied, because the service never reads it. A
    repository-level probe would report it green.

    ``service_method`` names the method ``run`` drives, and it is not decoration:
    it is what ties a route to the surface that proves that route's parameters
    reach a query (:func:`test_every_route_parameter_is_proved_by_its_own_reader`).
    One model can have several surfaces — ``CaseListFilter`` has two, because
    ``list_user_cases`` and ``list_all_cases`` are different readers and either
    could stop reading a field without the other noticing.

    ``covers`` is the subset of the model's fields this surface is responsible
    for; ``None`` means all of them. A secondary surface narrows it to what its
    route can actually set, and every field must still be covered by at least
    one surface — so narrowing delegates responsibility rather than dropping it.
    """

    name: str
    model: type[BaseModel]
    rules: Mapping[str, Rule]
    run: Callable[[CaseService, BaseModel], Awaitable[Outcome]]
    publishes_total: bool
    service_method: str
    covers: frozenset[str] | None = None
    no_total_because: str = ""

    def covered_fields(self) -> frozenset[str]:
        if self.covers is not None:
            return self.covers
        return frozenset(self.model.model_fields)


async def _run_list(service: CaseService, request: BaseModel) -> Outcome:
    summaries, total = await service.list_user_cases(SEED_OWNER, request)
    return Outcome(frozenset(s.case_id for s in summaries), total)


async def _run_list_all(service: CaseService, request: BaseModel) -> Outcome:
    summaries, total = await service.list_all_cases(request)
    return Outcome(frozenset(s.case_id for s in summaries), total)


async def _run_search(service: CaseService, request: BaseModel) -> Outcome:
    summaries = await service.search_cases(request, SEED_OWNER)
    return Outcome(frozenset(s.case_id for s in summaries), None)


#: Guarding another request model is ONE entry here plus its rule table: name
#: the model, name the service method, hand over the call that carries it to a
#: query, and say whether that call publishes a total. Everything else — walking
#: the fields, failing on an unclassified one, running the pair against every
#: repository — is already generic. The rule table is the part that cannot be
#: generated, because deciding what each field is *supposed* to do is the work.
MODEL_SURFACES = (
    ModelSurface(
        name="GET /api/v1/cases → CaseListFilter",
        model=CaseListFilter,
        rules=LIST_FILTER_RULES,
        run=_run_list,
        publishes_total=True,
        service_method="list_user_cases",
    ),
    ModelSurface(
        name="GET /api/v1/admin/cases → CaseListFilter",
        model=CaseListFilter,
        rules=ADMIN_LIST_FILTER_RULES,
        run=_run_list_all,
        publishes_total=True,
        service_method="list_all_cases",
        covers=frozenset({"state", "source", "limit", "offset"}),
    ),
    ModelSurface(
        name="POST /api/v1/cases/search → CaseSearchRequest",
        model=CaseSearchRequest,
        rules=SEARCH_REQUEST_RULES,
        run=_run_search,
        publishes_total=False,
        service_method="search_cases",
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
    """The one route with this endpoint name, or a named collection error.

    ``next()`` without a default raises a bare ``StopIteration`` at module
    scope, which makes the WHOLE file uncollectable — arm 2, the registry tests
    and the timezone tests included — while naming nothing. Every other failure
    here is a guided assertion; this one has no business being the exception.
    """
    for route in router.routes:
        if isinstance(route, APIRoute) and route.name == name:
            return route
    known = sorted(r.name for r in router.routes if isinstance(r, APIRoute))
    raise LookupError(
        f"This guard is registered against an endpoint named {name!r}, which "
        f"no longer exists on this router. It was probably renamed; point the "
        f"RouteSurface at its new name. Endpoints on this router: {known}"
    )


#: Resolved once, and shared by the RouteSurface table and the guided-failure
#: handler — which needs the route to tell "removed" from "moved behind a
#: Depends()".
_LIST_ROUTE = _route(case_routes.router, "list_cases")
_ADMIN_LIST_ROUTE = _route(admin_cases.router, "list_all_cases")


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


_UNEXPECTED_KWARG = re.compile(r"unexpected keyword argument '([^']+)'")


@asynccontextmanager
async def _guided_call(surface_name: str, route: APIRoute):
    """Turn a signature mismatch into the RIGHT sentence, not just a sentence.

    The capture arm invokes the endpoint as a plain function and passes each
    probe value BY KEYWORD, so a parameter that leaves the signature raises
    ``TypeError: got an unexpected keyword argument`` — a failure that names the
    symptom and not the cause. There are two causes and they want opposite
    responses, so the declared list decides which one is reported: a parameter
    that is GONE means somebody fixed its defect and the exemption should be
    deleted; one that is still declared has merely moved behind a ``Depends()``,
    which the coverage arm reads correctly and this arm must be taught to call
    through. Guessing between them is how a guard sends an author to the wrong
    file.
    """
    try:
        yield
    except TypeError as exc:  # pragma: no cover — only on such a change
        match = _UNEXPECTED_KWARG.search(str(exc))
        if match is None:
            raise
        name = match.group(1)
        if name not in _declared_query_params(route):
            raise AssertionError(
                f"{surface_name} no longer declares ?{name}= at all, and this "
                f"guard is still probing for it. If removing it was the fix for "
                f"its issue, delete its row from this route's rule table — an "
                f"exemption must not outlive the defect it excuses."
            ) from exc
        raise AssertionError(
            f"{surface_name} still declares ?{name}= but no longer accepts it "
            f"as a direct keyword argument. It has been factored behind a "
            f"Depends(), which the coverage arm reads correctly but this "
            f"capture arm cannot call through. Update the capture helper to "
            f"build that dependency's value and pass it under its own parameter "
            f"name."
        ) from exc


async def _capture_list_filter(probe: Mapping[str, Any]) -> BaseModel:
    service = _CapturingListService()
    async with _guided_call("GET /api/v1/cases", _LIST_ROUTE):
        await case_routes.list_cases(
            response=Response(),
            case_service=service,
            current_user=_StubPrincipal(),
            **_resolve(probe),
        )
    return service.filters


async def _capture_admin_list_filter(probe: Mapping[str, Any]) -> BaseModel:
    service = _CapturingListService()
    async with _guided_call("GET /api/v1/admin/cases", _ADMIN_LIST_ROUTE):
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
    #: The ``ICaseService`` method this route hands the filter to. It is what
    #: links the route to the arm-2 surface that proves its parameters reach a
    #: query, rather than merely reach a constructor.
    service_method: str
    rules: Mapping[str, Rule]
    probe: Mapping[str, Any]
    capture: Callable[[Mapping[str, Any]], Awaitable[BaseModel]]


#: Same shape for a route: one entry naming the route, the filter model it
#: feeds, a probe value per declared parameter, and the call that captures what
#: the endpoint built.
ROUTE_SURFACES = (
    RouteSurface(
        name="GET /api/v1/cases",
        route=_LIST_ROUTE,
        target_model=CaseListFilter,
        service_method="list_user_cases",
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
        route=_ADMIN_LIST_ROUTE,
        target_model=CaseListFilter,
        service_method="list_all_cases",
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


def _declared_query_params(route: APIRoute) -> list[str]:
    """Every query parameter the route publishes — sub-dependencies included.

    ``get_flat_params`` is what ``fastapi.openapi.utils.get_openapi_path``
    itself calls to build an operation's parameter list, so this returns exactly
    what lands in the OpenAPI document. ``route.dependant.query_params`` does
    NOT: it is the top-level dependant's own list, and ``get_flat_params``
    flattens sub-dependants first (``get_flat_dependant(..., skip_repeats=True)``).

    The difference is invisible today — both routes declare their parameters
    inline, and the two calls return the same nine and four names — and would
    become a hole the moment anyone factors shared parameters behind a
    ``Depends()``, which is the ordinary way to share them between these two
    very routes. The flattened read has no such blind spot, so it is the one to
    build the guard on rather than the one that happens to agree right now.
    """
    return [
        p.name
        for p in get_flat_params(route.dependant)
        if p.field_info.in_ == ParamTypes.query
    ]


def _route_ids(surface: RouteSurface) -> list[str]:
    return _declared_query_params(surface.route)


@pytest.mark.parametrize("surface", ROUTE_SURFACES, ids=lambda s: s.name)
def test_every_declared_query_parameter_is_classified(surface: RouteSurface) -> None:
    """A query parameter nobody classified is the defect, not a gap in the test.

    The names come from ``get_flat_params(route.dependant)`` — the very call
    ``fastapi.openapi.utils.get_openapi_path`` makes to build the operation's
    parameter list, so this IS what the OpenAPI document publishes. Reading the
    signature ourselves would be reconstructing the artifact instead of
    capturing it, and reading the top-level ``dependant.query_params`` would
    capture a narrower artifact than the one clients are shown.
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
        f'`"<name>": route_exempt(reason=..., issue="#NNNN")`.\n'
        f"Omitting it is how include_archived (#1413) survived: accepted, "
        f"published, and filtering nothing."
    )

    # A parameter that has GONE is the other half of the EXEMPT contract, and
    # the half that is easy to miss: an exemption asserts the defect is still
    # there, so it must go red both when the parameter starts being applied AND
    # when it is deleted. Only the second is what fixing #1413 looks like — the
    # route simply stops declaring `include_archived` — and the "is it still
    # dropped?" assertion cannot see that, because a parameter that no longer
    # exists is trivially still dropped.
    retired = sorted(set(surface.rules) - declared)
    still_exempt = [n for n in retired if surface.rules[n].verdict is Verdict.EXEMPT]
    assert not retired, (
        f"{surface.name} no longer declares {retired}, but this guard still "
        f"classifies them."
        + (
            f"\n{still_exempt} were exempt ("
            + ", ".join(surface.rules[n].issue for n in still_exempt)
            + "). If removing the parameter WAS the fix, that is the exemption "
            "doing its job — delete its row. An exemption must not outlive the "
            "defect it excuses."
            if still_exempt
            else " Delete the stale rule(s)."
        )
    )

    # The probe table must cover every declared parameter, or the capture below
    # calls the endpoint as a plain function with a parameter left unpassed —
    # which keeps its raw ``fastapi.params.Query`` DEFAULT rather than ``None``,
    # and the resulting ValidationError surfaces as "this route no longer feeds
    # that model" (or, on the operator route, escapes raw). Both point the
    # author at the wrong thing entirely.
    assert set(surface.probe) == declared, (
        f"{surface.name}: the arm-1 probe table and the route's declared "
        f"parameters disagree.\n"
        f"  missing from the probe: {sorted(declared - set(surface.probe))}\n"
        f"  probed but not declared: {sorted(set(surface.probe) - declared)}\n"
        f"Give every declared parameter a probe value distinct from its own "
        f"default; an unpassed one is not 'unset', it is the Query object."
    )

    # Route tables and model tables share one Rule type but not one vocabulary.
    # A `narrows(...)` pasted into a route table would fall through the arm-1
    # branch below and assert the exact inverse of its intent, reported as an
    # exemption citing nothing.
    wrong_arm = sorted(
        name
        for name, rule in surface.rules.items()
        if rule.verdict not in ROUTE_VERDICTS
    )
    assert not wrong_arm, (
        f"{surface.name} classifies {wrong_arm} with an arm-2 verdict. A route "
        f"table takes reaches() or route_exempt() only — narrows()/pages() "
        f"describe what a FIELD does to a query and belong in that route's "
        f"ModelSurface rule table instead."
    )


@pytest.mark.parametrize("surface", ROUTE_SURFACES, ids=lambda s: s.name)
async def test_declared_query_parameters_reach_the_filter_model(
    surface: RouteSurface,
) -> None:
    """Call the route; CAPTURE the filter it built; check each parameter arrived.

    Captured, never reconstructed. The assertion is made against the very
    ``CaseListFilter`` instance the endpoint handed to the service, so it cannot
    be satisfied by a model this test built from its own assumptions.

    Scope, stated rather than left to be inferred: the endpoint is invoked as a
    plain Python function, so this exercises the SIGNATURE handoff — does the
    value the route received reach the filter model intact — and not the wire.
    Query-string parsing, aliases and ``Literal`` rejection are FastAPI's own
    and are not covered here. That is the right boundary for this file, whose
    subject is a value that survives validation and then goes nowhere.
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
        elif rule.verdict is Verdict.EXEMPT:
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
        else:  # pragma: no cover — the shape check above forbids reaching here
            pytest.fail(
                f"{surface.name}?{name}= carries verdict {rule.verdict.name}, "
                f"which arm 1 has no meaning for. Route tables take reaches() "
                f"or route_exempt() only."
            )


async def test_the_filter_model_silently_swallows_an_undeclared_parameter() -> None:
    """The mechanism itself, captured once.

    ``include_archived`` does not die at the route, in validation, or at the
    repository — it dies HERE, in a constructor that accepts anything and keeps
    only what it declared. Pinning the mechanism separately from the instance
    means the next field to hit it is recognised rather than rediscovered.
    """
    assert "include_archived" not in CaseListFilter.model_fields
    # The invariant is about `extra`, not about the config being empty. An
    # unrelated key — populate_by_name, json_schema_extra, a ConfigDict on a
    # shared base — would otherwise turn this red with a message about
    # extra="forbid" that is simply false, and send the author to the wrong
    # issue.
    assert CaseListFilter.model_config.get("extra") in (None, "ignore"), (
        f"CaseListFilter now sets extra="
        f"{CaseListFilter.model_config.get('extra')!r}. The silent-drop "
        f"mechanism this file pins is gone, so #1413 has a louder failure mode "
        f"than the rest of this guard assumes — re-read those assertions rather "
        f"than relaxing this one."
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


@pytest.mark.parametrize("surface", MODEL_SURFACES, ids=lambda s: s.name)
def test_every_request_model_field_is_classified(surface: ModelSurface) -> None:
    """A field nobody classified fails. That is the entire point of the file."""
    covered = surface.covered_fields()
    unknown = sorted(covered - set(surface.model.model_fields))
    assert not unknown, (
        f"{surface.name} claims responsibility for {unknown}, which "
        f"{surface.model.__name__} does not declare. Fix the surface's "
        f"`covers`."
    )

    unclassified = sorted(covered - set(surface.rules))
    assert not unclassified, (
        f"{surface.name} covers field(s) {unclassified} that this guard has no "
        f"verdict for.\n"
        f"Decide, in tests/unit/modules/case/"
        f"test_declared_filters_reach_the_query.py:\n"
        f"  • it reaches a query → `narrows(baseline, variant)` (page AND "
        f"total_count move) or `pages(baseline, variant)` (page moves, "
        f"total_count does not);\n"
        f"  • it is declared and applied to nothing → `field_exempt(baseline, "
        f'variant, reason=..., issue="#NNNN")`.\n'
        f"A field with no verdict is exactly CaseSearchRequest.state (#1416): "
        f"declared, validated, published, read by nobody."
    )

    stale = sorted(set(surface.rules) - covered)
    assert not stale, (
        f"{surface.name} classifies {stale}, which it does not cover. Either "
        f"widen `covers` or delete the stale rule(s)."
    )


@pytest.mark.parametrize(
    "model",
    sorted({s.model for s in MODEL_SURFACES}, key=lambda m: m.__name__),
    ids=lambda m: m.__name__,
)
def test_no_model_field_escapes_every_surface(model: type[BaseModel]) -> None:
    """A narrowed `covers` delegates responsibility; it must not drop it.

    A secondary surface may declare it is answerable for a subset — the operator
    route publishes four of CaseListFilter's ten fields, and the other six are
    unreachable on that path rather than accepted-and-ignored. What must stay
    true across the whole registry is that no field is covered by NOBODY, or
    `covers` becomes a way to make a field disappear from the guard.
    """
    covered: set[str] = set()
    for surface in MODEL_SURFACES:
        if surface.model is model:
            covered |= surface.covered_fields()
    orphaned = sorted(set(model.model_fields) - covered)
    assert not orphaned, (
        f"{model.__name__} field(s) {orphaned} are covered by no surface at "
        f"all. Every field must be somebody's responsibility — widen a "
        f"surface's `covers`, or add the surface that can actually set them."
    )


@pytest.mark.parametrize("surface", ROUTE_SURFACES, ids=lambda s: s.name)
def test_every_route_parameter_is_proved_by_its_own_reader(
    surface: RouteSurface,
) -> None:
    """Landing on a filter model is not reaching a query. This is the seam.

    Arm 1 proves a parameter arrives on a ``CaseListFilter``. Arm 2 proves a
    ``CaseListFilter`` field reaches a query — but only through the service
    method it drives. Between the two sits the assumption that they are the
    same method, and for ``GET /api/v1/admin/cases`` they were not:
    ``list_all_cases`` is a separate reader, and it could have stopped passing
    ``state`` and ``source`` to the repository with every test in this file
    still green.

    So the link is asserted rather than assumed: for each route, find the
    surface driven by the method THAT route calls, and require every parameter
    classified ``reaches()`` to carry a discriminating rule there.
    """
    readers = [
        m
        for m in MODEL_SURFACES
        if m.model is surface.target_model
        and m.service_method == surface.service_method
    ]
    assert readers, (
        f"{surface.name} hands its {surface.target_model.__name__} to "
        f"CaseService.{surface.service_method}, and no ModelSurface drives that "
        f"method. Arm 1 can only prove the parameters reach a CONSTRUCTOR; "
        f"without a surface over the route's own reader, nothing proves they "
        f"reach a query — which is the whole defect this file exists to catch. "
        f"Add a ModelSurface with service_method={surface.service_method!r}."
    )

    proved: set[str] = set()
    for reader in readers:
        proved |= set(reader.rules)

    unproved = sorted(
        name
        for name, rule in surface.rules.items()
        if rule.verdict is Verdict.REACHES and name not in proved
    )
    assert not unproved, (
        f"{surface.name} classifies {unproved} as reaching "
        f"{surface.target_model.__name__}, but no rule under "
        f"CaseService.{surface.service_method} proves any of them reach a "
        f"query. Arm 1 stops at the constructor. Add a discriminating pair for "
        f"each to the rule table of the surface driving that method."
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

    for repository_name in rule.dropped_by:
        assert repository_name in REPOSITORY_ARMS, (
            f"{surface.model.__name__}.{field_name} records a gap on "
            f"{repository_name!r}, which is not a repository this guard runs."
        )


def _every_excuse() -> list[tuple[str, str, str]]:
    """Every EXCUSE in the file, as (where, what, issue).

    An excuse is any entry that says "this does not apply, and that is known":
    a whole-field or whole-parameter exemption, and a per-repository gap. Both
    arms, both kinds. They were separate before, and the consequence was that
    ``dropped_by`` notes never reached the citation rule at all — the test
    returned early unless the verdict was EXEMPT, and a gap hangs off a NARROWS
    rule.
    """
    rows: list[tuple[str, str, str]] = []

    def collect(where: str, rules: Mapping[str, Rule]) -> None:
        for name, rule in rules.items():
            if rule.verdict is Verdict.EXEMPT:
                rows.append((where, name, rule.issue))
            for repository_name, gap in rule.dropped_by.items():
                rows.append((where, f"{name} on {repository_name}", gap.issue))

    for route_surface in ROUTE_SURFACES:
        collect(route_surface.name, route_surface.rules)
    for model_surface in MODEL_SURFACES:
        collect(model_surface.name, model_surface.rules)
    return rows


def _every_excuse_reason() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    def collect(where: str, rules: Mapping[str, Rule]) -> None:
        for name, rule in rules.items():
            if rule.verdict is Verdict.EXEMPT:
                rows.append((where, name, rule.reason))
            for repository_name, gap in rule.dropped_by.items():
                rows.append((where, f"{name} on {repository_name}", gap.reason))

    for route_surface in ROUTE_SURFACES:
        collect(route_surface.name, route_surface.rules)
    for model_surface in MODEL_SURFACES:
        collect(model_surface.name, model_surface.rules)
    return rows


@pytest.mark.parametrize(
    "where,what,issue", _every_excuse(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_exemption_cites_something_lookupable(
    where: str, what: str, issue: str
) -> None:
    """An exemption without provenance is indistinguishable from an oversight.

    Every arm and every kind: a route parameter can be exempted just as a model
    field can (``include_archived`` is one), and a per-repository gap excuses
    exactly as much behaviour as a whole-field exemption does. An excuse nobody
    can look up is the failure mode this file exists to end, so none of them
    gets to skip the rule.
    """
    assert _ISSUE_REFERENCE.match(issue), (
        f"{where} / {what} is excused citing {issue!r}. Cite a '#NNNN' issue, "
        f"or admit 'UNREPORTED: ...' in the registry itself — an excuse nobody "
        f"can look up is indistinguishable from an oversight."
    )


@pytest.mark.parametrize(
    "where,what,reason",
    _every_excuse_reason(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_exemption_says_why(where: str, what: str, reason: str) -> None:
    """The issue says where to look; the reason says what was measured."""
    assert reason.strip(), f"{where} / {what} is excused with no reason."


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
    gap = rule.dropped_by.get(repository_name)
    failures: list[str] = []

    page_moved = baseline.ids != variant.ids
    claims_a_difference = rule.verdict in (Verdict.NARROWS, Verdict.PAGES) and not gap

    # BOTH SIDES NON-EMPTY, FOR EVERY VERDICT — including EXEMPT and a gap.
    # `list_user_cases` ends in `except Exception: return [], 0`, so an empty
    # answer means "no rows" and "this blew up" at the same time. Restricting
    # this to the verdicts that claim a DIFFERENCE was the same reasoning error
    # one level up, in its more dangerous direction: an exemption is a claim
    # that two requests answer ALIKE, and ([], 0) == ([], 0) confirms it
    # perfectly for a repository that raised on both calls. Measured: a
    # RuntimeError at the top of list_user_cases used to leave `user_id`,
    # `organization_id` and `source` reported as still-defective-as-classified
    # when nothing had been measured at all.
    if not baseline.ids or not variant.ids:
        failures.append(
            f"{where}: the pair answered EMPTY on at least one side "
            f"(baseline={sorted(baseline.ids)}, variant={sorted(variant.ids)}). "
            f"Nothing was measured, so neither a difference nor a sameness can "
            f"be concluded — an empty answer is also what the service's blanket "
            f"`except Exception: return [], 0` produces. Fix the seed corpus so "
            f"both sides match something, or fix whatever is raising."
        )
        return failures

    if claims_a_difference:
        if not page_moved:
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
    if rule.verdict is Verdict.NARROWS and not gap and page_moved:
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

    if gap and changed:
        failures.append(
            f"{where} now applies the field, but this guard still records a gap "
            f"on {repository_name} ({gap.issue}):\n    {gap.reason}\n"
            f"  If it has been implemented, delete the dropped_by entry."
        )

    return failures


@pytest.mark.parametrize("surface", MODEL_SURFACES, ids=lambda s: s.name)
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
    repository_name = seeded_repository.name
    service = _service(seeded_repository.repository)

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


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
@pytest.mark.parametrize("process_timezone", TIMEZONES, indirect=True)
async def test_creation_bounds_are_instants_under_any_process_timezone(
    seeded_repository: SeededRepository, process_timezone
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

    Every bound probed here is INTERIOR to the corpus, so both expectations are
    non-empty. An exterior bound makes one of them ``frozenset()`` and ``0`` —
    which is exactly what ``list_user_cases``' blanket ``except Exception:
    return [], 0`` produces for a repository that raised, so the assertion would
    be satisfied by a crash. The precondition below states that in the test
    rather than leaving it to the reader to notice the range still matches the
    corpus.
    """
    service = _service(seeded_repository.repository)
    corpus = _corpus()

    # The write half ran under this clock too — not just the reads. That is the
    # half that matters, and it is only true because `seeded_repository`
    # DEPENDS on `process_timezone` rather than being listed beside it.
    assert seeded_repository.tzname_at_seed == time.tzname, (
        f"The rows were seeded under TZ {seeded_repository.tzname_at_seed} but "
        f"are being read under {time.tzname}. The clock must move before the "
        f"INSERTs: `created_at` is rendered to TEXT on its way into SQLite, and "
        f"that rendering is what this test exists to pin."
    )

    for offset in _INTERIOR_BOUNDS:
        bound = _day(offset)

        expected_after = {c.case_id for c in corpus if c.created_at >= bound}
        expected_before = {c.case_id for c in corpus if c.created_at < bound}
        assert expected_after and expected_before, (
            f"_INTERIOR_BOUNDS includes day {offset}, which is outside the "
            f"corpus: one side expects nothing, and 'nothing' is also what a "
            f"raising repository returns. Pick bounds strictly inside the "
            f"seeded range."
        )

        seen = await _run_list(service, CaseListFilter(created_after=bound))
        assert seen.ids == frozenset(expected_after), (
            f"created_after={bound.isoformat()} under TZ={process_timezone} on "
            f"{seeded_repository.name}: expected {sorted(expected_after)}, got "
            f"{sorted(seen.ids)}. The lower bound is INCLUSIVE and is an "
            f"instant — the process clock must not enter into it."
        )
        assert seen.total == len(expected_after)

        seen_before = await _run_list(service, CaseListFilter(created_before=bound))
        assert seen_before.ids == frozenset(expected_before), (
            f"created_before={bound.isoformat()} under TZ={process_timezone} on "
            f"{seeded_repository.name}: expected {sorted(expected_before)}, got "
            f"{sorted(seen_before.ids)}. The upper bound is EXCLUSIVE."
        )
        assert seen_before.total == len(expected_before)

        # Pacific/Auckland is the far side of the date line and Asia/Kolkata a
        # half-hour offset: between them they break any bound that is secretly
        # a date, and any comparison that is secretly lexicographic. These are
        # SPELLINGS of the bound, independent of the process clock — which is
        # why Auckland does not also need to be a process zone.
        for zone in ("Asia/Kolkata", "Pacific/Auckland"):
            as_local = bound.astimezone(ZoneInfo(zone))
            # The two spellings must differ where it counts — otherwise the
            # comparison below is asking nothing. (Asserting `as_local == bound`
            # would be the tautology it looks like: `astimezone` preserves the
            # instant and `==` compares instants, so it holds for every input.)
            assert as_local.utcoffset() != bound.utcoffset(), (
                f"{zone} resolves to the same UTC offset as the bound itself at "
                f"{bound.isoformat()}, so this iteration compares a spelling "
                f"with itself. Pick a zone that is genuinely offset here."
            )
            local_answer = await _run_list(
                service, CaseListFilter(created_after=as_local)
            )
            assert local_answer == seen, (
                f"{seeded_repository.name} under TZ={process_timezone}: "
                f"created_after={bound.isoformat()} and the same instant "
                f"written {as_local.isoformat()} returned different rows "
                f"({sorted(seen.ids)} vs {sorted(local_answer.ids)})."
            )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
@pytest.mark.parametrize("process_timezone", TIMEZONES, indirect=True)
async def test_a_bound_with_no_offset_is_read_as_utc_not_as_process_local(
    seeded_repository: SeededRepository, process_timezone
) -> None:
    """The one assertion that makes the process-zone matrix mean anything.

    Every OTHER bound in this file is timezone-AWARE, and both normalisers —
    ``api_models.bound_to_utc`` and ``created_bounds.to_utc`` — handle an aware
    value with ``astimezone(timezone.utc)``, which consults no clock. So the
    process zone could not change those answers whatever it was set to, and the
    matrix around them was decoration. Measured: replacing ``to_utc`` with the
    process-local reading left every timezone test passing.

    A NAIVE bound is the lever, because ``datetime.astimezone()`` on a naive
    value reads the SYSTEM zone. The route publishes the promise in as many
    words — "ISO-8601 with an offset; a value without one is read as UTC" — and
    nothing tested it. Here it is tested, at both places that make the promise:

    * through ``CaseListFilter``, whose validator anchors the bound before any
      repository sees it, and
    * straight at ``repository.list``, which ``created_bounds.to_utc`` guards
      precisely because a caller can arrive without passing through the model.

    The second is not redundant: the model anchors first, so a defect in
    ``to_utc`` alone is invisible from the service path.
    """
    repository = seeded_repository.repository
    service = _service(repository)
    corpus = _corpus()

    assert seeded_repository.tzname_at_seed == time.tzname

    discriminating: list[str] = []

    for day_offset in _INTERIOR_BOUNDS:
        anchor = _day(day_offset)
        for hour in _NAIVE_BOUND_HOURS:
            naive = datetime(anchor.year, anchor.month, anchor.day, hour)
            assert naive.tzinfo is None  # the whole point of this test

            read_as_utc = naive.replace(tzinfo=UTC)
            expected = {c.case_id for c in corpus if c.created_at >= read_as_utc}

            # What a process-local misreading WOULD answer. Built with
            # `astimezone`, the operation the bug actually is, so it reads this
            # process's clock rather than a zone name we chose.
            misread = naive.astimezone(UTC)
            would_be = {c.case_id for c in corpus if c.created_at >= misread}
            if would_be != expected:
                discriminating.append(f"{naive.isoformat()} ({hour:02d}:00)")

            through_the_model = await _run_list(
                service, CaseListFilter(created_after=naive)
            )
            assert through_the_model.ids == frozenset(expected), (
                f"{seeded_repository.name} under TZ={process_timezone}: the "
                f"offsetless bound {naive.isoformat()} was read as "
                f"{misread.isoformat()} — this process's local time — instead "
                f"of {read_as_utc.isoformat()}. The route documents "
                f"'a value without one is read as UTC', and "
                f"api_models.bound_to_utc is what keeps that promise."
            )

            rows, total = await repository.list(user_id=SEED_OWNER, created_after=naive)
            assert {c.case_id for c in rows} == frozenset(expected), (
                f"{seeded_repository.name} under TZ={process_timezone}: the "
                f"offsetless bound {naive.isoformat()} handed STRAIGHT to "
                f"repository.list was read as process-local. "
                f"created_bounds.to_utc exists for exactly this caller — one "
                f"that did not pass through CaseListFilter's validator."
            )
            assert total == len(expected)

    if process_timezone != "UTC":
        assert discriminating, (
            f"Under TZ={process_timezone} not one probed bound would answer "
            f"differently if the offsetless value were read as process-local, "
            f"so this parametrization proves nothing. _NAIVE_BOUND_HOURS must "
            f"straddle a seeded instant in BOTH directions: a positive offset "
            f"moves the misreading earlier, a negative one later."
        )
