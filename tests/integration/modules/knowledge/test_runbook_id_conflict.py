"""The 046 unique index, exercised through the real service.

``uq_conversion_drafts_org_runbook_id`` admits one LIVE draft per
``(organization_id, runbook_id)`` (#1230). That is a correctness win and a
liability at the same time: the mint is deterministic on ``(service, title)``,
so two ordinary user actions can reach the same id, and an unhandled
``IntegrityError`` there is a 500 that explains nothing.

Everything here runs against a real SQLAlchemy engine with the real schema —
``Base.metadata.create_all`` builds the partial unique index from the same
``Index`` declaration migration 046 creates — so the assertions are about what
the database does, not about a mock.

Three claims:

1. The index BITES through the service: a second live draft on the same key is
   refused rather than silently persisted (the #1230 defect).
2. It is refused as a ``ConflictError`` (409) naming the id, not as a bare
   ``IntegrityError`` (500).
3. The scan DEGRADES: one unmintable file is skipped with an error entry, and
   the walk over the rest completes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)

pytestmark = pytest.mark.integration

ORG = "00000000-0000-0000-0000-000000000001"

TEMPLATE_ARGS = dict(
    domain="platform",
    symptom_class=["availability"],
    severity="high",
    scope="global",
    tags=[],
    difficulty="medium",
    symptom_recognition="Requests start failing with 503 across the fleet.",
    applicability="Any deployment of the affected service.",
    diagnostic_steps="### Step 1. Look at the logs",
    causes="### Cause A: Something\nStatement: it broke.",
    prevention="Watch the dashboard.",
    user_id="user_x",
)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=ORG, name="Default Enterprise", slug="default"
            )
        )
        session.add(
            OrganizationModel(
                organization_id=ORG,
                enterprise_id=ORG,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


@pytest.fixture
def service(session_factory, tmp_path, monkeypatch):
    """The real service, writing under ``tmp_path``.

    ``monkeypatch.chdir`` because ``knowledge_root()`` is deliberately
    relative; the ``_data_dir`` patch is what the sibling scan test uses.
    """
    monkeypatch.chdir(tmp_path)
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    svc = ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=session_factory,
    )
    with patch.object(
        type(svc), "_data_dir", new=property(lambda self: tmp_path / "data/knowledge")
    ):
        yield svc


async def _live_draft_count(session_factory, runbook_id: Optional[str] = None) -> int:
    stmt = select(func.count()).select_from(ConversionDraftModel)
    stmt = stmt.where(ConversionDraftModel.status != "discarded")
    if runbook_id is not None:
        stmt = stmt.where(ConversionDraftModel.runbook_id == runbook_id)
    async with session_factory() as session:
        return (await session.execute(stmt)).scalar_one()


class TestTheIndexBitesThroughTheService:
    async def test_the_second_draft_with_the_same_title_is_refused(
        self, service, session_factory
    ):
        """#1230's general defect, driven through the real create path.

        Before 046 this persisted TWO rows sharing one ``runbook_id``, which
        verify/approve and every id-keyed lookup could not tell apart.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        runbook_id = first["draft"].runbook_id
        assert runbook_id, first

        with pytest.raises(ConflictError) as excinfo:
            await service.create_runbook_from_template(
                title="Connection Pool Exhausted",
                service_name="checkout-api",
                **TEMPLATE_ARGS,
            )

        # A 409 that names the id, not a bare IntegrityError.
        assert not isinstance(excinfo.value, IntegrityError)
        assert runbook_id in str(excinfo.value)
        assert excinfo.value.conflict_reason == "duplicate_runbook_id"
        assert excinfo.value.resource_type == "conversion_draft"

        assert await _live_draft_count(session_factory, runbook_id) == 1

    async def test_the_refused_create_does_not_clobber_the_existing_file(
        self, service, tmp_path
    ):
        """A 409 must mean nothing happened.

        The draft file is named after ``runbook_id``, so the second create
        resolves to the SAME path. Writing first and rejecting at the INSERT
        would leave the first draft's row pointing at the second author's
        content — a worse state than the duplicate rows this index removes.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        path = Path(first["draft"].file_path)
        before = path.read_text(encoding="utf-8")
        assert "Connection Pool Exhausted" in before

        with pytest.raises(ConflictError):
            await service.create_runbook_from_template(
                title="Connection Pool Exhausted",
                service_name="checkout-api",
                **{**TEMPLATE_ARGS, "prevention": "A DIFFERENT PREVENTION SECTION"},
            )

        assert path.read_text(encoding="utf-8") == before
        assert "A DIFFERENT PREVENTION SECTION" not in path.read_text(encoding="utf-8")

    async def test_a_punctuation_only_title_no_longer_collides_at_all(
        self, service, session_factory
    ):
        """#1230's reproducing case, end to end.

        Two runbooks whose ``(service, title)`` filters to nothing used to
        share ``runbook_id = ''``. With the mint fixed they get distinct ids,
        so BOTH persist — the conflict path is not even reached, which is the
        point: the index is the backstop, not the fix.
        """
        a = await service.create_runbook_from_template(
            title="???", service_name="...", **TEMPLATE_ARGS
        )
        b = await service.create_runbook_from_template(
            title="!!!", service_name="___", **TEMPLATE_ARGS
        )

        assert a["draft"].runbook_id
        assert b["draft"].runbook_id
        assert a["draft"].runbook_id != b["draft"].runbook_id
        assert await _live_draft_count(session_factory) == 2

    async def test_a_discarded_draft_does_not_block_its_own_title(
        self, service, session_factory
    ):
        """The partial predicate, executed through the service.

        Discard is a SOFT delete. Without ``WHERE status <> 'discarded'`` the
        tombstone would block re-authoring the same runbook forever.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        runbook_id = first["draft"].runbook_id

        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, first["draft"].draft_id)
            row.status = "discarded"
            await session.commit()

        again = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        assert again["draft"].runbook_id == runbook_id
        assert await _live_draft_count(session_factory, runbook_id) == 1


class TestTheScanDegradesRatherThanAborting:
    """One file the index will not take must not kill the whole walk.

    The scan already skips a path it cannot contain; a taken id is the same
    class of problem and gets the same treatment, because a scan that aborts
    on file 3 of 90 leaves the operator with no inventory and no filename.
    """

    @staticmethod
    def _runbook(runbook_id: str, title: str) -> str:
        return f"""---
id: {runbook_id}
title: {title}
domain: database
service: redis
severity: high
tags: [redis]
---

# {title}

## Problem Definition

Redis is hitting the maxmemory limit and operations begin failing with OOM
command not allowed errors, so service availability suffers badly.

## Diagnostic Steps

### Step 1. Check current memory usage
```bash
redis-cli INFO memory | grep used_memory_human
```

## Recovery Procedure

1. Increase maxmemory if RAM is available.
2. Configure an eviction policy to age out cold keys.
"""

    async def test_a_duplicate_id_on_disk_is_skipped_and_the_rest_are_minted(
        self, service, session_factory, tmp_path
    ):
        scope_dir: Path = tmp_path / "data/knowledge/global/database"
        scope_dir.mkdir(parents=True)
        # Two files carrying the SAME frontmatter id, plus one distinct file.
        (scope_dir / "a.md").write_text(self._runbook("redis-oom", "Redis OOM A"))
        (scope_dir / "b.md").write_text(self._runbook("redis-oom", "Redis OOM B"))
        (scope_dir / "c.md").write_text(self._runbook("redis-slow", "Redis Slow"))

        result = await service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        minted = {d["runbook_id"] for d in result["drafts"]}
        assert "redis-oom" in minted
        # The walk continued past the refusal — this is the whole claim.
        assert "redis-slow" in minted, result
        assert result["discovered"] == 2, result
        assert any("already held" in e for e in result["errors"]), result["errors"]
        assert await _live_draft_count(session_factory, "redis-oom") == 1
