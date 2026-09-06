"""Integration test for ConversionService.scan_for_runbooks against a real
in-memory SQLAlchemy engine.

Exercises the full chain that the unit suite (which mocks the DB session)
misses: real INSERT statements hit the real schema, including FK constraints,
NOT NULL invariants, and TypeDecorator round-trips. This test specifically
guards against the three consumer-rewrite leaks fixed in the post-redesign
cleanup:

1. ``ConversionJobModel(source_filename=...)`` — column dropped in favor of
   ``source_file_id`` FK to ``uploaded_files``.
2. ``conversion_drafts.organization_id`` NOT NULL — was previously not set
   by the synthetic-job path.
3. ``ConversionDraftModel.tags`` is a TagsArray TypeDecorator expecting
   ``list[str]`` — passing a comma-joined string raises TypeError on bind.

If any of those regress, this test fails on a real INSERT, not a mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    OrganizationModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="function")
async def engine():
    """Async engine with a fresh in-memory SQLite database per test."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="function")
async def session_factory(engine):
    """Session factory bound to the in-memory engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="function")
async def seeded_session_factory(session_factory):
    """Pre-seed the default enterprise + organization rows so FK INSERTs from
    the conversion flow succeed (uploaded_files.organization_id, etc., are
    NOT NULL FKs that can't bind to nothing)."""
    async with session_factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id="00000000-0000-0000-0000-000000000001",
                name="Default Enterprise",
                slug="default",
            )
        )
        session.add(
            OrganizationModel(
                organization_id="00000000-0000-0000-0000-000000000001",
                enterprise_id="00000000-0000-0000-0000-000000000001",
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return session_factory


@pytest.fixture
def runbook_dir(tmp_path: Path) -> Path:
    """A directory containing a couple of sample runbooks under the
    standard ``global/<category>/`` structure ConversionService expects."""
    global_dir = tmp_path / "global" / "database"
    global_dir.mkdir(parents=True, exist_ok=True)

    (global_dir / "redis-oom.md").write_text(
        """---
id: redis-oom
title: Redis Out of Memory
domain: database
service: redis
severity: high
tags: [redis, oom, memory, cache]
---

# Redis Out of Memory

## Problem Definition

Redis is hitting the maxmemory limit. Operations begin failing with OOM
command not allowed errors. Service availability suffers.

## Diagnostic Steps

### Step 1. Check current memory usage
```bash
redis-cli INFO memory | grep used_memory_human
```

## Recovery Procedure

1. Increase maxmemory if RAM is available.
2. Configure an eviction policy (allkeys-lru) to age out cold keys.
3. Identify large keys with redis-cli --bigkeys for targeted cleanup.
""",
        encoding="utf-8",
    )

    (global_dir / "pg-slow-queries.md").write_text(
        """---
id: pg-slow-queries
title: PostgreSQL Slow Queries
domain: database
service: postgresql
severity: medium
tags: [postgres, performance, slow-query]
---

# PostgreSQL Slow Queries

## Problem Definition

Queries are taking longer than expected. Users see latency in dependent
services. The root cause is typically missing indexes, stale statistics,
or table bloat.

## Diagnostic Steps

### Step 1. Identify slow queries
```sql
SELECT query, mean_exec_time FROM pg_stat_statements
ORDER BY mean_exec_time DESC LIMIT 10;
```
""",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def conversion_service(seeded_session_factory, runbook_dir):
    """ConversionService wired against the in-memory DB and the temp
    runbook dir. The LLM router and settings are mocked because the scan
    path doesn't invoke the LLM."""
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"

    service = ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=seeded_session_factory,
    )

    # Override the hardcoded data_dir with the test fixture's temp path.
    with patch.object(
        type(service),
        "_data_dir",
        new=property(lambda self: runbook_dir),
    ):
        yield service


# =============================================================================
# Tests
# =============================================================================


class TestScanForRunbooks:
    """End-to-end scan flow against real schema."""

    @pytest.mark.asyncio
    async def test_scan_creates_three_table_chain(
        self, conversion_service, seeded_session_factory
    ):
        """Each discovered .md produces one row in uploaded_files +
        conversion_jobs + conversion_drafts, with FK linkage intact and
        organization_id populated on all three (the post-redesign NOT NULL
        invariant)."""
        result = await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        assert result["discovered"] == 2
        assert result["errors"] == []

        async with seeded_session_factory() as session:
            from sqlalchemy import select

            uploads = (await session.execute(select(UploadedFileModel))).scalars().all()
            jobs = (await session.execute(select(ConversionJobModel))).scalars().all()
            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )

            assert len(uploads) == 2
            assert len(jobs) == 2
            assert len(drafts) == 2

            # Every row populates organization_id (NOT NULL invariant).
            assert all(u.organization_id for u in uploads)
            assert all(j.organization_id for j in jobs)
            assert all(d.organization_id for d in drafts)

            # source_file_id FK linkage: every job points at a real upload row.
            upload_ids = {u.file_id for u in uploads}
            assert all(j.source_file_id in upload_ids for j in jobs)

            # upload_source distinguishes KB-bound uploads from case uploads.
            assert all(u.upload_source == "conversion_source" for u in uploads)
            # KB-bound uploads carry no case_id.
            assert all(u.case_id is None for u in uploads)

    @pytest.mark.asyncio
    async def test_scan_persists_tags_as_list_via_decorator(
        self, conversion_service, seeded_session_factory
    ):
        """Tags from frontmatter must be persisted as list[str] — passing
        a comma-joined string raises TypeError because TagsArray validates
        the bind shape. This test catches the third leak from the cleanup."""
        await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        async with seeded_session_factory() as session:
            from sqlalchemy import select

            drafts = (
                (
                    await session.execute(
                        select(ConversionDraftModel).where(
                            ConversionDraftModel.runbook_id == "redis-oom"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(drafts) == 1

            # On SQLite, TagsArray serializes the list to a comma-joined
            # TEXT column under the hood and deserializes on read. The shape
            # we get back is a list.
            tags = drafts[0].tags
            assert isinstance(tags, list)
            assert set(tags) == {"redis", "oom", "memory", "cache"}

    @pytest.mark.asyncio
    async def test_scan_idempotent_on_second_run(
        self, conversion_service, seeded_session_factory
    ):
        """Re-running scan over the same directory should skip already-tracked
        runbooks (drafts deduplicated by file_path) and not error."""
        first = await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )
        second = await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        assert first["discovered"] == 2
        assert second["discovered"] == 0
        assert second["skipped"] == 2

        async with seeded_session_factory() as session:
            from sqlalchemy import select

            uploads = (await session.execute(select(UploadedFileModel))).scalars().all()
            # Still 2 uploads — the second scan didn't create duplicates.
            assert len(uploads) == 2

    @pytest.mark.asyncio
    async def test_scan_with_explicit_organization_id(
        self, conversion_service, seeded_session_factory
    ):
        """When the caller passes organization_id explicitly, it propagates
        to all three persisted rows (not the default fallback)."""
        explicit_org = "00000000-0000-0000-0000-000000000001"

        await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=explicit_org, is_platform_admin=True
        )

        async with seeded_session_factory() as session:
            from sqlalchemy import select

            uploads = (await session.execute(select(UploadedFileModel))).scalars().all()
            jobs = (await session.execute(select(ConversionJobModel))).scalars().all()
            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )

            assert all(u.organization_id == explicit_org for u in uploads)
            assert all(j.organization_id == explicit_org for j in jobs)
            assert all(d.organization_id == explicit_org for d in drafts)


class TestScanSkipsBootstrapIngestedRunbooks:
    """The scan must not manufacture phantom drafts for runbooks already
    published into knowledge_items by the startup bootstrap (which ingests
    directly and never creates a draft, so the drafts table has no row).

    Regression: scan tracked files only via conversion_drafts and ignored
    knowledge_items, so every bootstrap-published runbook got a redundant
    status='draft' row — 60/60 in eval 2026-06-01.
    """

    @staticmethod
    async def _publish_kb_item(session_factory, runbook_id: str):
        from faultmaven.utils.runbook_id import item_id_from_runbook_id

        async with session_factory() as session:
            session.add(
                KnowledgeItemModel(
                    item_id=item_id_from_runbook_id(runbook_id),
                    # Global rows are the org-free platform tier (#770,
                    # knowledge_items_global_org_check).
                    enterprise_id=None,
                    scope="global",
                    title=runbook_id,
                    content="published by bootstrap",
                    item_type="runbook",
                )
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_scan_skips_file_already_in_knowledge_items(
        self, conversion_service, seeded_session_factory
    ):
        # redis-oom is already published by the bootstrap; pg-slow-queries is not.
        await self._publish_kb_item(seeded_session_factory, "redis-oom")

        result = await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        # Only the un-published runbook becomes a draft.
        assert result["discovered"] == 1
        async with seeded_session_factory() as session:
            from sqlalchemy import select

            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )
        active = {d.runbook_id for d in drafts if d.status == "draft"}
        assert "redis-oom" not in active  # phantom prevented
        assert "pg-slow-queries" in active

    @pytest.mark.asyncio
    async def test_scan_discards_existing_phantom_draft(
        self, conversion_service, seeded_session_factory
    ):
        # First scan (nothing published yet) creates both drafts.
        first = await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )
        assert first["discovered"] == 2

        # Bootstrap later publishes redis-oom directly into knowledge_items
        # (knowledge_item_id stays None on the draft — the two paths don't link).
        await self._publish_kb_item(seeded_session_factory, "redis-oom")

        # Second scan must DISCARD the now-redundant redis-oom draft and
        # leave the genuinely-pending pg-slow-queries draft alone.
        await conversion_service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        async with seeded_session_factory() as session:
            from sqlalchemy import select

            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )
        by_runbook = {d.runbook_id: d.status for d in drafts}
        assert by_runbook["redis-oom"] == "discarded"
        assert by_runbook["pg-slow-queries"] == "draft"


class TestScanGlobalAuthoringGate:
    """R4 (#770): scan mints global-scope drafts only for a platform operator.

    The fixture files live under ``global/database/`` → inferred scope
    ``global`` (the org-free platform corpus). Minting them into drafts is
    platform-tier authoring, so a caller who may not author global scope skips
    them; the same call by an admin single-tenant discovers them (covered by
    the ``is_platform_admin=True`` calls in the classes above).
    """

    @pytest.mark.asyncio
    async def test_non_admin_skips_global_files_single_tenant(
        self, conversion_service, seeded_session_factory
    ):
        from faultmaven.modules.knowledge.domain import global_authoring
        from faultmaven.providers.tenancy.factory import BUILTIN_SINGLE

        with patch.object(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
        ):
            result = await conversion_service.scan_for_runbooks(
                user_id="member", organization_id=None, is_platform_admin=False
            )

        # Both fixture runbooks are global-inferred → skipped, no drafts minted.
        assert result["discovered"] == 0
        assert result["skipped"] == 2
        async with seeded_session_factory() as session:
            from sqlalchemy import select

            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )
        assert drafts == []

    @pytest.mark.asyncio
    async def test_multi_tenant_skips_global_even_for_admin(
        self, conversion_service, seeded_session_factory
    ):
        from faultmaven.modules.knowledge.domain import global_authoring
        from faultmaven.providers.tenancy.factory import BUILTIN_MULTI

        with patch.object(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_MULTI
        ):
            result = await conversion_service.scan_for_runbooks(
                user_id="org-admin", organization_id=None, is_platform_admin=True
            )

        # Under multi no tenant session authors global — org admins included.
        assert result["discovered"] == 0
        assert result["skipped"] == 2
        async with seeded_session_factory() as session:
            from sqlalchemy import select

            drafts = (
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            )
        assert drafts == []
