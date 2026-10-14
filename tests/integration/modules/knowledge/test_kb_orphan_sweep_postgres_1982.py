"""The authored-orphan sweep under each PostgreSQL posture (#1982).

The sweep deletes an authored chunk only when its row read is proven complete:
``NOT row_security_active('knowledge_items')`` in the same transaction as the
read. This module runs it, through the production engine (whose ``begin``
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
==================  =========================  =======  ==========================

The owner is the posture that tells the right proof from the tempting one: it
reads every row with ``rolbypassrls = false``. The app role is the hazard: a
negative control with the proof switched off shows it losing LIVE chunks under
either binding, which is what the proof exists to prevent.

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
            svc, factory, max_parents=25
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
    assert result.deleted == []
    assert {e: c.unproven for e, c in result.enterprises.items()} == {
        sc.ST: 3,
        sc.E1: 3,
        sc.E2: 2,
    }
    assert result.unattributed.unproven == 3
    assert {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} <= present


#: What the app role loses when the proof is switched off. Bound to the
#: Standalone enterprise it cannot see E1's rows, and the reference-set check
#: does not save E1's unstamped legacy chunks: that bucket overlaps through the
#: Standalone document it CAN see. Bound to E1 it cannot see the Standalone
#: enterprise's personal rows, and the Standalone bucket overlaps through the
#: global document every session sees.
_LOST_WITHOUT_THE_PROOF = {
    sc.ST: {sc.UNSTAMPED_LIVE_E1},
    sc.E1: {sc.ST_LIVE_PERSONAL, sc.UNSTAMPED_LIVE_ST},
}


@pytest.mark.parametrize("bind", [sc.ST, sc.E1], ids=["bound-standalone", "bound-e1"])
async def test_negative_control_without_the_proof_the_app_role_loses_live_chunks(
    pg, tmp_path, monkeypatch, bind
):
    """The test can see the hazard: with the proof answering "unfiltered" the
    app role's partial read deletes live documents' chunks."""
    monkeypatch.setattr(
        kb_orphan_sweep,
        "_ROW_SECURITY_ACTIVE",
        "SELECT false WHERE CAST(:table AS text) IS NOT NULL",
    )

    result, present, _posture = await _sweep(tmp_path, pg.app_url, bind)

    lost = sc.live_chunk_ids() - present
    assert lost == sc.live_chunk_ids(_LOST_WITHOUT_THE_PROOF[bind])


async def test_a_proof_that_cannot_be_asked_is_unproven(pg, tmp_path, monkeypatch):
    """A failed check answers "unproven" and leaves the session usable."""
    monkeypatch.setattr(
        kb_orphan_sweep,
        "_ROW_SECURITY_ACTIVE",
        "SELECT row_security_active(CAST(:table AS text) || '_no_such_table')",
    )

    result, present, _posture = await _sweep(tmp_path, pg.owner_url)

    assert result.proven is False
    assert result.skipped is None
    assert result.deleted == []
    assert {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} <= present


def test_kb_seeds_pass_under_the_maintenance_role_sweeps_and_reports(tmp_path, caplog):
    """The whole bootstrap pass as ``kb_seed`` runs it under multi, in a
    database of its own (the pass writes: it seeds the pack and prunes
    built-ins). Every enterprise's authored orphans go, nothing the role is
    refused is issued, and the job result carries the counts."""
    from faultmaven.bootstrap import kb_init
    from faultmaven.jobs.kb_seed import run as kb_seed_run
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from tests.unit.bootstrap.test_kb_init import _write_pack

    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, sc.DOCS)
    store = KnowledgeVectorStore(client)
    pack_dir = _write_pack(tmp_path)

    async def seed_as_kb_seed_does(postgres: Postgres) -> dict:
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
                caplog.at_level(logging.WARNING),
            ):
                return await kb_seed_run(
                    settings=MagicMock(),
                    container=SimpleNamespace(get_knowledge_service=lambda: svc),
                )

    # Synchronous, so the database outlives the event loop the pass runs in.
    with provisioned_database() as postgres:
        job = asyncio.run(seed_as_kb_seed_does(postgres))

    assert job["status"] == "completed", job
    assert job["ingested"] == 1
    sweep = job["authored_orphans"]
    assert sweep["proven"] is True
    assert sweep["deleted"] == 4
    assert {e: c["deleted"] for e, c in sweep["enterprises"].items()} == {
        sc.ST: 1,
        sc.E1: 1,
        sc.E2: 1,
    }
    assert sweep["unattributed"]["deleted"] == 1
    present = sc.chunk_ids_present(client)
    # The built-ins outside this pack are pruned with their vectors (the
    # built-in path); every live AUTHORED chunk stays.
    authored_live = [d for d in sc.LIVE if not is_builtin_item_id(d.item_id)]
    assert sc.live_chunk_ids(authored_live) <= present
    assert not {c for d in ALL_AUTHORED_ORPHANS for c in d.chunk_ids()} & present
    noise = [
        f"{r.levelname} {r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("faultmaven")
    ]
    assert noise == [], noise
