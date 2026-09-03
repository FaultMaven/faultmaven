"""Adversarial probe: walk the API surfaces as two real tenants (#1317).

Every link of the tenant chain already has its own probe. The *composition* did
not, and that is what #1252 records: four organizations exist on the live
deployment and nothing anchors the claim that a principal in one cannot observe
another through the API. Self-service sign-up turns every stranger into a
tenant, so this is a gate rather than a nicety.

What the siblings cover, and what they do not
---------------------------------------------
====================================================  ===================================
``test_rls_tenant_isolation.py``                      link 3 — the PostgreSQL policies
``test_tenant_scope_request_isolation.py``            link 2 — binder ordering/isolation
``test_multi_tenant_isolation_probe.py``              link 2's input — the claim itself
``test_kb_tenant_isolation_probe.py``                 the KB read filter, at the store
**this module**                                       **the surfaces, end to end**
====================================================  ===================================

None of them issues a request as tenant A for a row owned by tenant B. This one
does, over HTTP, through the real ``faultmaven.main`` application, against a real
PostgreSQL with the migrations applied — which is why it is marked ``postgres``.
SQLite proves nothing here: it has no RLS, so half the boundary would be absent.

The posture under test
----------------------
The app connects as a **non-superuser, non-owner role**, exactly as the deployed
``faultmaven_app`` role does (ADR-010, ``rls_role_guard``). This matters twice
over. A superuser bypasses RLS, so a probe run as the migration role would
measure the application filters alone and call it isolation. And the deployed
posture is precisely "application filter *and* RLS", so anything weaker is a
test of a system nobody runs.

``TENANT_PROVIDER=multi`` is in force, so the binder resolves a request's tenant
from the verified ``organization_id`` claim and the engine ``begin`` listener
applies it as ``app.current_org_id`` for every transaction the request opens.

Read this as: A's request is bound to A, and every store it can reach is scoped
to A by the same mechanism production uses.

How a pass is distinguished from an empty deployment
----------------------------------------------------
Every attack is paired with a **positive control**: the identical call, issued by
B, asserting the seeded row is found. A 200 with an empty list is a pass only
because B's control shows the same call non-empty; a 404 is a pass only because
B's control shows 200. Without the pairing, a probe against an app that lost its
database would be uniformly green.

Marker strings carry the tenant's name (``BETA-SECRET-...``), so a body scan
reports *whose* data leaked rather than "unexpected content". Every response
body A receives is scanned for every one of B's markers, including bodies whose
status code already looks like a refusal — a 500 that echoes the row is still a
leak, and so is a 403 whose detail names the case title.

Route coverage is derived, not remembered
-----------------------------------------
``test_every_tenant_scoped_route_is_in_the_inventory`` reads the **live app's**
OpenAPI document, selects every operation carrying a tenant-scoped path
parameter, request-body field or query parameter, and requires each one to
appear in :data:`SURFACE_INVENTORY` — as a probed surface or as an exemption
with a stated reason. A route added tomorrow that takes a ``{case_id}`` fails
this module until someone decides which it is. That is the only part of this
file that cannot rot silently.

The vector layer is called out separately
-----------------------------------------
#1168 records that ChromaDB carries no tenant dimension: KB isolation is derived
from the ``owner_id`` arm and one SQL ``WHERE``. So the KB surfaces here are
probed through the API *over a real ChromaDB seeded with B-owned chunks*, and a
pass on the SQL surfaces is explicitly not read as a pass on retrieval.

Shown to fail against a broken boundary
---------------------------------------
A probe that has only ever been green is indistinguishable from one that asserts
nothing. Every guard this module watches was mutated to its prior/broken
behaviour, the module was re-run, and the mutation was reverted from an
in-memory copy of the file (never ``git checkout``). Each mutation asserts its
own replacement count before the run, so a no-op edit cannot be mistaken for a
guard that held.

Two things make this table readable. Most SQL surfaces here are guarded
**twice** — an application-layer predicate and the PostgreSQL RLS policies — so a
mutation of one layer alone is caught by the other and the suite stays green.
That is a property of the system, not a hole in the probe, and it is recorded
rather than hidden: the rows say which layers had to go before an assertion
moved. "RLS bypassed" means the app was pointed at the table-owner role for that
run, which is the pre-ADR-010 posture.

=================================================================  ==========================================
Mutation (reverted from an in-memory copy after each run)          Went red
=================================================================  ==========================================
app connects as the table owner (RLS bypassed) — alone             ``..._debug_causal_graph_...`` only
``CaseService.get_case``'s owner ∪ shared check removed — alone    nothing (RLS caught every one)
the same, with RLS bypassed                                        15 cases: detail, ui, transcript,
                                                                   analytics, evidence, files, case data,
                                                                   reports, report-recommendations, the
                                                                   mutation battery, three case-addressed
                                                                   battery params, generate-by-case, debug
``list_user_cases(current_user.user_id, ...)`` → ``None``,         ``..._case_list_shows_a_only_a_and_b...``
with RLS bypassed                                                  + ``..._case_list_team_filter_...``
``repository.search(user_id=...)`` → ``None``, RLS bypassed        both ``..._case_search_...`` cases
``require_case_access`` drops its ``user_id`` — alone              nothing (the session service's own
                                                                   organization check caught it)
the same, plus that organization check, with RLS bypassed          three session params of the
                                                                   case-addressed battery
``build_kb_scope_filter``'s owner arm widened to                   ``..._semantic_kb_retrieval_...``
``{"owner_id": {"$nin": [...]}}`` (#1167's shape)
the ``knowledge_items`` org clause dropped from the read           nothing (the owner/team arms are
visibility rule, RLS bypassed                                      per-user, so they held on their own)
``get_visible_by_id`` → the unscoped ``get_by_id`` (pre-#867),     ``..._knowledge_item_is_not_readable_
RLS bypassed                                                       by_id_...``
``is_platform_admin`` also accepts the org ``admin`` role          ``..._tenant_admin_reaches_no_admin_
                                                                   route...`` + ``..._suggestions_are_
                                                                   scoped_to_the_operators_own_tenant``
``authorize_content_read`` returns standing access in cloud        both break-glass cases
(pre-#815)
``find_live_grant`` stops keying on ``target_case_id``             ``..._grant_unlocks_exactly_the_case_
                                                                   it_names``
the runbook publish target's team-membership check removed         ``..._runbook_cannot_be_published_into_
                                                                   the_other_tenants_team``
an entry deleted from ``SURFACE_INVENTORY``                        ``..._every_tenant_scoped_route_is_in_
                                                                   the_inventory``
an entry ADDED for a route the app does not expose                 the same case (the stale half)
the ``ENVIRONMENT`` pin removed, run under an ambient           the same case (its fixture errors
``ENVIRONMENT=production``                                         outright)
=================================================================  ==========================================

Two of those runs are findings in their own right, recorded here because a
mutation that changes nothing is evidence too:

* **``/debug/cases/{case_id}/causal-graph`` is guarded by RLS alone.** It takes
  no authentication and performs no case-access check — it calls
  ``repo.get(case_id)`` directly. Under the deployed cloud posture RLS covers
  it, and it is not registered when ``ENVIRONMENT=production``. On a
  non-production deployment without RLS (standalone on SQLite) it would serve
  any case to any caller. Left as an observation for the owner rather than
  fixed here; this module is test-only.
* **The knowledge and session surfaces each hold on two independent
  predicates.** Neither single-layer mutation moved an assertion. Worth knowing
  before anyone "simplifies" one of them on the grounds that the other exists.

Re-run those rather than trusting the table if you change the binder, the case
read filter, the KB scope filter, the share lookup or the operator-grant gate.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


# =============================================================================
# The two tenants, and the strings that name them
# =============================================================================
#
# Ids are generated per session rather than fixed, so a run that leaves rows
# behind cannot make the next run's "A sees nothing of B" pass by looking at its
# own leftovers.

_RUN = uuid.uuid4().hex[:8]

#: Marker substrings planted in every B-owned row. An assertion names the tenant
#: whose secret must not appear, so a failure says WHOSE data leaked.
SECRET_B = f"BETA-SECRET-{_RUN}"
SECRET_B_TITLE = f"{SECRET_B}-acquisition-outage"
SECRET_B_TRANSCRIPT = f"{SECRET_B}-transcript-line"
SECRET_B_EVIDENCE = f"{SECRET_B}-payroll-dsn"
SECRET_B_FILE = f"{SECRET_B}-oncall-rota.log"
SECRET_B_REPORT = f"{SECRET_B}-postmortem"
SECRET_B_KB_PERSONAL = f"{SECRET_B}-runbook-personal"
SECRET_B_KB_TEAM = f"{SECRET_B}-runbook-team"
SECRET_B_DRAFT = f"{SECRET_B}-conversion-draft"
SECRET_B_SUGGESTION = f"{SECRET_B}-suggestion"

#: A's own row, so a control can show the same call working for A.
SECRET_A = f"ALPHA-OWN-{_RUN}"

#: Everything that must never appear in a body A receives. Assembled once and
#: applied to EVERY attack response, including refusals: a 403 whose detail
#: echoes the title is a leak, and so is a 500 traceback carrying the row.
B_MARKERS = (
    SECRET_B_TITLE,
    SECRET_B_TRANSCRIPT,
    SECRET_B_EVIDENCE,
    SECRET_B_FILE,
    SECRET_B_REPORT,
    SECRET_B_KB_PERSONAL,
    SECRET_B_KB_TEAM,
    SECRET_B_DRAFT,
    SECRET_B_SUGGESTION,
)

#: The signing key the probe's ``AuthService`` verifies with. Local (HS256)
#: mode: the tokens here are forged directly, so a mint path is not needed.
_JWT_SECRET = "two-tenant-probe-secret-padded-to-32-bytes"

#: Statuses that count as "the surface refused". 401 is absent on purpose: the
#: attacker holds a VALID token, so a 401 would mean the probe failed to
#: authenticate and every assertion below it would be vacuous.
REFUSED = frozenset({403, 404, 409, 422})


# =============================================================================
# Environment: a limited role, and the app wired onto it
# =============================================================================

_LIMITED_ROLE = f"fm_2t_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_2t_probe_pw"
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""

#: Environment the probe app is built under. Restored wholesale in teardown —
#: the `-m postgres` lane runs this module inside the same session as
#: ``test_rls_tenant_isolation.py``, which reads ``DATABASE_URL`` expecting the
#: SUPERUSER url. Leaking the limited-role url into that module would make it
#: measure RLS as the wrong role and quietly stop proving anything.
_PROBE_ENV_KEYS = (
    "DATABASE_URL",
    "DEPLOYMENT_MODE",
    "TENANT_PROVIDER",
    "AUTH_MODE",
    "JWT_SECRET_KEY",
    "SKIP_SERVICE_CHECKS",
    "OAUTH_ENABLED",
    "ENVIRONMENT",
)


def _limited_url(superuser_url: str) -> str:
    from sqlalchemy.engine import make_url

    # ``render_as_string(hide_password=False)``, not ``str()``: SQLAlchemy's
    # ``URL.__str__`` masks the password as ``***``, and the resulting url fails
    # authentication with a message that names the role rather than the mask.
    return (
        make_url(superuser_url)
        .set(username=_LIMITED_ROLE, password=_LIMITED_PW)
        .render_as_string(hide_password=False)
    )


async def _create_limited_role(superuser_url: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
            await conn.exec_driver_sql(
                f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' "
                "NOSUPERUSER"
            )
            await conn.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
            )
            await conn.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}"
            )
            await conn.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {_LIMITED_ROLE}"
            )
            # Sequences too. ``case_actions.transition_id`` is a serial, so a
            # role without this cannot record a state transition — the probe
            # would then measure "permission denied for sequence" and read it
            # as a refusal. The deployed ``faultmaven_app`` role holds it.
            await conn.exec_driver_sql(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                f"TO {_LIMITED_ROLE}"
            )
    finally:
        await engine.dispose()


async def _drop_limited_role(superuser_url: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
    finally:
        await engine.dispose()


def _fresh_chroma():
    """An in-process ChromaDB with PINNED settings and an EMPTY KB collection.

    Same idiom, and the same reasons, as
    ``test_kb_tenant_isolation_probe._ephemeral_client``: chromadb caches one
    System per identifier and refuses a second client whose ``Settings`` differ,
    so an unpinned ``EphemeralClient`` here would sometimes be handed a store a
    sibling module seeded. The explicit drop is what keeps this module's corpus
    a function of its own fixture rather than of collection order.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KB_COLLECTION,
    )

    client = chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )
    try:
        client.delete_collection(KB_COLLECTION)
    except Exception:  # noqa: BLE001 - absent on the first client of a session
        pass
    return client


class _Tripwire:
    """A service that fails loudly the moment anything touches it.

    Some routes resolve a heavyweight collaborator (the milestone engine, the
    LLM router) as a FastAPI dependency, before their own case check runs. With
    the slot empty the dependency 503s and the case check never executes, so the
    probe would score "refused" without the guard having been consulted — the
    exact vacuous green this module exists to avoid.

    Filling the slot with this object makes the two outcomes distinguishable: if
    the case gate holds, the request is refused before anything is read off it;
    if the gate is missing, the first attribute access raises and the response
    is a 500 that no ``assert_refused`` accepts. It is a tripwire, not a stand-in
    — it implements nothing and can satisfy no assertion.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(
            f"the request reached {self._name}.{attribute} — the case-access "
            "check did not refuse it first"
        )


def _wire_services(app, chroma) -> None:
    """Put the REAL services on ``app.state``, over the limited-role engine.

    Not doubles. A double would answer from whatever the test seeded into it,
    which is exactly the assumption a tenant probe exists to test — the question
    is what the production read paths do when a foreign id reaches them.

    The one stand-in is ``ConversionService``'s LLM router: it is consulted only
    on the *generation* path, which this module never drives, and wiring a live
    provider would make a security gate depend on an API key.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.observability.tracing import OpikTracer
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.sessionless_operator_audit_repository import (  # noqa: E501
        SessionlessOperatorAuditRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_operator_grant_repository import (  # noqa: E501
        SessionlessOperatorGrantRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_share_repository import (
        SessionlessShareRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_team_repository import (
        SessionlessTeamRepository,
    )
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.modules.auth.domain.services.auth_service import AuthService
    from faultmaven.modules.auth.domain.services.auth_session_service import (
        AuthSessionService,
    )
    from faultmaven.modules.auth.domain.services.team_service import TeamService
    from faultmaven.modules.auth.domain.services.user_service import UserService
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
        DatabaseSuggestionRepository,
    )
    from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
    from tests.utils import InMemoryRevocationStore

    settings = get_settings()

    case_repository = SessionlessCaseRepository()
    share_repository = SessionlessShareRepository()
    team_service = TeamService(SessionlessTeamRepository())
    organization_repository = SessionlessOrganizationRepository()
    sanitizer = DataSanitizer(settings=settings)
    tracer = OpikTracer(settings=settings)

    knowledge_service = KnowledgeService(
        knowledge_ingester=None,
        sanitizer=sanitizer,
        tracer=tracer,
        vector_store=KnowledgeVectorStore(chroma),
        redis_client=None,
        settings=settings,
        llm_provider=None,
        db_session_factory=get_db_session,
        share_repository=share_repository,
    )

    auth_service = AuthService(revocation_store=InMemoryRevocationStore())
    app.state.auth_service = auth_service
    app.state.user_service = UserService(
        user_repo=SessionlessUserRepository(), auth_service=auth_service
    )
    app.state.user_store = DatabaseUserStore(SessionlessUserRepository())
    app.state.case_service = CaseService(
        case_repository=case_repository,
        settings=settings,
        team_service=team_service,
        share_repository=share_repository,
    )
    app.state.team_service = team_service
    app.state.session_service = AuthSessionService(settings=settings)
    app.state.investigation_service = _Tripwire("investigation_service")
    app.state.share_repository = share_repository
    app.state.knowledge_service = knowledge_service
    app.state.organization_repository = organization_repository
    app.state.tenant_provider = MultiTenantProvider(
        organization_repository=organization_repository
    )
    app.state.operator_audit_repository = SessionlessOperatorAuditRepository()
    app.state.operator_grant_repository = SessionlessOperatorGrantRepository()
    app.state.suggestion_service = SuggestionService(
        case_repository=case_repository,
        knowledge_service=knowledge_service,
        sanitizer=sanitizer,
        suggestion_repository=DatabaseSuggestionRepository(),
    )
    app.state.conversion_service = ConversionService(
        llm_router=AsyncMock(),
        settings=settings,
        db_session_factory=get_db_session,
        knowledge_service=knowledge_service,
        share_repository=share_repository,
        team_service=team_service,
    )
    # ``get_case_repository`` reads the repository off ``app.extra`` rather than
    # ``app.state`` — the reports read path is the only consumer. A namespace is
    # the whole of what that accessor uses (``getattr(container, name, None)``);
    # a real DIContainer would drag in the LLM stack for no additional coverage.
    app.extra["di_container"] = SimpleNamespace(
        case_repository=case_repository,
        case_vector_store=None,
        runbook_kb=None,
        llm_provider=None,
    )


@pytest.fixture(scope="module")
def probe_app():
    """The real application, built once, over the limited PostgreSQL role.

    Module-scoped and synchronous on purpose. Building the app is loop-free, and
    the environment it is built under has to be *restored* before any sibling
    module runs — ``test_rls_tenant_isolation.py`` reads ``DATABASE_URL``
    expecting the superuser url, and would silently measure RLS as the wrong
    role if this module leaked the limited one.
    """
    superuser_url = os.environ["DATABASE_URL"]
    saved = {key: os.environ.get(key) for key in _PROBE_ENV_KEYS}

    asyncio.run(_create_limited_role(superuser_url))

    os.environ["DATABASE_URL"] = _limited_url(superuser_url)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"
    os.environ["AUTH_MODE"] = "local"
    os.environ["JWT_SECRET_KEY"] = _JWT_SECRET
    os.environ["SKIP_SERVICE_CHECKS"] = "true"
    os.environ.pop("OAUTH_ENABLED", None)
    # Pinned, not inherited. ``ENVIRONMENT`` decides whether the debug router is
    # mounted, and the route inventory is computed from the app that is actually
    # built — so an ambient ``ENVIRONMENT=production`` would drop
    # ``/debug/cases/{case_id}/causal-graph`` from the live set and the
    # inventory's stale-entry half would fail for a reason that has nothing to
    # do with tenancy. Pinning it also fixes the protection preset, so the
    # module does not inherit a different rate-limit shape per machine.
    os.environ["ENVIRONMENT"] = "development"

    from faultmaven.config.settings import reset_settings
    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.integration._app_rebuild import rebuild_app

    reset_settings()
    reset_engine()
    app = rebuild_app()
    _wire_services(app, _fresh_chroma())

    yield SimpleNamespace(app=app, superuser_url=superuser_url)

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings()
    reset_engine()
    asyncio.run(_drop_limited_role(superuser_url))


@contextmanager
def _as_tenant(org_id: str) -> Iterator[None]:
    """Bind the tenant contextvar the way ``bind_request_org_context`` does.

    Used only by the seeding helpers. Every *probe* binds its tenant the real
    way — by presenting a token — because the binding is part of what is under
    test.
    """
    from faultmaven.config.tenant_context import _current_org_id

    token = _current_org_id.set(org_id)
    try:
        yield
    finally:
        _current_org_id.reset(token)


async def _seed_case_with_content(*, org_id, user_id, title, secret_prefix):
    """Write one case and its child rows through the PRODUCTION writers.

    Everything here goes through ``SessionlessCaseRepository`` under the tenant
    binding, so the rows are stamped and RLS-accepted exactly as a real request
    would leave them. Hand-inserting them as superuser would seed rows the
    application could never have written, and a probe over those proves nothing
    about the deployed system.
    """
    from faultmaven.modules.case.domain.models import (
        Case,
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        UploadedFile,
    )
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportStatus,
        ReportType,
    )
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    repository = SessionlessCaseRepository()
    case_id = f"case_{uuid.uuid4().hex[:12]}"
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
    report_id = str(uuid.uuid4())

    uploaded_file = UploadedFile(
        file_id=file_id,
        filename=f"{secret_prefix}-oncall-rota.log",
        size_bytes=42,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        uploaded_by=user_id,
    )
    evidence = Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="M1",
        summary=f"{secret_prefix}-payroll-dsn",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        collected_by=user_id,
        collected_at_turn=1,
    )
    case = Case(
        case_id=case_id,
        organization_id=org_id,
        user_id=user_id,
        title=title,
        description=f"{secret_prefix}-description",
    )

    with _as_tenant(org_id):
        await repository.save(case)
        await repository.add_message(
            case_id,
            {
                "role": "user",
                "content": f"{secret_prefix}-transcript-line",
                "turn_number": 1,
            },
        )
        # The uploaded file first: ``evidence.source_file_id`` is an FK to it,
        # and the aggregate save writes evidence in the same statement batch.
        await repository.add_uploaded_file(case_id, uploaded_file, org_id)
        case.uploaded_files = [uploaded_file]
        case.evidence = [evidence]
        await repository.save(case)
        await repository.add_report(
            CaseReport(
                report_id=report_id,
                case_id=case_id,
                report_type=ReportType.CLOSURE_SUMMARY,
                title=f"{secret_prefix}-postmortem summary",
                content=f"# {secret_prefix}-postmortem\n\nroot cause: rotated dsn",
                generation_status=ReportStatus.COMPLETED,
                generated_at=datetime.now(UTC).isoformat(),
                generation_time_ms=1,
            )
        )

    return SimpleNamespace(
        case_id=case_id,
        file_id=file_id,
        evidence_id=evidence_id,
        report_id=report_id,
    )


_KB_INSERT = text("""
    INSERT INTO knowledge_items
        (item_id, organization_id, scope, owner_id, title, content, item_type,
         tags, is_published, metadata)
    VALUES (:item_id, :org, :scope, :owner, :title, :content, 'runbook',
            ARRAY['probe']::varchar[], true, '{}'::jsonb)
    """)

_SHARE_INSERT = text("""
    INSERT INTO resource_shares
        (share_id, resource_type, resource_id, scope_type, scope_id,
         organization_id, created_by)
    VALUES (:share_id, :resource_type, :resource_id, 'team', :team_id, :org, :by)
    """)

#: Membership is what ``MultiTenantProvider.get_current_organization`` verifies,
#: and the reports router refuses (403) when it cannot. Seeding it is not
#: convenience: a "tenant user" who is not a member is not the principal the
#: deployment issues tokens to, and probing with one would exercise a refusal
#: path instead of the boundary.
_MEMBERSHIP_INSERT = text("""
    INSERT INTO organization_members (user_id, organization_id, role_id)
    SELECT :u, :o, role_id FROM roles WHERE name = 'admin'
    """)

_TEAM_INSERT = text(
    "INSERT INTO teams (team_id, organization_id, name) VALUES (:t, :o, :n)"
)
_MEMBER_INSERT = text(
    "INSERT INTO team_members (user_id, team_id, team_role) "
    "VALUES (:u, :t, 'member')"
)


async def _seed_tenant_rows(session_factory, tenant) -> None:
    """Knowledge items, team, membership and shares — written as the tenant.

    Written through the limited role under the tenant's binding, so PostgreSQL's
    ``WITH CHECK`` half of the RLS policy validates every ``organization_id``
    stamp. A superuser INSERT would bypass the policy and could seed a row the
    application is not able to produce.
    """
    with _as_tenant(tenant.org_id):
        async with session_factory() as session:
            await session.execute(
                _MEMBERSHIP_INSERT, {"u": tenant.user_id, "o": tenant.org_id}
            )
            for extra_member in tenant.extra_members:
                await session.execute(
                    _MEMBERSHIP_INSERT, {"u": extra_member, "o": tenant.org_id}
                )
            await session.execute(
                _TEAM_INSERT,
                {"t": tenant.team_id, "o": tenant.org_id, "n": f"team-{_RUN}"},
            )
            await session.execute(
                _MEMBER_INSERT, {"u": tenant.user_id, "t": tenant.team_id}
            )
            await session.execute(
                _KB_INSERT,
                {
                    "item_id": tenant.kb_personal_id,
                    "org": tenant.org_id,
                    "scope": "personal",
                    "owner": tenant.user_id,
                    "title": f"{tenant.secret}-runbook-personal",
                    "content": f"{tenant.secret}-runbook-personal body",
                },
            )
            await session.execute(
                _KB_INSERT,
                {
                    "item_id": tenant.kb_team_id,
                    "org": tenant.org_id,
                    "scope": "team",
                    "owner": tenant.user_id,
                    "title": f"{tenant.secret}-runbook-team",
                    "content": f"{tenant.secret}-runbook-team body",
                },
            )
            await session.execute(
                _SHARE_INSERT,
                {
                    "share_id": str(uuid.uuid4()),
                    "resource_type": "knowledge_item",
                    "resource_id": tenant.kb_team_id,
                    "team_id": tenant.team_id,
                    "org": tenant.org_id,
                    "by": tenant.user_id,
                },
            )
            await session.commit()


_CONVERSION_JOB_INSERT = text("""
    INSERT INTO conversion_jobs
        (id, organization_id, user_id, case_id, source_file_id, scope, status,
         source_type)
    VALUES (:id, :org, :user, :case_id, :file_id, 'personal', 'completed', 'case')
    """)

_CONVERSION_DRAFT_INSERT = text("""
    INSERT INTO conversion_drafts
        (id, organization_id, conversion_id, runbook_id, title, file_path, status)
    VALUES (:id, :org, :conversion_id, :runbook_id, :title, :path, 'draft')
    """)

_SUGGESTION_INSERT = text("""
    INSERT INTO knowledge_suggestions
        (suggestion_id, organization_id, case_id, status, suggested_title,
         suggested_content, extracted_by, source_case_title)
    VALUES (:id, :org, :case_id, 'pending_review', :title, :content, :by, :title)
    """)


async def _seed_conversion_and_suggestion(session_factory, tenant) -> None:
    """The conversion job, its draft, and one knowledge suggestion.

    Written as the tenant, through the limited role, for the same reason as
    everything else here: the RLS ``WITH CHECK`` half validates the stamp, so a
    row that lands is a row the application could have written.
    """
    conversion_id = f"conv_{uuid.uuid4().hex[:12]}"
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    suggestion_id = str(uuid.uuid4())

    with _as_tenant(tenant.org_id):
        async with session_factory() as session:
            await session.execute(
                _CONVERSION_JOB_INSERT,
                {
                    "id": conversion_id,
                    "org": tenant.org_id,
                    "user": tenant.user_id,
                    "case_id": tenant.case.case_id,
                    "file_id": tenant.case.file_id,
                },
            )
            await session.execute(
                _CONVERSION_DRAFT_INSERT,
                {
                    "id": draft_id,
                    "org": tenant.org_id,
                    "conversion_id": conversion_id,
                    "runbook_id": f"rb-{uuid.uuid4().hex[:8]}",
                    "title": f"{tenant.secret}-conversion-draft",
                    "path": f"/tmp/{draft_id}.md",
                },
            )
            await session.execute(
                _SUGGESTION_INSERT,
                {
                    "id": suggestion_id,
                    "org": tenant.org_id,
                    "case_id": tenant.case.case_id,
                    "title": f"{tenant.secret}-suggestion",
                    "content": f"{tenant.secret}-suggestion body",
                    "by": tenant.user_id,
                },
            )
            await session.commit()

    tenant.conversion_id = conversion_id
    tenant.draft_id = draft_id
    tenant.suggestion_id = suggestion_id


#: One embedding for every chunk and every query, so cosine similarity excludes
#: nothing and the metadata filter is the ONLY thing that can keep a row out of
#: a KB result set. Lifted from the KB probe for the same reason it exists
#: there: a probe whose negative could be produced by a poor match is not a
#: probe of the filter.
_VEC = [0.5] * 8


async def _fixed_embedding(*_args: Any, **_kwargs: Any) -> list[float]:
    return list(_VEC)


async def _seed_kb_chunks(chroma_store, tenants) -> None:
    """Index each tenant's runbooks into the REAL ChromaDB the app searches.

    Rows go in through ``KnowledgeVectorStore.add_documents``, including its
    ``VectorMetadata`` allowlist, so a metadata key production never stamps
    cannot be smuggled in here. #1168 is the reason this exists at all: the
    vector layer has no tenant dimension, so a SQL-surface pass says nothing
    about retrieval.
    """
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KB_COLLECTION,
    )

    rows = []
    for tenant in tenants:
        for item_id, scope, label in (
            (tenant.kb_personal_id, "personal", "runbook-personal"),
            (tenant.kb_team_id, "personal", "runbook-team"),
        ):
            rows.append(
                {
                    "id": f"{item_id}_chunk_0",
                    "content": (
                        f"# Runbook\n{tenant.secret}-{label}\n"
                        "connection pool exhaustion in production."
                    ),
                    "metadata": {
                        "document_type": "runbook",
                        "scope": scope,
                        "title": f"{tenant.secret}-{label}",
                        "parent_document_id": item_id,
                        "chunk_index": 0,
                        "total_chunks": 1,
                        "owner_id": tenant.user_id,
                        "domain": "database",
                        "service": "postgres",
                    },
                }
            )
    await chroma_store.add_documents(
        rows, embeddings=[list(_VEC) for _ in rows], collection_name=KB_COLLECTION
    )


@pytest.fixture
async def world(probe_app):
    """Two tenants, each holding identifiable data, and a client for each.

    Function-scoped and freshly identified on every test. Two reasons, both
    learned the hard way in the sibling KB probe: an engine created in one
    event loop cannot be used from another (asyncpg binds its pool to the loop
    that made it), and a corpus that accumulates across tests makes every
    ``got == expected`` assertion depend on collection order.
    """
    import httpx

    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_session_factory,
        reset_engine,
    )
    from tests.utils import forge_access_token, seed_organizations, seed_users

    app = probe_app.app
    reset_engine()

    superuser_engine = create_async_engine(probe_app.superuser_url, future=True)
    org_a = f"org_a_{uuid.uuid4().hex[:8]}"
    org_b = f"org_b_{uuid.uuid4().hex[:8]}"
    user_a = f"user_a_{uuid.uuid4().hex[:8]}"
    user_b = f"user_b_{uuid.uuid4().hex[:8]}"
    operator_a = f"user_op_{uuid.uuid4().hex[:8]}"

    superuser_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with superuser_maker() as session:
        await seed_organizations(session, [org_a, org_b])
        await seed_users(session, [user_a, user_b, operator_a])
        # ``seed_users`` derives ``<user_id>@test.local``, and ``.local`` is a
        # reserved special-use name that ``email-validator`` refuses. The admin
        # user routes hydrate a ``User`` model on the way out, so a seeded row
        # would answer 500 — and a 500 is not a refusal, it is an assertion
        # that never got to run. Re-stamped to a domain the model accepts.
        await session.execute(
            text(
                "UPDATE users SET email = user_id || '@example.com' "
                "WHERE user_id IN (:a, :b, :c)"
            ),
            {"a": user_a, "b": user_b, "c": operator_a},
        )
        await session.commit()

    tenant_a = SimpleNamespace(
        org_id=org_a,
        user_id=user_a,
        secret=SECRET_A,
        team_id=f"team_a_{uuid.uuid4().hex[:8]}",
        kb_personal_id=f"kb_a_{uuid.uuid4().hex[:12]}",
        kb_team_id=f"kb_at_{uuid.uuid4().hex[:12]}",
        extra_members=[operator_a],
    )
    tenant_b = SimpleNamespace(
        org_id=org_b,
        user_id=user_b,
        secret=SECRET_B,
        team_id=f"team_b_{uuid.uuid4().hex[:8]}",
        kb_personal_id=f"kb_b_{uuid.uuid4().hex[:12]}",
        kb_team_id=f"kb_bt_{uuid.uuid4().hex[:12]}",
        extra_members=[],
    )

    session_factory = get_session_factory()
    await _seed_tenant_rows(session_factory, tenant_a)
    await _seed_tenant_rows(session_factory, tenant_b)

    tenant_b.case = await _seed_case_with_content(
        org_id=org_b, user_id=user_b, title=SECRET_B_TITLE, secret_prefix=SECRET_B
    )
    tenant_a.case = await _seed_case_with_content(
        org_id=org_a,
        user_id=user_a,
        title=f"{SECRET_A}-own-incident",
        secret_prefix=SECRET_A,
    )

    await _seed_conversion_and_suggestion(session_factory, tenant_b)
    await _seed_conversion_and_suggestion(session_factory, tenant_a)

    # A FRESH ChromaDB per test, not the one the module fixture built. The
    # collection is dropped and re-seeded, so the corpus is a function of this
    # test rather than of everything that ran before it — the ordering
    # dependence the sibling KB probe records as its own first defect.
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )

    vector_store = KnowledgeVectorStore(_fresh_chroma())
    app.state.knowledge_service._vector_store = vector_store
    await _seed_kb_chunks(vector_store, (tenant_a, tenant_b))

    auth_service = app.state.auth_service
    token_a = forge_access_token(
        auth_service,
        user_id=user_a,
        organization_id=org_a,
        email=f"{user_a}@example.com",
        roles=["user", "admin"],
    )
    token_b = forge_access_token(
        auth_service,
        user_id=user_b,
        organization_id=org_b,
        email=f"{user_b}@example.com",
        roles=["user", "admin"],
    )
    # A platform operator whose *request* still binds tenant A. The cross-tenant
    # role is the strongest principal the deployment mints, and the question
    # this file asks of it is whether the role alone reaches B's rows.
    token_operator_a = forge_access_token(
        auth_service,
        user_id=operator_a,
        organization_id=org_a,
        email=f"{operator_a}@example.com",
        roles=["user", "platform_admin"],
    )

    # The runbook-publish control writes a markdown file under the KB root.
    # Snapshotted here and diffed at teardown, so a run leaves the filesystem as
    # it found it — the same discipline the row cleanup below applies, and the
    # path is resolved from settings rather than assumed.
    from faultmaven.utils.runbook_id import knowledge_root

    kb_root = knowledge_root()
    kb_entries_before = set(kb_root.iterdir()) if kb_root.exists() else set()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://probe", timeout=60.0
    ) as http:
        yield SimpleNamespace(
            app=app,
            http=http,
            a=tenant_a,
            b=tenant_b,
            token_a=token_a,
            token_b=token_b,
            token_operator_a=token_operator_a,
            superuser_engine=superuser_engine,
            superuser_maker=superuser_maker,
        )

    await close_database()
    if kb_root.exists():
        for entry in set(kb_root.iterdir()) - kb_entries_before:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    async with superuser_engine.begin() as conn:
        for conversion_id in (tenant_a.conversion_id, tenant_b.conversion_id):
            await conn.execute(
                text("DELETE FROM conversion_drafts WHERE conversion_id = :c"),
                {"c": conversion_id},
            )
            await conn.execute(
                text("DELETE FROM conversion_jobs WHERE id = :c"), {"c": conversion_id}
            )
        for case_id in (tenant_a.case.case_id, tenant_b.case.case_id):
            await conn.execute(
                text("DELETE FROM knowledge_suggestions WHERE case_id = :c"),
                {"c": case_id},
            )
            await conn.execute(
                text("DELETE FROM reports WHERE case_id = :c"), {"c": case_id}
            )
            await conn.execute(
                text("DELETE FROM evidence WHERE case_id = :c"), {"c": case_id}
            )
            await conn.execute(
                text("DELETE FROM uploaded_files WHERE case_id = :c"), {"c": case_id}
            )
            await conn.execute(
                text("DELETE FROM case_messages WHERE case_id = :c"), {"c": case_id}
            )
            await conn.execute(
                text("DELETE FROM cases WHERE case_id = :c"), {"c": case_id}
            )
        await conn.execute(
            text("DELETE FROM knowledge_items WHERE organization_id IN (:a, :b)"),
            {"a": org_a, "b": org_b},
        )
        # organizations cascades teams, team_members, organization_members and
        # resource_shares; knowledge_items is deleted explicitly above because
        # its org column carries no cascading FK (the global tier is org-free).
        await conn.execute(
            text("DELETE FROM organizations WHERE organization_id IN (:a, :b)"),
            {"a": org_a, "b": org_b},
        )
        await conn.execute(
            text("DELETE FROM users WHERE user_id IN (:a, :b, :c)"),
            {"a": user_a, "b": user_b, "c": operator_a},
        )
    await superuser_engine.dispose()


# =============================================================================
# The two moves every test makes: B finds it, A must not
# =============================================================================


async def _call(world, token: str, method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    return await world.http.request(method, path, headers=headers, **kwargs)


async def as_b(world, method: str, path: str, **kwargs):
    """The positive control: the same call, by the tenant who owns the row."""
    return await _call(world, world.token_b, method, path, **kwargs)


async def as_a(world, method: str, path: str, **kwargs):
    """The attack: tenant A, holding a valid token for A, reaching for B."""
    return await _call(world, world.token_a, method, path, **kwargs)


def assert_no_b_content(response, surface: str) -> None:
    """No marker of B's may appear in a body A received — at ANY status.

    Applied to refusals as well as successes on purpose. A 403 whose ``detail``
    echoes the case title, or a 500 whose traceback carries the row, has already
    leaked; reading only the status code would call both of those a pass.
    """
    body = response.text
    leaked = [marker for marker in B_MARKERS if marker in body]
    assert not leaked, (
        f"{surface}: tenant A received tenant B's content {leaked} "
        f"(status {response.status_code}): {body[:400]}"
    )


def assert_refused(response, surface: str) -> None:
    """A must be refused, and refused without confirming the row exists."""
    assert response.status_code in REFUSED, (
        f"{surface}: expected a refusal, got {response.status_code}: "
        f"{response.text[:400]}"
    )
    assert_no_b_content(response, surface)


def _ids(payload: Any, key: str) -> set[str]:
    """Every value of ``key`` anywhere in a JSON document.

    Walks the whole structure rather than a known path: the shapes differ per
    surface (``{"cases": [...]}, [...], {"results": {...}}``) and a probe that
    hard-codes one of them stops counting the moment a response model changes,
    which is silent under a "the list is empty" assertion.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get(key)
            if isinstance(value, str):
                found.add(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return found


# =============================================================================
# Surface 1 — cases: list, detail, transcript, search, analytics
# =============================================================================


async def test_the_case_list_shows_a_only_a_and_b_only_b(world):
    """The collection read. Both directions, because "empty" is not isolation.

    A's list must contain A's case and not B's; B's must contain B's and not
    A's. Asserting only "A cannot see B" would pass against a deployment whose
    case table is unreadable to everyone.
    """
    mine = await as_a(world, "GET", "/api/v1/cases")
    theirs = await as_b(world, "GET", "/api/v1/cases")

    assert theirs.status_code == 200
    assert world.b.case.case_id in _ids(theirs.json(), "case_id"), (
        "the positive control failed: B cannot see B's own case, so every "
        "negative assertion in this module is vacuous"
    )
    assert mine.status_code == 200
    assert world.a.case.case_id in _ids(mine.json(), "case_id")
    assert world.b.case.case_id not in _ids(mine.json(), "case_id")
    assert_no_b_content(mine, "GET /api/v1/cases")


async def test_a_case_detail_read_by_the_other_tenant_is_refused(world):
    """The id-addressed read, which is where a missing predicate shows up.

    The list can look isolated while ``get(case_id)`` has no tenant clause at
    all — the list filters by owner, the detail resolves by primary key.
    """
    control = await as_b(world, "GET", f"/api/v1/cases/{world.b.case.case_id}")
    attack = await as_a(world, "GET", f"/api/v1/cases/{world.b.case.case_id}")

    assert control.status_code == 200, "control: B cannot read B's own case"
    assert SECRET_B_TITLE in control.text
    assert_refused(attack, "GET /api/v1/cases/{case_id}")


async def test_the_case_ui_projection_is_refused_to_the_other_tenant(world):
    """``/ui`` is a second, independently written read of the same aggregate."""
    control = await as_b(world, "GET", f"/api/v1/cases/{world.b.case.case_id}/ui")
    attack = await as_a(world, "GET", f"/api/v1/cases/{world.b.case.case_id}/ui")

    assert control.status_code == 200, "control: B cannot read B's own case UI"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/ui")


async def test_the_transcript_is_refused_to_the_other_tenant(world):
    """The transcript is the highest-value content on a case."""
    path = f"/api/v1/cases/{world.b.case.case_id}/messages"
    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert SECRET_B_TRANSCRIPT in control.text, "control: B's transcript is missing"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/messages")


async def test_case_search_does_not_match_the_other_tenants_cases(world):
    """``POST /cases/search`` — a query aimed straight at B's marker string.

    Search is the surface where a missing predicate is least visible: the caller
    supplies the term, so a leak looks like a good result.
    """
    body = {"query": SECRET_B_TITLE, "limit": 50}
    control = await as_b(world, "POST", "/api/v1/cases/search", json=body)
    attack = await as_a(world, "POST", "/api/v1/cases/search", json=body)

    assert control.status_code == 200
    assert world.b.case.case_id in _ids(control.json(), "case_id"), (
        "control: B's own search does not find B's case, so A finding nothing "
        "proves nothing"
    )
    assert attack.status_code == 200
    assert world.b.case.case_id not in _ids(attack.json(), "case_id")
    assert_no_b_content(attack, "POST /api/v1/cases/search")


async def test_a_search_body_naming_the_other_tenant_does_not_widen_the_scope(world):
    """The body carries ``organization_id``/``user_id``. Are they honoured?

    ``CaseSearchRequest`` declares tenant-shaped fields, so the caller can name
    B in the request. The tenant must come from the verified claim; a filter
    field that *narrows* is harmless, one that *selects* is the boundary.
    """
    attack = await as_a(
        world,
        "POST",
        "/api/v1/cases/search",
        json={
            "query": SECRET_B_TITLE,
            "organization_id": world.b.org_id,
            "user_id": world.b.user_id,
            "limit": 50,
        },
    )

    assert attack.status_code in (200, 403, 422)
    assert world.b.case.case_id not in _ids(
        attack.json() if attack.status_code == 200 else [], "case_id"
    )
    assert_no_b_content(attack, "POST /api/v1/cases/search (org_id injected)")


async def test_case_analytics_are_refused_to_the_other_tenant(world):
    """Counts are inference: "how many messages does that case have" is content."""
    path = f"/api/v1/cases/{world.b.case.case_id}/analytics"
    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert control.json()["message_count"] >= 1, "control: B's analytics are empty"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/analytics")


# =============================================================================
# Surface 2 — evidence and uploaded files
# =============================================================================


async def test_evidence_is_refused_to_the_other_tenant(world):
    """Evidence carries the verbatim system output — the sharpest content."""
    listing = f"/api/v1/cases/{world.b.case.case_id}/evidence"
    item = f"{listing}/{world.b.case.evidence_id}"

    control_list = await as_b(world, "GET", listing)
    control_item = await as_b(world, "GET", item)
    assert control_list.status_code == 200
    assert SECRET_B_EVIDENCE in control_list.text, "control: B's evidence is missing"
    assert control_item.status_code == 200
    assert SECRET_B_EVIDENCE in control_item.text

    assert_refused(await as_a(world, "GET", listing), "GET .../evidence")
    assert_refused(await as_a(world, "GET", item), "GET .../evidence/{evidence_id}")


async def test_uploaded_files_are_refused_to_the_other_tenant(world):
    """Filenames alone are content: they name systems, tenants and people."""
    listing = f"/api/v1/cases/{world.b.case.case_id}/uploaded-files"
    item = f"{listing}/{world.b.case.file_id}"

    control_list = await as_b(world, "GET", listing)
    control_item = await as_b(world, "GET", item)
    assert control_list.status_code == 200
    assert SECRET_B_FILE in control_list.text, "control: B's uploaded file is missing"
    assert control_item.status_code == 200

    assert_refused(await as_a(world, "GET", listing), "GET .../uploaded-files")
    assert_refused(await as_a(world, "GET", item), "GET .../uploaded-files/{file_id}")


async def test_the_case_data_surface_is_refused_to_the_other_tenant(world):
    """``/cases/{id}/data`` — read AND delete, both gated on the same case.

    The control here proves only that B's request passes the case gate: the
    id-addressed read returns a placeholder payload rather than the stored file,
    so "B sees the content" is not assertable. What *is* assertable, and is the
    property at issue, is that A does not get past the gate at all — including
    on the DELETE, which is where a missing predicate destroys another tenant's
    row rather than merely reading it.
    """
    listing = f"/api/v1/cases/{world.b.case.case_id}/data"
    item = f"{listing}/{world.b.case.file_id}"

    control = await as_b(world, "GET", listing)
    assert control.status_code == 200, "control: B cannot reach its own case data"

    assert_refused(await as_a(world, "GET", listing), "GET .../data")
    assert_refused(await as_a(world, "GET", item), "GET .../data/{data_id}")
    assert_refused(await as_a(world, "DELETE", item), "DELETE .../data/{data_id}")


# =============================================================================
# Surface 3 — reports and conversion drafts
# =============================================================================


async def test_case_reports_are_refused_to_the_other_tenant(world):
    """Both report surfaces: the case-nested one and the report-id one."""
    nested = f"/api/v1/cases/{world.b.case.case_id}/reports"
    download = f"{nested}/{world.b.case.report_id}/download"
    by_case = f"/api/v1/reports/case/{world.b.case.case_id}"
    by_id = f"/api/v1/reports/{world.b.case.report_id}"

    for path in (nested, download, by_case, by_id, f"{by_id}/versions"):
        control = await as_b(world, "GET", path)
        assert control.status_code == 200, f"control: B cannot read {path}"
        assert SECRET_B_REPORT in control.text, f"control: {path} carried no marker"
        assert_refused(await as_a(world, "GET", path), f"GET {path}")


async def test_a_report_cannot_be_edited_or_deleted_by_the_other_tenant(world):
    """Mutation, not just reading. The destructive half of the same surface.

    The DELETE is asserted against the DATABASE afterwards, not against the
    response: a route that answers 404 and deletes anyway would satisfy a
    status-code assertion while destroying the row.
    """
    by_id = f"/api/v1/reports/{world.b.case.report_id}"

    assert_refused(
        await as_a(world, "PUT", by_id, json={"content": "PWNED"}), f"PUT {by_id}"
    )
    assert_refused(await as_a(world, "DELETE", by_id), f"DELETE {by_id}")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title FROM reports WHERE report_id = :r"),
                {"r": world.b.case.report_id},
            )
        ).first()
    assert row is not None, "A's refused DELETE removed B's report anyway"
    assert SECRET_B_REPORT in row[0], "A's refused PUT rewrote B's report anyway"


async def test_report_recommendations_do_not_confirm_the_other_tenants_case(world):
    """A refusal must not distinguish "not yours" from "wrong state".

    B's own call gets 400 (the case is not RESOLVED) — it reached the state
    check. A's gets 404, having never resolved the case at all. The pair is the
    control: two different refusals, and only one of them tells the caller
    anything about the case.
    """
    path = f"/api/v1/cases/{world.b.case.case_id}/report-recommendations"

    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 400, (
        "control: B's own request no longer reaches the state check, so A's 404 "
        "no longer distinguishes anything"
    )
    assert attack.status_code == 404
    assert_no_b_content(attack, path)


async def test_conversion_jobs_and_drafts_are_refused_to_the_other_tenant(world):
    """The runbook-conversion surface: job listing, job detail, by-case, drafts."""
    listing = "/api/v1/knowledge/conversions"
    detail = f"{listing}/{world.b.conversion_id}"
    by_case = f"{listing}/by-case/{world.b.case.case_id}"
    drafts = "/api/v1/knowledge/drafts"

    control_list = await as_b(world, "GET", listing)
    assert control_list.status_code == 200
    assert world.b.conversion_id in _ids(control_list.json(), "conversion_id") | _ids(
        control_list.json(), "id"
    ), "control: B cannot list B's own conversion"

    control_drafts = await as_b(world, "GET", drafts)
    assert control_drafts.status_code == 200
    assert SECRET_B_DRAFT in control_drafts.text, "control: B's draft is missing"

    attack_list = await as_a(world, "GET", listing)
    assert attack_list.status_code == 200
    assert world.b.conversion_id not in _ids(
        attack_list.json(), "conversion_id"
    ) | _ids(attack_list.json(), "id")
    assert_no_b_content(attack_list, f"GET {listing}")

    attack_drafts = await as_a(world, "GET", drafts)
    assert attack_drafts.status_code == 200
    assert_no_b_content(attack_drafts, f"GET {drafts}")

    assert_refused(await as_a(world, "GET", detail), f"GET {detail}")
    assert_refused(await as_a(world, "GET", by_case), f"GET {by_case}")


async def test_a_conversion_draft_cannot_be_edited_or_discarded_across_tenants(world):
    """The draft mutations, checked against the row rather than the status."""
    draft = (
        f"/api/v1/knowledge/conversions/{world.b.conversion_id}"
        f"/drafts/{world.b.draft_id}"
    )

    assert_refused(
        await as_a(world, "PUT", draft, json={"content": "PWNED"}), f"PUT {draft}"
    )
    assert_refused(await as_a(world, "DELETE", draft), f"DELETE {draft}")
    assert_refused(await as_a(world, "POST", f"{draft}/verify"), f"POST {draft}/verify")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title, status FROM conversion_drafts WHERE id = :d"),
                {"d": world.b.draft_id},
            )
        ).first()
    assert row is not None, "A's refused DELETE discarded B's draft anyway"
    assert SECRET_B_DRAFT in row[0]
    assert row[1] == "draft", "A's refused verify promoted B's draft anyway"


# =============================================================================
# Surface 4 — knowledge items, in every scope and through `resource_shares`
# =============================================================================


async def test_the_knowledge_inventory_shows_each_tenant_only_its_own(world):
    """Both scopes at once: the personal item and the team-shared one.

    The team item is the interesting half. It is reachable to its owner through
    the ``resource_shares`` arm, and that arm is an **allowlist the vector layer
    takes on trust** (#1168) — the only thing keeping another tenant's ids out
    of it is one SQL ``WHERE`` in the share lookup.
    """
    mine = await as_a(world, "GET", "/api/v1/knowledge/documents")
    theirs = await as_b(world, "GET", "/api/v1/knowledge/documents")

    assert theirs.status_code == 200
    listed_by_b = _ids(theirs.json(), "document_id")
    assert {world.b.kb_personal_id, world.b.kb_team_id} <= listed_by_b, (
        "control: B cannot list B's own knowledge items, so A listing none "
        "proves nothing"
    )

    assert mine.status_code == 200
    listed_by_a = _ids(mine.json(), "document_id")
    assert {world.a.kb_personal_id, world.a.kb_team_id} <= listed_by_a
    assert listed_by_a & {world.b.kb_personal_id, world.b.kb_team_id} == set()
    assert_no_b_content(mine, "GET /api/v1/knowledge/documents")


async def test_a_knowledge_item_is_not_readable_by_id_across_tenants(world):
    """404, identical to an absent id: the refusal must not confirm existence."""
    for item_id in (world.b.kb_personal_id, world.b.kb_team_id):
        for suffix in ("", "/snippet"):
            path = f"/api/v1/knowledge/documents/{item_id}{suffix}"
            control = await as_b(world, "GET", path)
            attack = await as_a(world, "GET", path)

            assert control.status_code == 200, f"control: B cannot read {path}"
            assert attack.status_code == 404, (
                f"{path}: expected 404 (indistinguishable from absent), got "
                f"{attack.status_code}"
            )
            assert_no_b_content(attack, f"GET {path}")


async def test_a_knowledge_item_cannot_be_edited_or_deleted_across_tenants(world):
    """The write half, checked against the row.

    Bulk delete is included because it takes a **list of ids** and reports
    per-id outcomes: a route that deletes what it can and reports the rest as
    "not found" would pass an aggregate status assertion.
    """
    item = f"/api/v1/knowledge/documents/{world.b.kb_team_id}"

    assert_refused(
        await as_a(world, "PUT", item, json={"title": "PWNED", "content": "x"}),
        f"PUT {item}",
    )
    assert_refused(await as_a(world, "DELETE", item), f"DELETE {item}")

    bulk_delete = await as_a(
        world,
        "POST",
        "/api/v1/knowledge/documents/bulk-delete",
        json={"document_ids": [world.b.kb_personal_id, world.b.kb_team_id]},
    )
    bulk_update = await as_a(
        world,
        "POST",
        "/api/v1/knowledge/documents/bulk-update",
        json={
            "document_ids": [world.b.kb_personal_id, world.b.kb_team_id],
            "updates": {"tags": ["pwned"]},
        },
    )
    assert bulk_delete.status_code in (200, 207, 403, 404)
    assert (
        bulk_delete.json().get("deleted_count", 0) == 0
    ), "bulk-delete removed another tenant's knowledge items"
    assert (
        bulk_update.json().get("updated_count", 0) == 0
    ), "bulk-update rewrote another tenant's knowledge items"
    assert_no_b_content(bulk_delete, "POST /knowledge/documents/bulk-delete")
    assert_no_b_content(bulk_update, "POST /knowledge/documents/bulk-update")

    async with world.superuser_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT item_id, title, tags FROM knowledge_items "
                    "WHERE item_id IN (:p, :t)"
                ),
                {"p": world.b.kb_personal_id, "t": world.b.kb_team_id},
            )
        ).all()
    assert len(rows) == 2, "a refused write removed one of B's knowledge items"
    for _item_id, title, tags in rows:
        assert SECRET_B in title, "a refused write rewrote B's title"
        assert "pwned" not in (tags or []), "a refused bulk-update rewrote B's tags"


async def test_full_text_knowledge_search_does_not_reach_the_other_tenant(world):
    """The keyword arm, queried with B's marker string.

    The query itself is B's marker, so it is echoed in the response — which is
    exactly why this case asserts on the returned **document ids** rather than
    scanning the body. A marker scan here would fail on the echo and pass on a
    leak that came back under a different title.
    """
    body = {"query": SECRET_B_KB_TEAM, "limit": 50}
    control = await as_b(world, "POST", "/api/v1/knowledge/documents/search", json=body)
    attack = await as_a(world, "POST", "/api/v1/knowledge/documents/search", json=body)

    assert control.status_code == 200
    assert world.b.kb_team_id in _ids(
        control.json(), "document_id"
    ), "control: B's own full-text search does not find B's runbook"
    assert attack.status_code == 200
    assert (
        _ids(attack.json(), "document_id")
        & {
            world.b.kb_personal_id,
            world.b.kb_team_id,
        }
        == set()
    )


async def test_semantic_kb_retrieval_does_not_reach_the_other_tenant(world):
    """The **vector** surface — the one #1168 says is derived, not enforced.

    ChromaDB carries no tenant dimension, so this cannot be waved through on the
    strength of the SQL surfaces above. Every chunk and every query is given the
    SAME embedding, so cosine similarity excludes nothing and the metadata
    filter is the only thing that can keep a row out of the result set. If the
    filter stopped being derived from the caller's own identifiers, this is the
    case that would notice.
    """
    from faultmaven.infrastructure.knowledge import knowledge_vector_store as kvs

    body = {"query": "connection pool exhaustion in production", "limit": 50}
    with patch.object(kvs, "embed_query_or_raise", new=_fixed_embedding):
        control = await as_b(world, "POST", "/api/v1/knowledge/search", json=body)
        attack = await as_a(world, "POST", "/api/v1/knowledge/search", json=body)

    assert control.status_code == 200
    assert (
        "error" not in control.json()
    ), f"control: B's semantic search failed outright: {control.text[:300]}"
    assert SECRET_B_KB_PERSONAL in control.text or SECRET_B_KB_TEAM in control.text, (
        "control: the vector search found none of B's chunks even with a "
        "constant embedding, so A finding none proves nothing"
    )

    assert attack.status_code == 200
    assert_no_b_content(attack, "POST /api/v1/knowledge/search")


async def test_knowledge_suggestions_are_scoped_to_the_operators_own_tenant(world):
    """The review inbox. The operator role says WHAT you may do, not WHOSE.

    Two principals are tried: a tenant admin in A (who must be refused outright,
    the route being platform-only) and a **platform operator** whose request
    binds tenant A. The second is the one that matters — a cross-tenant role on
    a tenant-scoped read is precisely where "admin" gets confused with "any
    tenant".
    """
    path = "/api/v1/knowledge/suggestions"

    tenant_admin = await as_a(world, "GET", path)
    assert tenant_admin.status_code == 403
    assert_no_b_content(tenant_admin, f"GET {path} (tenant admin)")

    operator = await _call(world, world.token_operator_a, "GET", path)
    assert operator.status_code == 200
    assert world.b.suggestion_id not in _ids(operator.json(), "suggestion_id")
    assert_no_b_content(operator, f"GET {path} (platform operator bound to A)")

    # Bodies are filled in so each call passes its own validation and actually
    # reaches the tenant resolution. A 400 for a missing field would "refuse"
    # the attack without ever consulting the boundary.
    id_addressed = (
        ("GET", "", None),
        ("PUT", "", {"suggested_title": "PWNED", "suggested_content": "PWNED"}),
        ("POST", "/approve", {}),
        ("POST", "/reject", {"rejection_reason": "probe"}),
        ("POST", "/remediate-pii", {}),
    )
    for method, suffix, json_body in id_addressed:
        target = f"{path}/{world.b.suggestion_id}{suffix}"
        response = await _call(
            world, world.token_operator_a, method, target, json=json_body
        )
        assert response.status_code in REFUSED, (
            f"{method} {target}: a platform operator bound to A reached B's "
            f"suggestion ({response.status_code}): {response.text[:300]}"
        )
        assert_no_b_content(response, f"{method} {target}")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status FROM knowledge_suggestions "
                    "WHERE suggestion_id = :s"
                ),
                {"s": world.b.suggestion_id},
            )
        ).first()
    assert (
        row is not None and row[0] == "pending_review"
    ), "a refused approve/reject changed B's suggestion anyway"


# =============================================================================
# Surface 5 — admin and break-glass
# =============================================================================
#
# Two different principals are needed here, and conflating them is the whole
# risk. ``Role.ADMIN`` is an ORGANISATION role — a tenant's own administrator,
# who must reach nothing outside their tenant. ``platform_admin`` is the
# deployment-wide operator role, and ADR-012 D9 gives it a *deliberate,
# audited* path to tenant content through a break-glass grant. So the invariant
# on these routes is not "nobody sees B": it is that a tenant admin never does,
# and that an operator does so only through a grant naming exactly that case,
# with a row in the append-only audit trail to show for it.


async def test_a_tenant_admin_reaches_no_admin_route_at_all(world):
    """``roles: ["user", "admin"]`` is an org role. It buys nothing here."""
    routes = [
        ("GET", "/api/v1/admin/cases", None),
        ("GET", f"/api/v1/admin/cases/{world.b.case.case_id}", None),
        ("GET", f"/api/v1/admin/cases/{world.b.case.case_id}/messages", None),
        ("GET", "/api/v1/admin/grants", None),
        (
            "POST",
            "/api/v1/admin/grants",
            {
                "case_id": world.b.case.case_id,
                "organization_id": world.b.org_id,
                "reason": "probe: a tenant admin asking for another tenant's case",
                "duration_minutes": 30,
            },
        ),
        ("GET", "/api/v1/admin/audit/operator-access", None),
        ("GET", "/api/v1/admin/users", None),
        ("GET", f"/api/v1/admin/users/{world.b.user_id}", None),
    ]
    for method, path, body in routes:
        response = await as_a(world, method, path, json=body)
        assert response.status_code == 403, (
            f"{method} {path}: an organization admin was admitted to a "
            f"platform route ({response.status_code}): {response.text[:200]}"
        )
        assert_no_b_content(response, f"{method} {path}")


async def test_the_cross_tenant_case_listing_is_refused_under_multi_tenant(world):
    """``/admin/cases`` refuses rather than serving an RLS-truncated answer.

    The failure this guards is not a leak but its mirror: a list that claims to
    span every tenant while RLS silently scopes it to the operator's own. An
    operator triaging "which tenant is stuck" would be misled precisely when the
    endpoint matters.
    """
    response = await _call(world, world.token_operator_a, "GET", "/api/v1/admin/cases")

    assert response.status_code == 403
    assert world.b.case.case_id not in response.text
    assert_no_b_content(response, "GET /api/v1/admin/cases")


async def test_an_operator_without_a_grant_cannot_open_another_tenants_case(world):
    """No grant, no content — and the refusal must not confirm the case exists.

    A 403 that reads "you need a grant for THIS case" is the same text whether
    the case exists or not; what would leak is a 404 for absent ids and a 403
    for present ones. Both ids are tried here for exactly that reason.
    """
    real = f"/api/v1/admin/cases/{world.b.case.case_id}"
    absent = f"/api/v1/admin/cases/case_{'0' * 12}"

    for path in (real, f"{real}/messages", absent, f"{absent}/messages"):
        response = await _call(world, world.token_operator_a, "GET", path)
        assert response.status_code == 403, (
            f"{path}: content was served without a live break-glass grant "
            f"({response.status_code})"
        )
        assert_no_b_content(response, f"GET {path}")


async def test_a_break_glass_grant_unlocks_exactly_the_case_it_names(world):
    """The audited escape hatch — and its bound.

    ADR-012 D9 *intends* a platform operator to reach tenant content through a
    grant, so "the operator read B's case" is not the finding here. The finding
    would be a grant that reaches further than the case it names: this asserts
    the granted case opens, a SECOND case in the SAME organization stays shut,
    and the access is recorded in the append-only audit trail.
    """
    second = await _seed_case_with_content(
        org_id=world.b.org_id,
        user_id=world.b.user_id,
        title=f"{SECRET_B}-second-case",
        secret_prefix=f"{SECRET_B}-second",
    )
    try:
        minted = await _call(
            world,
            world.token_operator_a,
            "POST",
            "/api/v1/admin/grants",
            json={
                "case_id": world.b.case.case_id,
                "organization_id": world.b.org_id,
                "reason": "probe: exercising the audited break-glass path",
                "duration_minutes": 30,
            },
        )
        assert minted.status_code == 201, (
            f"control: the grant could not be minted, so the bound this case "
            f"asserts is untested ({minted.status_code}): {minted.text[:300]}"
        )

        granted = await _call(
            world,
            world.token_operator_a,
            "GET",
            f"/api/v1/admin/cases/{world.b.case.case_id}",
        )
        assert granted.status_code == 200, (
            "control: a live grant did not open the case it names, so the "
            "refusal below cannot be attributed to scoping"
        )
        assert SECRET_B_TITLE in granted.text

        other = await _call(
            world,
            world.token_operator_a,
            "GET",
            f"/api/v1/admin/cases/{second.case_id}",
        )
        assert other.status_code == 403, (
            "a break-glass grant for one case opened a DIFFERENT case in the "
            f"same organization ({other.status_code}): {other.text[:300]}"
        )
        assert f"{SECRET_B}-second-case" not in other.text

        async with world.superuser_engine.begin() as conn:
            trail = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM operator_access_audit "
                        "WHERE target_case_id = :c"
                    ),
                    {"c": world.b.case.case_id},
                )
            ).scalar()
        assert trail and trail >= 1, (
            "the break-glass read left no row in operator_access_audit; the "
            "escape hatch is only acceptable because it is recorded"
        )
    finally:
        async with world.superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM reports WHERE case_id = :c"), {"c": second.case_id}
            )
            await conn.execute(
                text("DELETE FROM evidence WHERE case_id = :c"), {"c": second.case_id}
            )
            await conn.execute(
                text("DELETE FROM uploaded_files WHERE case_id = :c"),
                {"c": second.case_id},
            )
            await conn.execute(
                text("DELETE FROM case_messages WHERE case_id = :c"),
                {"c": second.case_id},
            )
            await conn.execute(
                text("DELETE FROM cases WHERE case_id = :c"), {"c": second.case_id}
            )


# =============================================================================
# Surface 5b — FINDING: the operator user-administration surface is not
# tenant-confined, and unlike the case surface it is neither granted nor audited
# =============================================================================
#
# These two cases assert what the code DOES, not what the invariant wants, and
# they are written that way deliberately — the same discipline the KB probe
# applies to its F1/F2 findings. An executable description goes red when someone
# fixes it, which is a prompt to update this file; a comment would not.
#
# Scope, stated precisely so this is not over-read: an ORGANISATION admin is
# refused every one of these routes (``test_a_tenant_admin_reaches_no_admin_
# route_at_all`` covers that), so this is not "tenant A reads tenant B". It is
# the deployment operator, and the point is the INCONSISTENCY: on case content
# the same role must hold a live break-glass grant naming the case and every
# read lands in ``operator_access_audit`` (ADR-012 D9); on user identity and
# user administration it needs neither, and ``/admin/cases`` even refuses a
# cross-tenant LISTING under multi for a reason that applies here verbatim.
#
# The route docstrings claim the confinement the code does not implement:
# ``get_user_details`` says "Admin can only view users in their own
# organization" and documents a 403 for "user belongs to different
# organization". No such check exists. ``list_users`` takes no organization
# filter at all and stamps ``organization_id=current_user.organization_id`` onto
# every row it returns, so another tenant's user is reported as belonging to the
# caller's organization.
#
# Filed as FaultMaven/faultmaven#1318.


async def test_finding_an_operator_reads_another_tenants_user_without_a_grant(world):
    """FINDING: ``GET /admin/users/{user_id}`` crosses the tenant boundary.

    Red when fixed. If this starts failing, the fix landed — make it the
    assertion the invariant wants (``status_code in REFUSED``) and drop the
    finding note above.
    """
    response = await _call(
        world, world.token_operator_a, "GET", f"/api/v1/admin/users/{world.b.user_id}"
    )

    assert response.status_code == 200, (
        "the operator user read is now refused — #1318 appears fixed; turn "
        "this case into the invariant assertion"
    )
    body = response.json()
    assert body["email"] == f"{world.b.user_id}@example.com"
    assert body["organization_id"] != world.b.org_id, (
        "the response now reports the target's real organization; it used to "
        "report the CALLER's, which is the mislabelling half of #1318"
    )

    async with world.superuser_engine.begin() as conn:
        trail = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM operator_access_audit "
                    "WHERE target_organization_id = :o"
                ),
                {"o": world.b.org_id},
            )
        ).scalar()
    assert (
        trail == 0
    ), "the operator user read is now audited — #1318 appears partly fixed"


async def test_finding_an_operator_mutates_another_tenants_user_account(world):
    """FINDING: the same surface writes, too — deactivate and token revocation.

    Asserted against the ROW, not the response: a route that answers 200 and
    changes nothing would be a different (and much smaller) problem than this
    one, and only the database can tell them apart.
    """
    deactivate = await _call(
        world,
        world.token_operator_a,
        "POST",
        f"/api/v1/admin/users/{world.b.user_id}/deactivate",
    )
    revoke = await _call(
        world,
        world.token_operator_a,
        "POST",
        f"/api/v1/auth/users/{world.b.user_id}/revoke-tokens",
    )

    assert (
        deactivate.status_code == 200
    ), "cross-tenant deactivation is now refused — #1318 appears fixed"
    assert (
        revoke.status_code == 200
    ), "cross-tenant token revocation is now refused — #1318 appears fixed"

    async with world.superuser_engine.begin() as conn:
        is_active = (
            await conn.execute(
                text("SELECT is_active FROM users WHERE user_id = :u"),
                {"u": world.b.user_id},
            )
        ).scalar()
    assert is_active is False, (
        "the 200 did not actually deactivate the account; re-read #1318 before "
        "changing this case"
    )


# =============================================================================
# Surface 6 — mutations, asserted against the database
# =============================================================================
#
# Every case here checks the ROW after the refusal. A status code says what the
# caller was told; only the row says what happened. The specific failure this
# guards is a handler that answers 404 for the reader and still runs the write —
# which is exactly the shape a "delete is idempotent, absent is fine" refactor
# produces.


async def test_the_other_tenants_case_survives_every_mutation_a_tries(world):
    """Update, close and delete, in that order, then read the row back."""
    case_id = world.b.case.case_id

    update = await as_a(
        world, "PUT", f"/api/v1/cases/{case_id}", json={"title": "PWNED"}
    )
    close = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{case_id}/close",
        json={"closure_reason": "other"},
    )
    delete = await as_a(world, "DELETE", f"/api/v1/cases/{case_id}")

    assert_refused(update, f"PUT /api/v1/cases/{case_id}")
    assert_refused(close, f"POST /api/v1/cases/{case_id}/close")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title, state FROM cases WHERE case_id = :c"),
                {"c": case_id},
            )
        ).first()
    assert row is not None, (
        f"tenant A's DELETE (answered {delete.status_code}) removed tenant B's "
        "case from the database"
    )
    assert row[0] == SECRET_B_TITLE, "tenant A's refused PUT renamed B's case"
    assert row[1] == "inquiry", "tenant A's refused close moved B's case to terminal"

    # Same call as B: the control that the mutations are reachable at all, so
    # the refusals above are attributable to the boundary rather than to a
    # route that refuses everyone.
    control = await as_b(
        world,
        "PUT",
        f"/api/v1/cases/{case_id}",
        json={"title": "B renames its own case"},
    )
    assert (
        control.status_code == 200
    ), "control: B cannot update B's own case, so A's 404 proves nothing"


async def test_a_case_cannot_be_shared_into_the_other_tenants_team(world):
    """``team-shares`` writes a ``resource_shares`` row — the KB allowlist arm.

    Two directions, and the second is the subtle one. Sharing B's case to A's
    team would be a straightforward theft; sharing **A's own** case into **B's**
    team plants a row in B's tenant, and #1168 records that any id reaching the
    shared arm is served by the vector layer verbatim.
    """
    steal = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{world.b.case.case_id}/team-shares",
        json={"team_id": world.a.team_id},
    )
    plant = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{world.a.case.case_id}/team-shares",
        json={"team_id": world.b.team_id},
    )
    unshare = await as_a(
        world,
        "DELETE",
        f"/api/v1/cases/{world.b.case.case_id}/team-shares/{world.b.team_id}",
    )

    assert_refused(steal, "POST .../team-shares (B's case -> A's team)")
    assert_refused(plant, "POST .../team-shares (A's case -> B's team)")
    assert_refused(unshare, "DELETE .../team-shares/{team_id}")

    async with world.superuser_engine.begin() as conn:
        planted = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_shares "
                    "WHERE scope_id = :t AND resource_type = 'case'"
                ),
                {"t": world.b.team_id},
            )
        ).scalar()
    assert planted == 0, (
        "a refused share planted a resource_shares row in the other tenant's "
        "team; that row is an allowlist entry the vector layer serves verbatim"
    )


async def test_team_membership_never_spans_organizations(world):
    """``GET /teams`` — the input to every share-mediated read.

    If this listed another tenant's team, the share arm downstream would resolve
    ids from it, so this is upstream of the KB and case share paths rather than
    a surface of its own.
    """
    mine = await as_a(world, "GET", "/api/v1/teams")
    theirs = await as_b(world, "GET", "/api/v1/teams")

    assert mine.status_code == 200 and theirs.status_code == 200
    assert _ids(mine.json(), "team_id") == {world.a.team_id}
    assert _ids(theirs.json(), "team_id") == {world.b.team_id}
    assert _ids(mine.json(), "organization_id") == {world.a.org_id}


# =============================================================================
# Surface 7 — the rest of the case-addressed operations, as one battery
# =============================================================================
#
# These are the operations whose only tenant handle is ``{case_id}`` in the
# path. They are driven with bodies valid enough to get PAST request validation:
# a 422 for a missing field is not a refusal, it is an assertion that never ran.

CASE_ADDRESSED_OPERATIONS = [
    # ``turns`` is a multipart form, not JSON. Sent as ``data=`` (see the
    # battery body) so the query field actually binds and the request reaches
    # the case check instead of failing its own validation.
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/turns",
        {"__form__": {"query": "probe"}},
        id="turns",
    ),
    pytest.param("POST", "/api/v1/cases/{case_id}/title", {}, id="title"),
    pytest.param(
        "POST", "/api/v1/cases/{case_id}/extract-knowledge", {}, id="extract-knowledge"
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/reports",
        {"report_type": "closure_summary"},
        id="reports-generate",
    ),
    pytest.param("POST", "/api/v1/cases/{case_id}/sessions", {}, id="session-create"),
    pytest.param("GET", "/api/v1/cases/{case_id}/sessions", None, id="session-list"),
    pytest.param(
        "GET", "/api/v1/cases/{case_id}/sessions/active", None, id="session-active"
    ),
    pytest.param(
        "GET",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001",
        None,
        id="session-get",
    ),
    pytest.param(
        "PATCH",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001",
        {},
        id="session-patch",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/pause",
        {},
        id="session-pause",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/resume",
        {},
        id="session-resume",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/complete",
        {},
        id="session-complete",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/sessions/sess_probe_0001/resume/{case_id}",
        {},
        id="session-resume-case",
    ),
]


@pytest.mark.parametrize("method,template,body", CASE_ADDRESSED_OPERATIONS)
async def test_every_case_addressed_operation_refuses_the_other_tenant(
    world, method, template, body
):
    """One battery over the remaining ``{case_id}`` operations.

    ``app.state.investigation_service`` is a tripwire (see :class:`_Tripwire`),
    so a route that resolved the engine before checking the case would answer
    500 rather than a refusal — which this assertion rejects. That is what keeps
    the battery honest for the write paths whose collaborators are not wired
    here: they cannot pass by being unavailable.
    """
    path = template.format(case_id=world.b.case.case_id)
    if body is not None and "__form__" in body:
        response = await as_a(world, method, path, data=body["__form__"])
    else:
        response = await as_a(world, method, path, json=body)

    assert_refused(response, f"{method} {path}")


async def test_report_generation_by_case_id_refuses_the_other_tenant(world):
    """``POST /reports/generate?case_id=...`` — the case id rides in the query.

    The control is the *difference*: B's call reaches the generation service (and
    fails there, unwired), A's never resolves the case at all. Two different
    failures, and only one of them is the boundary.
    """
    path = f"/api/v1/reports/generate?case_id={world.b.case.case_id}"
    body = {"report_types": ["closure_summary"]}

    control = await as_b(world, "POST", path, json=body)
    attack = await as_a(world, "POST", path, json=body)

    assert control.status_code != 404, (
        "control: B's own generate request now 404s too, so A's 404 no longer "
        "distinguishes the boundary from a broken route"
    )
    assert attack.status_code == 404
    assert_no_b_content(attack, f"POST {path}")


async def test_the_debug_causal_graph_answers_200_to_everyone_and_content_to_one(world):
    """A development-only route that is NOT in the published contract.

    It answers 200 regardless, so the status code says nothing — which is why
    this case reads the body. B gets the graph; A gets ``case not found`` in a
    200 envelope. Included precisely because the generator excludes debug
    endpoints from ``openapi.json``: a route absent from the contract is still a
    route, and this is the one that carries a ``{case_id}``.
    """
    path = f"/debug/cases/{world.b.case.case_id}/causal-graph"

    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert (
        control.json().get("error") is None
    ), f"control: B cannot read B's own causal graph: {control.text[:300]}"
    assert "causal_nodes" in control.json()

    assert attack.status_code == 200
    assert attack.json().get("error") == "case not found"
    assert "causal_nodes" not in attack.json()
    assert_no_b_content(attack, f"GET {path}")


async def test_the_grant_listing_filter_cannot_name_another_tenant(world):
    """``GET /admin/grants?organization_id=...`` — a caller-supplied org filter.

    The operator mints a grant over their OWN tenant, then asks for grants in
    B's. A filter that *narrows* the operator's own rows is harmless; one that
    *selects* by organization is a cross-tenant read, and the control (the same
    call filtered to A) is what tells the two apart.
    """
    minted = await _call(
        world,
        world.token_operator_a,
        "POST",
        "/api/v1/admin/grants",
        json={
            "case_id": world.a.case.case_id,
            "organization_id": world.a.org_id,
            "reason": "probe: a grant over the operator's own tenant",
            "duration_minutes": 30,
        },
    )
    assert minted.status_code == 201, f"control: no grant minted: {minted.text[:300]}"
    grant_id = minted.json()["grant_id"]

    own = await _call(
        world,
        world.token_operator_a,
        "GET",
        f"/api/v1/admin/grants?organization_id={world.a.org_id}",
    )
    other = await _call(
        world,
        world.token_operator_a,
        "GET",
        f"/api/v1/admin/grants?organization_id={world.b.org_id}",
    )

    assert own.status_code == 200
    assert grant_id in _ids(own.json(), "grant_id"), (
        "control: the operator's own grant is not listed under its own org "
        "filter, so an empty answer for B's org proves nothing"
    )
    assert other.status_code == 200
    assert _ids(other.json(), "grant_id") == set()
    assert_no_b_content(other, "GET /api/v1/admin/grants?organization_id=<B>")


async def test_a_case_created_by_a_lands_in_as_organization(world):
    """Creation, the one write where the tenant is chosen rather than checked.

    A case is stamped from the request's bound tenant. The body carries no
    organization field to attack, so the attack here is on the *stamp*: read the
    row back as superuser and confirm it is A's, not the Standalone sentinel and
    not B's.
    """
    created = await as_a(
        world,
        "POST",
        "/api/v1/cases",
        json={
            "title": f"{SECRET_A}-created-through-the-api",
            "description": "probe",
            "severity": "medium",
        },
    )
    assert (
        created.status_code == 201
    ), f"control: A cannot create a case: {created.text[:300]}"
    case_id = created.json()["case_id"]

    try:
        async with world.superuser_engine.begin() as conn:
            org = (
                await conn.execute(
                    text("SELECT organization_id FROM cases WHERE case_id = :c"),
                    {"c": case_id},
                )
            ).scalar()
        assert org == world.a.org_id, (
            f"a case created by A was stamped {org!r}; anything but A's own "
            "organization is a row in someone else's tenant"
        )
    finally:
        async with world.superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM case_messages WHERE case_id = :c"), {"c": case_id}
            )
            await conn.execute(
                text("DELETE FROM cases WHERE case_id = :c"), {"c": case_id}
            )


async def test_the_case_list_team_filter_cannot_name_the_other_tenants_team(world):
    """``GET /cases?team_id=`` — a caller-supplied team id on a read.

    The team arm of the case allowlist resolves through ``resource_shares``. A
    filter that narrows within the caller's own teams is a feature; one that
    *selects* a team by id is a cross-tenant read, so the attacker names B's.
    """
    attack = await as_a(world, "GET", f"/api/v1/cases?team_id={world.b.team_id}")

    assert attack.status_code in (200, 403, 422)
    if attack.status_code == 200:
        assert world.b.case.case_id not in _ids(attack.json(), "case_id")
    assert_no_b_content(attack, "GET /api/v1/cases?team_id=<B's team>")


async def test_a_runbook_cannot_be_published_into_the_other_tenants_team(world):
    """``POST /knowledge/runbooks/create`` with ``team_id`` naming B's team.

    A publish, not a read — and the dangerous direction. The team id becomes a
    ``resource_shares`` row, and #1168 records that any id reaching the shared
    arm is served by the vector layer verbatim; a row planted in B's team would
    put A's content inside B's KB reads. Verified against the rows, not the
    status, and the same guard covers ``POST /knowledge/convert``, whose
    ``team_id`` travels the identical path.
    """
    body = {
        "title": f"{SECRET_A} planted runbook",
        "domain": "database",
        "service": "postgres",
        "symptom_class": ["timeout"],
        "severity": "medium",
        "scope": "team",
        "team_id": world.b.team_id,
        "symptom_recognition": "connections time out under load",
        "applicability": "postgres 16 on bare metal",
        "diagnostic_steps": "1. check pool saturation",
        "causes": "pool exhausted",
        "prevention": "raise the pool ceiling",
    }
    attack = await as_a(world, "POST", "/api/v1/knowledge/runbooks/create", json=body)

    assert attack.status_code in REFUSED, (
        f"a runbook was published into another tenant's team "
        f"({attack.status_code}): {attack.text[:300]}"
    )

    async with world.superuser_engine.begin() as conn:
        planted = (
            await conn.execute(
                text("SELECT count(*) FROM resource_shares WHERE scope_id = :t"),
                {"t": world.b.team_id},
            )
        ).scalar()
        foreign_items = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM knowledge_items "
                    "WHERE organization_id = :o AND title LIKE :t"
                ),
                {"o": world.b.org_id, "t": f"{SECRET_A}%"},
            )
        ).scalar()
    assert planted == 1, (
        "a refused publish added a resource_shares row to the other tenant's "
        f"team (expected only the seeded knowledge_item share, found {planted})"
    )
    assert foreign_items == 0, "a refused publish wrote a knowledge item into B's org"

    control = await as_b(
        world,
        "POST",
        "/api/v1/knowledge/runbooks/create",
        json={**body, "title": f"{SECRET_B} own runbook", "team_id": world.b.team_id},
    )
    assert control.status_code in (200, 201), (
        "control: B cannot publish into B's own team either, so A's refusal is "
        f"not attributable to the boundary: {control.text[:300]}"
    )


# =============================================================================
# The inventory — derived from the live app, not from memory
# =============================================================================
#
# A hand-written list of "the surfaces that carry tenant data" is wrong the day
# after it is written. So the set is COMPUTED from the running application's
# OpenAPI document, and this file only has to say, for each computed operation,
# whether it is probed or deliberately not. A new route carrying a ``{case_id}``
# fails the suite until someone decides which.
#
# The classifier is deliberately crude and over-inclusive: any operation whose
# path, request body or query string names an identifier that can address
# another tenant's row. Over-inclusion costs an inventory line; under-inclusion
# costs a surface nobody probed.

#: Path parameters that name a tenant-owned row. ``component_name`` and ``role``
#: are the two path parameters that do not (a health component, an RBAC role
#: name), and their absence here is the whole of the exclusion.
TENANT_SCOPED_PATH_PARAMS = frozenset(
    {
        "case_id",
        "conversion_id",
        "data_id",
        "document_id",
        "draft_id",
        "evidence_id",
        "file_id",
        "grant_id",
        "report_id",
        "session_id",
        "suggestion_id",
        "team_id",
        "user_id",
        "username",
    }
)

#: Request-body and query fields that can name someone else's row. ``client_id``
#: is excluded: it identifies an OAuth client, not a tenant resource.
TENANT_SCOPED_FIELDS = frozenset(
    {
        "case_id",
        "organization_id",
        "session_id",
        "team_id",
        "user_id",
        "draft_ids",
        "document_ids",
        "item_ids",
    }
)

_PROBED = "probed"
_FINDING = "finding"
_EXEMPT = "exempt"

#: Every tenant-scoped operation, and what this module does about it.
#:
#: ``_PROBED`` — an attack and a positive control exist above.
#: ``_FINDING`` — the boundary does NOT hold; asserted as it behaves, with an
#:   issue reference, so a fix turns the case red.
#: ``_EXEMPT`` — deliberately not probed, with the reason. Every reason is a
#:   claim about the route, so it can be checked; "no time" is not one of them.
SURFACE_INVENTORY: dict[tuple[str, str], tuple[str, str]] = {
    # --- cases: read ------------------------------------------------------
    ("GET", "/api/v1/cases"): (_PROBED, "case list + the team_id filter"),
    ("POST", "/api/v1/cases"): (_PROBED, "creation stamps the caller's own org"),
    ("POST", "/api/v1/cases/search"): (_PROBED, "search, plus an injected org/user"),
    ("GET", "/api/v1/cases/{case_id}"): (_PROBED, "case detail"),
    ("GET", "/api/v1/cases/{case_id}/ui"): (_PROBED, "case UI projection"),
    ("GET", "/api/v1/cases/{case_id}/messages"): (_PROBED, "transcript"),
    ("GET", "/api/v1/cases/{case_id}/analytics"): (_PROBED, "counts are inference"),
    ("GET", "/api/v1/cases/{case_id}/report-recommendations"): (
        _PROBED,
        "refusal shape (404 vs the owner's 400)",
    ),
    # --- cases: write -----------------------------------------------------
    ("PUT", "/api/v1/cases/{case_id}"): (_PROBED, "mutation battery, row-checked"),
    ("DELETE", "/api/v1/cases/{case_id}"): (_PROBED, "mutation battery, row-checked"),
    ("POST", "/api/v1/cases/{case_id}/close"): (
        _PROBED,
        "mutation battery, row-checked",
    ),
    ("POST", "/api/v1/cases/{case_id}/title"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/turns"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/extract-knowledge"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/reports"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/team-shares"): (
        _PROBED,
        "both directions, resource_shares checked",
    ),
    ("DELETE", "/api/v1/cases/{case_id}/team-shares/{team_id}"): (
        _PROBED,
        "unshare, resource_shares checked",
    ),
    # --- evidence, files, case data ---------------------------------------
    ("GET", "/api/v1/cases/{case_id}/evidence"): (_PROBED, "evidence listing"),
    ("GET", "/api/v1/cases/{case_id}/evidence/{evidence_id}"): (
        _PROBED,
        "evidence detail",
    ),
    ("GET", "/api/v1/cases/{case_id}/uploaded-files"): (_PROBED, "file listing"),
    ("GET", "/api/v1/cases/{case_id}/uploaded-files/{file_id}"): (
        _PROBED,
        "file detail",
    ),
    ("GET", "/api/v1/cases/{case_id}/data"): (_PROBED, "case data listing"),
    ("GET", "/api/v1/cases/{case_id}/data/{data_id}"): (_PROBED, "case data read"),
    ("DELETE", "/api/v1/cases/{case_id}/data/{data_id}"): (
        _PROBED,
        "case data delete",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): (
        _EXEMPT,
        "the route is feature-disabled — it answers 404 "
        "'Reclassification endpoint is not enabled' to its OWNER, so an "
        "attacker-side 404 would be indistinguishable from the tenant check. "
        "Re-probe when the flag is turned on.",
    ),
    ("POST", "/api/v1/cases/{case_id}/queries"): (
        _EXEMPT,
        "410 Gone — removed in favour of POST /cases/{case_id}/turns, which is "
        "probed. The handler reads no case at all.",
    ),
    ("POST", "/api/v1/cases/{case_id}/data"): (
        _EXEMPT,
        "410 Gone — removed in favour of turn attachments; the handler reads "
        "no case at all.",
    ),
    # --- investigation sessions (case-nested) ------------------------------
    ("GET", "/api/v1/cases/{case_id}/sessions"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/sessions"): (_PROBED, "case-addressed battery"),
    ("GET", "/api/v1/cases/{case_id}/sessions/active"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("GET", "/api/v1/cases/{case_id}/sessions/{session_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/sessions/{session_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/pause"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/resume"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/complete"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/sessions/{session_id}/case"): (
        _EXEMPT,
        "the handler resolves the session from the Redis-backed auth session "
        "store before touching a case; with no store it 500s for the OWNER "
        "too, so no positive control exists in-process. Its case-side effect "
        "is the same allowlist the probed /cases/{case_id}/sessions battery "
        "exercises.",
    ),
    # --- reports -----------------------------------------------------------
    ("GET", "/api/v1/cases/{case_id}/reports"): (_PROBED, "case-nested listing"),
    ("GET", "/api/v1/cases/{case_id}/reports/{report_id}/download"): (
        _PROBED,
        "report download",
    ),
    ("GET", "/api/v1/reports/case/{case_id}"): (_PROBED, "reports by case"),
    ("GET", "/api/v1/reports/{report_id}"): (_PROBED, "report by id"),
    ("PUT", "/api/v1/reports/{report_id}"): (_PROBED, "report edit, row-checked"),
    ("DELETE", "/api/v1/reports/{report_id}"): (
        _PROBED,
        "report delete, row-checked",
    ),
    ("GET", "/api/v1/reports/{report_id}/versions"): (_PROBED, "version history"),
    ("POST", "/api/v1/reports/generate"): (_PROBED, "case_id rides in the query"),
    ("POST", "/api/v1/reports/{report_id}/link-case"): (
        _PROBED,
        "case-addressed battery (report id resolves first)",
    ),
    # --- knowledge ---------------------------------------------------------
    ("GET", "/api/v1/knowledge/documents/{document_id}"): (_PROBED, "item read"),
    ("GET", "/api/v1/knowledge/documents/{document_id}/snippet"): (
        _PROBED,
        "item snippet",
    ),
    ("PUT", "/api/v1/knowledge/documents/{document_id}"): (
        _PROBED,
        "item edit, row-checked",
    ),
    ("DELETE", "/api/v1/knowledge/documents/{document_id}"): (
        _PROBED,
        "item delete, row-checked",
    ),
    ("POST", "/api/v1/knowledge/runbooks/create"): (
        _PROBED,
        "publish into another tenant's team, rows checked",
    ),
    ("POST", "/api/v1/knowledge/convert"): (
        _EXEMPT,
        "multipart upload whose conversion pass needs a live LLM provider. Its "
        "team_id travels the identical share-resolution path as "
        "POST /knowledge/runbooks/create, which IS probed for a planted share.",
    ),
    ("GET", "/api/v1/knowledge/conversions/{conversion_id}"): (
        _PROBED,
        "conversion detail",
    ),
    ("GET", "/api/v1/knowledge/conversions/by-case/{case_id}"): (
        _PROBED,
        "conversion by case",
    ),
    ("PUT", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _PROBED,
        "draft edit, row-checked",
    ),
    ("DELETE", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _PROBED,
        "draft discard, row-checked",
    ),
    (
        "POST",
        "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}/verify",
    ): (
        _PROBED,
        "draft verify, row-checked",
    ),
    ("POST", "/api/v1/knowledge/drafts/verify-batch"): (
        _EXEMPT,
        "no positive control in-process: the batch path answers 'Conversion "
        "job not found' to the draft's OWNER, so an attacker-side failure "
        "would be indistinguishable from the endpoint being broken. The same "
        "rows are probed one at a time through the per-draft verify above.",
    ),
    ("GET", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    ("PUT", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/approve"): (
        _PROBED,
        "operator bound to A vs B's suggestion, row-checked",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/reject"): (
        _PROBED,
        "operator bound to A vs B's suggestion, row-checked",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/remediate-pii"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    # --- admin and break-glass --------------------------------------------
    ("GET", "/api/v1/admin/cases/{case_id}"): (
        _PROBED,
        "tenant admin refused; operator needs a grant naming the case",
    ),
    ("GET", "/api/v1/admin/cases/{case_id}/messages"): (
        _PROBED,
        "tenant admin refused; operator needs a grant naming the case",
    ),
    ("GET", "/api/v1/admin/grants"): (_PROBED, "the organization_id list filter"),
    ("POST", "/api/v1/admin/grants"): (_PROBED, "tenant admin refused; operator mints"),
    ("POST", "/api/v1/admin/grants/{grant_id}/revoke"): (
        _EXEMPT,
        "a grant row is operator-scoped, not tenant-scoped, and revoking one "
        "only REMOVES access — there is no cross-tenant read to attack. The "
        "tenant-admin arm is covered by the all-admin-routes battery.",
    ),
    ("GET", "/api/v1/admin/users/{user_id}"): (
        _FINDING,
        "#1318 — an operator bound to A reads B's user, unaudited",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/deactivate"): (
        _FINDING,
        "#1318 — an operator bound to A deactivates B's user",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/activate"): (
        _FINDING,
        "#1318 — same handler family as deactivate; the tenant-admin arm is "
        "probed, the operator arm is the finding",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/roles"): (
        _FINDING,
        "#1318 — an operator bound to A assigns roles on B's user",
    ),
    ("DELETE", "/api/v1/admin/users/{user_id}/roles/{role}"): (
        _FINDING,
        "#1318 — an operator bound to A removes roles on B's user",
    ),
    ("POST", "/api/v1/auth/users/{user_id}/revoke-tokens"): (
        _FINDING,
        "#1318 — an operator bound to A revokes B's user's tokens",
    ),
    ("DELETE", "/api/v1/auth/users/{username}"): (
        _FINDING,
        "#1318 — reachable across tenants; deliberately NOT exercised here "
        "because it destroys the account, which would perturb every later "
        "assertion in the run",
    ),
    # --- auth sessions (Redis-backed, not a SQL tenant surface) ------------
    ("GET", "/api/v1/sessions"): (
        _EXEMPT,
        "auth sessions live in the Redis session store, keyed per user and "
        "carrying no organization column; with no store the listing answers "
        "the same empty page to every caller, so no control exists.",
    ),
    ("POST", "/api/v1/sessions"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("GET", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("PUT", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("DELETE", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("POST", "/api/v1/sessions/{session_id}/archive"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("POST", "/api/v1/sessions/{session_id}/heartbeat"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("POST", "/api/v1/sessions/{session_id}/restore"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/recovery-info"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/stats"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/cases"): (
        _EXEMPT,
        "the case half resolves through the same allowlist the probed case "
        "list uses; the session half is Redis (see GET /api/v1/sessions).",
    ),
    # --- development-only ---------------------------------------------------
    ("GET", "/debug/cases/{case_id}/causal-graph"): (
        _PROBED,
        "200 to everyone; the BODY is the boundary",
    ),
}


def tenant_scoped_operations(app) -> dict[tuple[str, str], str]:
    """Every operation in the LIVE app that can address a tenant-owned row."""
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})

    def properties(schema: dict) -> set:
        ref = schema.get("$ref")
        if ref:
            return set(schemas.get(ref.split("/")[-1], {}).get("properties", {}) or {})
        return set(schema.get("properties", {}) or {})

    found: dict[tuple[str, str], str] = {}
    for path, operations in spec["paths"].items():
        path_params = set(re.findall(r"\{([^}]+)\}", path))
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            reasons = []
            if path_params & TENANT_SCOPED_PATH_PARAMS:
                reasons.append(
                    "path:" + ",".join(sorted(path_params & TENANT_SCOPED_PATH_PARAMS))
                )
            for content in (
                (operation.get("requestBody") or {}).get("content", {}).values()
            ):
                hit = properties(content.get("schema", {})) & TENANT_SCOPED_FIELDS
                if hit:
                    reasons.append("body:" + ",".join(sorted(hit)))
            query = sorted(
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query"
                and parameter["name"] in TENANT_SCOPED_FIELDS
            )
            if query:
                reasons.append("query:" + ",".join(query))
            if reasons:
                found[(method.upper(), path)] = ";".join(sorted(set(reasons)))
    return found


def test_every_tenant_scoped_route_is_in_the_inventory(probe_app):
    """A new tenant-scoped route fails this module until someone classifies it.

    This is the only assertion here that cannot rot silently. Everything else
    describes surfaces that existed when it was written; this one asks the
    running application what surfaces exist now.

    It fails in both directions on purpose. An operation missing from the
    inventory is an unprobed surface. An inventory entry with no matching
    operation is a probe aimed at a route that no longer exists — which, left
    alone, is a green test asserting nothing about anything.
    """
    live = tenant_scoped_operations(probe_app.app)

    unclassified = {
        key: why for key, why in live.items() if key not in SURFACE_INVENTORY
    }
    assert not unclassified, (
        "these tenant-scoped operations are not in SURFACE_INVENTORY — probe "
        "them, or add an entry saying why not:\n"
        + "\n".join(
            f"  {m} {p}  ({why})" for (m, p), why in sorted(unclassified.items())
        )
    )

    stale = sorted(set(SURFACE_INVENTORY) - set(live))
    assert not stale, (
        "these SURFACE_INVENTORY entries name operations the app no longer "
        "exposes (or no longer classifies as tenant-scoped):\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )


def _resolve_reason(reason: str) -> str:
    """Follow a ``see <METHOD> <path>`` cross-reference to the stated reason.

    The Redis session routes share one reason between eleven entries. Repeating
    it eleven times invites the copies to drift; pointing at it keeps one text
    and still forces that text to exist — a dangling pointer resolves to itself
    and fails the length rule below.
    """
    if not reason.startswith("see "):
        return reason
    target = reason[len("see ") :].strip()
    method, _, path = target.partition(" ")
    referenced = SURFACE_INVENTORY.get((method, path))
    if referenced is None or referenced[0] != _EXEMPT:
        return reason
    return referenced[1]


def test_the_inventory_states_a_reason_for_every_unprobed_surface():
    """An exemption without a reason is an unprobed surface with a nice name."""
    for (method, path), (disposition, reason) in SURFACE_INVENTORY.items():
        assert disposition in (
            _PROBED,
            _FINDING,
            _EXEMPT,
        ), f"{method} {path}: unknown disposition {disposition!r}"
        assert reason.strip(), f"{method} {path}: no reason given"
        if disposition == _EXEMPT:
            resolved = _resolve_reason(reason)
            assert len(resolved) > 40, (
                f"{method} {path}: an exemption reason has to say what about "
                f"the route makes it unprobeable, not {reason!r}"
            )
        if disposition == _FINDING:
            assert (
                "#" in reason
            ), f"{method} {path}: a finding must name the issue that tracks it"
