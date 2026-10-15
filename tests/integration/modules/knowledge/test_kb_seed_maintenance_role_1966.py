"""The KB bootstrap under ``kb_seed``'s maintenance role (#1966, PostgreSQL).

Under ``TENANT_PROVIDER=multi`` the ``kb_seed`` job connects as
``faultmaven_maintenance`` (faultmaven-enterprise-infra
``kubernetes/data/postgresql/init-scripts/03-create-maintenance-role.sql``):
BYPASSRLS, SELECT everywhere, INSERT and DELETE on ``knowledge_items`` — and
deliberately NO UPDATE. The first #1966 fix issued two statements that role is
refused: the repair's ``SELECT … FOR SHARE`` re-read (which needs UPDATE), so
every orphaned row's freshly written vectors were discarded with an ERROR on
every run; and the pack-hash stamp's UPDATE (a WARNING per older built-in per
run). The bootstrap now asks the role's capability once per pass and issues
neither.

The role here mirrors 03's grants exactly, in a database of the test's own.
"""

from __future__ import annotations

import logging
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_init
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.unit.bootstrap.test_kb_init import RUNBOOK_MD, _write_pack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

ORPHAN_ID = "0f0f0f0f-1111-2222-3333-444444444444"


class _KbStore:
    def __init__(self) -> None:
        self.parents: dict[str, int] = {}

    async def delete_documents_by_parent_id(
        self, parent_document_id, collection_name=None, *, retries=2
    ):
        return self.parents.pop(parent_document_id, 0)

    async def list_all_chunk_ids_by_parent(self):
        return {
            p: [f"{p}_chunk_{n}" for n in range(c)] for p, c in self.parents.items()
        }

    async def delete_observed_chunk_ids(self, chunk_ids, collection_name=None):
        ids = list(chunk_ids)
        for parent in {c.rsplit("_chunk_", 1)[0] for c in ids}:
            self.parents.pop(parent, None)
        return ids

    # The authored-orphan sweep's listing (#1982): this index holds no
    # authored chunk when the sweep runs (repair writes ORPHAN_ID's after it).
    async def list_chunk_ids_by_parent(self, where, collection_name=None):
        return {}

    async def delete_chunk_ids(self, chunk_ids, where, collection_name=None):
        return []


async def test_kb_seed_under_the_maintenance_role_repairs_and_never_updates(
    tmp_path, caplog
):
    from tests.integration.security.conftest import database_of_its_own

    role = f"fm_maint_{uuid.uuid4().hex[:8]}"
    superuser_url = os.environ["DATABASE_URL"]
    admin = create_async_engine(superuser_url, isolation_level="AUTOCOMMIT")
    try:
        async with database_of_its_own(superuser_url, "fm_1966_maint") as url:
            owner = create_async_engine(url)
            async with owner.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, 'E', 'e')"
                    ),
                    {"e": STANDALONE_ENTERPRISE_ID},
                )
                shipped = kb_init._item_id_from_runbook_id("example-runbook")
                # A built-in ingested before the stamp existed (metadata '{}')
                # whose content still matches the pack: the skip would stamp
                # it — an UPDATE this role is refused.
                # An authored row whose vectors are missing: repair's work.
                for item_id, content in (
                    (shipped, RUNBOOK_MD),
                    (ORPHAN_ID, "# Orphan\n\nbody"),
                ):
                    await conn.execute(
                        text(
                            "INSERT INTO knowledge_items (item_id, enterprise_id, "
                            "scope, title, content, item_type, metadata) "
                            "VALUES (:i, :e, 'global', 't', :c, 'runbook', '{}')"
                        ),
                        {"i": item_id, "e": STANDALONE_ENTERPRISE_ID, "c": content},
                    )
            # The maintenance role, exactly as 03-create-maintenance-role.sql
            # grants it: SELECT everywhere, INSERT + DELETE on knowledge_items.
            await owner.dispose()
            dbname = make_url(url).database
            async with admin.connect() as conn:
                await conn.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER "
                        "BYPASSRLS NOCREATEDB NOCREATEROLE"
                    )
                )
                await conn.execute(
                    text(f'GRANT CONNECT ON DATABASE "{dbname}" TO {role}')
                )
            owner = create_async_engine(url, isolation_level="AUTOCOMMIT")
            async with owner.connect() as conn:
                await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                await conn.execute(
                    text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
                )
                await conn.execute(
                    text(f"GRANT INSERT, DELETE ON TABLE knowledge_items TO {role}")
                )
            await owner.dispose()

            maint = create_async_engine(make_url(url).set(username=role, password="pw"))
            factory = async_sessionmaker(
                maint, class_=AsyncSession, expire_on_commit=False
            )
            store = _KbStore()
            store.parents[shipped] = 2
            svc = KnowledgeService.__new__(KnowledgeService)
            svc._db_session_factory = factory
            svc._vector_store = store
            svc._tracer = MagicMock()
            svc._share_repo = None

            async def _index(document, prechunked=None, enterprise_id=None):
                store.parents[document.document_id] = 3
                return 3

            svc._index_document_in_vector_store = _index
            real_stamp = kb_init._stamp_pack_hash
            stamp_calls: list[str] = []

            async def spy_stamp(item_id, metadata, session_factory):
                stamp_calls.append(item_id)
                await real_stamp(item_id, metadata, session_factory)

            try:
                with (
                    patch.object(kb_init, "_stamp_pack_hash", spy_stamp),
                    caplog.at_level(logging.WARNING),
                ):
                    result = await kb_init.bootstrap_kb(
                        knowledge_service=svc,
                        db_session_factory=factory,
                        enterprise_id=STANDALONE_ENTERPRISE_ID,
                        project_root=tmp_path,
                        pack_dir=_write_pack(tmp_path),
                    )

                async with factory() as session:
                    stored_meta = (
                        await session.execute(
                            text(
                                "SELECT metadata FROM knowledge_items WHERE item_id = :i"
                            ),
                            {"i": shipped},
                        )
                    ).scalar()
            finally:
                await maint.dispose()

            # The one expected line: reconcile's detection of the seeded
            # orphan, which is what sends it to repair. Anything else at
            # WARNING or above is the regression (a refused FOR SHARE re-read
            # logs an ERROR and discards; a refused stamp logs a WARNING).
            detection = f"KB consistency: knowledge_item {ORPHAN_ID} has no vectors"
            noise = [
                f"{r.levelname} {r.name}: {r.getMessage()}"
                for r in caplog.records
                if r.levelno >= logging.WARNING
                and r.name.startswith("faultmaven")
                and not r.getMessage().startswith(detection)
            ]
            assert noise == [], noise
            assert result.repaired_rows == [ORPHAN_ID], result
            assert store.parents.get(ORPHAN_ID) == 3, "the repair discarded its vectors"
            assert result.skipped_unchanged == ["global/example.md"], result
            assert (
                stamp_calls == []
            ), "a stamp was attempted under a role without UPDATE"
            assert stored_meta in ({}, "{}")
            assert result.failed == []
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()
