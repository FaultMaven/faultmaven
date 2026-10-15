"""The authored-orphan sweep under each PostgreSQL posture (#1982).

The sweep deletes an authored chunk only when its row read is proven complete,
by two independent guards in the same transaction as the read:
``NOT row_security_active('knowledge_items')``, and the read itself issued under
``SET LOCAL row_security = off`` — which makes PostgreSQL refuse a read any
policy would filter instead of filtering it. This module runs it (armed), through the production engine (whose ``begin``
listener binds ``app.current_enterprise_id``), under every role that can run
the KB bootstrap, in a database migrated to head by a NON-superuser owner —
Cloud's shape:

==================  =========================  =======  ==========================
role                grants                     proven?  swept?
==================  =========================  =======  ==========================
table owner         owns every table           yes      every enterprise
maintenance         03-create-maintenance-     yes      every enterprise
                    role.sql (BYPASSRLS,
                    SELECT + INSERT/DELETE on
                    knowledge_items)
app role, bound     02-create-rls-app-role.sql no       nothing; every enterprise
to one enterprise   (RLS applies)                       reported unproven
owner, table        FORCE ROW LEVEL SECURITY   no       nothing
FORCEd
==================  =========================  =======  ==========================

The owner is the posture that tells the right proof from the tempting one: it
reads every row with ``rolbypassrls = false``. The app role is the hazard. With
the first guard forced to answer "unfiltered", the second still refuses the
read and nothing is lost; only with BOTH guards switched off does the app role
lose LIVE chunks, under either binding — which shows the tests can see the
hazard the guards exist to prevent.

Roles carry the grants of faultmaven-enterprise-infra's ``02`` / ``03`` init
scripts for the ``public`` schema, under names of this module's own (roles are
cluster-wide).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.bootstrap import kb_orphan_sweep
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence import database
from faultmaven.utils.runbook_id import is_builtin_item_id
from tests import kb_orphan_sweep_scenario as sc
from tests.integration.security.conftest import (
    RUNTIME_ROLE,
    alembic_on,
    create_limited_role,
    drop_role_sql,
    limited_url,
)
from tests.unit.bootstrap.test_kb_orphan_sweep import chroma_client

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

_PW = "fm_1982_sweep_pw"  # pragma: allowlist secret
ALL_AUTHORED_ORPHANS = tuple(sc.AUTHORED_ORPHANS.values()) + (sc.UNSTAMPED_ORPHAN,)


@dataclass(frozen=True)
class Postgres:
    superuser_url: str
    owner_url: str
    app_url: str
    maintenance_url: str


async def _execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _create_maintenance_role(db_superuser_url: str, role: str) -> None:
    """``03-create-maintenance-role.sql``'s role, for the ``public`` schema."""
    dbname = make_url(db_superuser_url).database
    await _execute(
        db_superuser_url,
        drop_role_sql(role),
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER BYPASSRLS "
        "NOCREATEDB NOCREATEROLE",
        f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '60s'",
        f'GRANT CONNECT ON DATABASE "{dbname}" TO {role}',
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}",
        f"GRANT INSERT, DELETE ON TABLE public.knowledge_items TO {role}",
    )


async def _seed(owner_url: str) -> None:
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as conn:
            await sc.seed_database(conn)
    finally:
        await engine.dispose()


@contextmanager
def provisioned_database():
    """A database migrated to head by its non-superuser owner, the two runtime
    roles, and the scenario's rows; dropped (roles too) however it ends."""
    superuser_url = os.environ["DATABASE_URL"]
    tag = uuid.uuid4().hex[:8]
    owner, app, maintenance = (f"fm1982_{r}_{tag}" for r in ("own", "app", "mnt"))
    dbname = f"fm1982_{tag}"
    db_superuser_url = (
        make_url(superuser_url)
        .set(database=dbname)
        .render_as_string(hide_password=False)
    )

    async def runtime_role_exists() -> bool:
        engine = create_async_engine(superuser_url)
        try:
            async with engine.connect() as conn:
                return bool(
                    (
                        await conn.execute(
                            text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
                            {"r": RUNTIME_ROLE},
                        )
                    ).first()
                )
        finally:
            await engine.dispose()

    created_runtime_role = not asyncio.run(runtime_role_exists())
    asyncio.run(
        _execute(
            superuser_url,
            drop_role_sql(owner),
            f"CREATE ROLE {owner} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
            *([f"CREATE ROLE {RUNTIME_ROLE} NOLOGIN"] if created_runtime_role else []),
            f'CREATE DATABASE "{dbname}" OWNER "{owner}"',
        )
    )
    try:
        owner_url = limited_url(db_superuser_url, owner, _PW)
        migrated = alembic_on(owner_url, "upgrade head")
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        asyncio.run(create_limited_role(db_superuser_url, app, _PW))
        asyncio.run(_create_maintenance_role(db_superuser_url, maintenance))
        asyncio.run(_seed(owner_url))
        yield Postgres(
            superuser_url=db_superuser_url,
            owner_url=owner_url,
            app_url=limited_url(db_superuser_url, app, _PW),
            maintenance_url=limited_url(db_superuser_url, maintenance, _PW),
        )
    finally:
        asyncio.run(
            _execute(
                superuser_url,
                f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)',
                drop_role_sql(app),
                drop_role_sql(maintenance),
                drop_role_sql(owner),
                *(
                    [f"DROP ROLE IF EXISTS {RUNTIME_ROLE}"]
                    if created_runtime_role
                    else []
                ),
            )
        )


@pytest.fixture(scope="module")
def pg():
    """One database for the module: the sweep only ever READS it."""
    with provisioned_database() as postgres:
        yield postgres


@asynccontextmanager
async def production_sessions(url: str, bind: str = sc.ST):
    """``get_db_session`` on the production engine for ``url``, bound to
    ``bind`` — its ``begin`` listener sets ``app.current_enterprise_id`` per
    transaction, exactly as the web bootstrap's and kb_seed's sessions do."""
    saved = (database._engine, database._session_factory)
    database._engine = None
    database._session_factory = None
    set_current_enterprise_id(bind)
    try:
        database.get_session_factory(url)
        yield database.get_db_session
    finally:
        if database._engine is not None:
            await database._engine.dispose()
        database._engine, database._session_factory = saved
        set_current_enterprise_id(sc.ST)


async def _sweep(tmp_path, url: str, bind: str = sc.ST):
    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, sc.DOCS)
    svc = SimpleNamespace(_vector_store=KnowledgeVectorStore(client))
    async with production_sessions(url, bind) as factory:
        async with factory() as session:
            posture = (
                await session.execute(
                    text(
                        "SELECT current_user, row_security_active('knowledge_items'), "
                        "(SELECT rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user)"
                    )
                )
            ).one()
        result = await kb_orphan_sweep.sweep_authored_orphans(
            svc, factory, max_parents=25, mode=kb_orphan_sweep.SWEEP_ARMED
        )
    return result, sc.chunk_ids_present(client), posture


def _assert_every_enterprise_swept(result, present):
    assert result.proven is True
    for enterprise_id, orphan in sc.AUTHORED_ORPHANS.items():
        assert result.enterprises[enterprise_id].deleted == [orphan.item_id]
    assert result.unattributed.deleted == [sc.UNSTAMPED_ORPHAN.item_id]
    assert sc.live_chunk_ids() <= present, sc.live_chunk_ids() - present
    assert not {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} & present


async def test_the_table_owner_sweeps_every_enterprise(pg, tmp_path):
    """The owner reads every row with ``rolbypassrls = false``: a proof keyed on
    that attribute would refuse the one role that runs migrations and, on a
    single-role self-hosted install, the API itself."""
    result, present, posture = await _sweep(tmp_path, pg.owner_url)

    assert posture[1:] == (False, False), posture
    _assert_every_enterprise_swept(result, present)


async def test_the_maintenance_role_sweeps_every_enterprise(pg, tmp_path):
    """kb_seed's role under multi: BYPASSRLS, so nothing filters its read."""
    result, present, posture = await _sweep(tmp_path, pg.maintenance_url)

    assert posture[1:] == (False, True), posture
    _assert_every_enterprise_swept(result, present)


async def _session_state(factory):
    """The pooled connection's identity and its ``row_security`` setting."""
    async with factory() as session:
        return tuple(
            (
                await session.execute(
                    text("SELECT pg_backend_pid(), current_setting('row_security')")
                )
            ).one()
        )


@pytest.mark.parametrize("mode", [kb_orphan_sweep.SWEEP_REPORT_ONLY, "armed"])
async def test_the_sweeps_row_security_off_does_not_outlive_its_transaction(
    pg, tmp_path, mode
):
    """#2022: the second guard's own observable. ``SET LOCAL`` ends with the
    sweep's transaction; a plain ``SET`` would leave ``row_security = off`` on
    the pooled connection, and the next session on the pool would read every
    enterprise's rows. Guard 1 returns first for any role RLS would affect, so
    only a role that reaches the ``SET`` (the maintenance role) shows it. The
    pid is asserted equal so the "next session" is the same connection."""
    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, sc.DOCS)
    svc = SimpleNamespace(_vector_store=KnowledgeVectorStore(client))
    async with production_sessions(pg.maintenance_url, sc.ST) as factory:
        before = await _session_state(factory)
        result = await kb_orphan_sweep.sweep_authored_orphans(
            svc,
            factory,
            max_parents=25,
            mode=(
                kb_orphan_sweep.SWEEP_ARMED
                if mode == "armed"
                else kb_orphan_sweep.SWEEP_REPORT_ONLY
            ),
        )
        after = await _session_state(factory)

    assert result.proven is True  # the SET ran: the read was issued under it
    assert after[0] == before[0], "the next session is not the swept connection"
    assert before[1] == "on"
    assert after[1] == "on", after


@pytest.mark.parametrize("bind", [sc.ST, sc.E1], ids=["bound-standalone", "bound-e1"])
async def test_the_app_role_proves_nothing_and_sweeps_nothing(pg, tmp_path, bind):
    """RLS filters the app role's read to the bound enterprise (plus the global
    tier), so no enterprise is proven — not even the bound one, whose rows it
    does see: the binding is not the proof. Every enterprise is reported."""
    result, present, posture = await _sweep(tmp_path, pg.app_url, bind)

    assert posture[1:] == (True, False), posture
    # The hazard first: a live chunk lost is what an unproven sweep costs.
    assert sc.live_chunk_ids() <= present, sc.live_chunk_ids() - present
    assert result.proven is False
    assert result.unproven_reason == kb_orphan_sweep.UNPROVEN_FILTERED
    assert result.deleted == []
    assert {e: c.unproven for e, c in result.enterprises.items()} == {
        sc.ST: 3,
        sc.E1: 3,
        sc.E2: 2,
    }
    assert result.unattributed.unproven == 3
    assert {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} <= present


#: Guard 1 forced to answer "unfiltered".
_PROOF_FORCED = "SELECT false WHERE CAST(:table AS text) IS NOT NULL"


@pytest.mark.parametrize("bind", [sc.ST, sc.E1], ids=["bound-standalone", "bound-e1"])
async def test_with_the_proof_forced_the_second_guard_refuses_the_read(
    pg, tmp_path, monkeypatch, bind
):
    """#2018 review R1: the proof was the only thing between the app role and
    a loss. With it forced, ``row_security = off`` makes the partial read an
    error, the run is unproven, and no live chunk goes."""
    monkeypatch.setattr(kb_orphan_sweep, "_ROW_SECURITY_ACTIVE", _PROOF_FORCED)

    result, present, _posture = await _sweep(tmp_path, pg.app_url, bind)

    assert sc.live_chunk_ids() <= present, sc.live_chunk_ids() - present
    assert result.proven is False
    assert result.unproven_reason == kb_orphan_sweep.UNPROVEN_READ_REFUSED
    assert result.deleted == []


@pytest.mark.parametrize("bind", [sc.ST, sc.E1], ids=["bound-standalone", "bound-e1"])
async def test_the_app_roles_read_under_row_security_off_raises(pg, bind):
    """F4, pinned: what the second guard rests on. The app role's read of
    knowledge_items with row security off is an ERROR, not a filtered answer —
    and the same role's read with row security on succeeds (the control)."""
    from sqlalchemy.exc import DBAPIError

    async with production_sessions(pg.app_url, bind) as factory:
        async with factory() as session:
            visible = (
                await session.execute(text("SELECT count(*) FROM knowledge_items"))
            ).scalar()
        assert 0 < visible < len([d for d in sc.DOCS if d.row])
        async with factory() as session:
            await session.execute(text("SET LOCAL row_security = off"))
            with pytest.raises(DBAPIError, match="row-level security policy"):
                await session.execute(text("SELECT item_id FROM knowledge_items"))
            await session.rollback()


#: What the app role loses with BOTH guards switched off. Bound to the
#: Standalone enterprise it cannot see E1's rows, and the reference-set check
#: does not save E1's unstamped legacy chunks: that bucket overlaps through the
#: Standalone document it CAN see. Bound to E1 it cannot see the Standalone
#: enterprise's personal rows, and the Standalone bucket overlaps through the
#: global document every session sees.
_LOST_WITHOUT_EITHER_GUARD = {
    sc.ST: {sc.UNSTAMPED_LIVE_E1},
    sc.E1: {sc.ST_LIVE_PERSONAL, sc.UNSTAMPED_LIVE_ST},
}


@pytest.mark.parametrize("bind", [sc.ST, sc.E1], ids=["bound-standalone", "bound-e1"])
async def test_negative_control_without_either_guard_the_app_role_loses_live_chunks(
    pg, tmp_path, monkeypatch, bind
):
    """The tests can see the hazard: with the proof forced AND row security
    left on (so the read is filtered rather than refused), the app role's
    partial read deletes live documents' chunks."""
    monkeypatch.setattr(kb_orphan_sweep, "_ROW_SECURITY_ACTIVE", _PROOF_FORCED)
    monkeypatch.setattr(kb_orphan_sweep, "_ROW_SECURITY_OFF", "SELECT 1")

    result, present, _posture = await _sweep(tmp_path, pg.app_url, bind)

    lost = sc.live_chunk_ids() - present
    assert lost == sc.live_chunk_ids(_LOST_WITHOUT_EITHER_GUARD[bind])


@pytest.mark.parametrize("proof_forced", [False, True], ids=["proof", "proof-forced"])
async def test_an_owner_on_a_forced_table_is_unproven(
    pg, tmp_path, monkeypatch, proof_forced
):
    """FORCE ROW LEVEL SECURITY subjects the owner to the policies: the first
    guard sees it, and with that guard forced the second still refuses."""
    if proof_forced:
        monkeypatch.setattr(kb_orphan_sweep, "_ROW_SECURITY_ACTIVE", _PROOF_FORCED)
    await _execute(pg.owner_url, "ALTER TABLE knowledge_items FORCE ROW LEVEL SECURITY")
    try:
        result, present, posture = await _sweep(tmp_path, pg.owner_url)
    finally:
        await _execute(
            pg.owner_url, "ALTER TABLE knowledge_items NO FORCE ROW LEVEL SECURITY"
        )

    assert posture[1] is True, posture
    assert result.proven is False
    assert result.unproven_reason == (
        kb_orphan_sweep.UNPROVEN_READ_REFUSED
        if proof_forced
        else kb_orphan_sweep.UNPROVEN_FILTERED
    )
    assert sc.live_chunk_ids() <= present
    assert result.deleted == []


async def test_a_proof_that_cannot_be_asked_is_unproven(pg, tmp_path, monkeypatch):
    """A failed check answers "unproven" and leaves the session usable."""
    monkeypatch.setattr(
        kb_orphan_sweep,
        "_ROW_SECURITY_ACTIVE",
        "SELECT row_security_active(CAST(:table AS text) || '_no_such_table')",
    )

    result, present, _posture = await _sweep(tmp_path, pg.owner_url)

    assert result.proven is False
    assert result.unproven_reason == kb_orphan_sweep.UNPROVEN_CHECK_FAILED
    assert result.skipped is None
    assert result.deleted == []
    assert {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} <= present


def test_kb_seed_under_the_maintenance_role_reports_then_sweeps_once_armed(
    tmp_path, caplog, monkeypatch
):
    """The whole bootstrap pass as ``kb_seed`` runs it under multi, in a
    database of its own (the pass writes: it seeds the pack and prunes
    built-ins). On the shipped settings it REPORTS every enterprise's authored
    orphans and deletes none; armed (``KB_ORPHAN_SWEEP_ENABLED`` and
    ``--no-dry-run``) it deletes exactly what the report listed. Nothing the
    role is refused is issued, and the counts reach the job result and the
    ``BootstrapResult(...)`` line."""
    from faultmaven.bootstrap import kb_init
    from faultmaven.config import settings as settings_module
    from faultmaven.config.settings import reset_settings
    from faultmaven.jobs.kb_seed import run as kb_seed_run
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from tests.unit.bootstrap.test_kb_init import _write_pack

    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, sc.DOCS)
    store = KnowledgeVectorStore(client)
    pack_dir = _write_pack(tmp_path)
    orphan_chunks = {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()}

    async def seed_as_kb_seed_does(postgres: Postgres, **job_kwargs) -> dict:
        async with production_sessions(postgres.maintenance_url) as factory:
            svc = KnowledgeService.__new__(KnowledgeService)
            svc._db_session_factory = factory
            svc._vector_store = store
            svc._share_repo = None
            svc._tracer = MagicMock()

            real_bootstrap = kb_init.bootstrap_kb

            async def bootstrap_this_pack(**kwargs):
                return await real_bootstrap(
                    **kwargs, project_root=tmp_path, pack_dir=pack_dir
                )

            # kb_seed opens its sessions through get_db_session, which
            # production_sessions has pointed at the maintenance role.
            with (
                patch.object(kb_init, "bootstrap_kb", bootstrap_this_pack),
                caplog.at_level(logging.INFO),
            ):
                return await kb_seed_run(
                    settings=MagicMock(),
                    container=SimpleNamespace(get_knowledge_service=lambda: svc),
                    **job_kwargs,
                )

    def noise():
        return [
            f"{r.levelname} {r.name}: {r.getMessage()}"
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith("faultmaven")
            # Rebuilding settings (the arming below) re-runs their own
            # coherence warnings, which are about the environment, not the pass.
            and r.name != "faultmaven.config.settings"
            and "[report-only]" not in r.getMessage()
        ]

    saved_settings = settings_module._settings_instance
    reset_settings()
    try:
        # Synchronous, so the database outlives the event loops the passes
        # run in.
        with provisioned_database() as postgres:
            reported = asyncio.run(seed_as_kb_seed_does(postgres))
            assert orphan_chunks <= sc.chunk_ids_present(client)
            report_lines = [
                r.getMessage()
                for r in caplog.records
                if r.getMessage().startswith("KB seed completed: BootstrapResult(")
            ]
            assert noise() == [], noise()
            caplog.clear()

            monkeypatch.setenv("KB_ORPHAN_SWEEP_ENABLED", "true")
            reset_settings()
            armed = asyncio.run(seed_as_kb_seed_does(postgres, dry_run=False))
    finally:
        monkeypatch.delenv("KB_ORPHAN_SWEEP_ENABLED", raising=False)
        # Restore, not reset: a later test must not rebuild settings (and
        # re-log their warnings) inside its own log window because of this one.
        settings_module._settings_instance = saved_settings

    assert reported["status"] == "completed", reported
    assert reported["ingested"] == 1
    report = reported["authored_orphans"]
    assert (report["mode"], report["proven"]) == ("report_only", True)
    assert (report["would_delete"], report["deleted"]) == (4, 0)
    assert report["would_delete_documents"] == {
        **{e: [d.item_id] for e, d in sc.AUTHORED_ORPHANS.items()},
        "unattributed": [sc.UNSTAMPED_ORPHAN.item_id],
    }
    assert len(report_lines) == 1
    assert "authored_orphans_mode=report_only, " in report_lines[0]
    assert "authored_orphans_would_delete=4, authored_orphans_deleted=0," in (
        report_lines[0]
    )

    assert armed["status"] == "completed", armed
    sweep = armed["authored_orphans"]
    assert (sweep["mode"], sweep["deleted"]) == ("armed", 4)
    assert {e: c["deleted"] for e, c in sweep["enterprises"].items()} == {
        e: 1 for e in sc.AUTHORED_ORPHANS
    }
    assert sweep["unattributed"]["deleted"] == 1
    present = sc.chunk_ids_present(client)
    # The built-ins outside this pack are pruned with their vectors (the
    # built-in path); every live AUTHORED chunk stays.
    authored_live = [d for d in sc.LIVE if not is_builtin_item_id(d.item_id)]
    assert sc.live_chunk_ids(authored_live) <= present
    assert not orphan_chunks & present
    assert noise() == [], noise()
