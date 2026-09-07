"""`KnowledgeService.upload_document` on-disk layout (scope-dir routing).

Runbook source files must land in the canonical flat scope layout the scan
pass infers scope from — ``global/``, ``team_{id}/``, ``user_{id}/`` — with NO
per-domain subdirectory (domain lives in frontmatter + ChromaDB metadata).

Regression guard: the previous ``data/knowledge/{scope}/{domain}/`` layout
wrote a literal ``personal/`` folder. The scan's scope inference keys off the
``user_``/``team_`` directory prefixes, so a re-discovered personal upload was
mis-inferred as ``global`` (a scope-elevation risk), and the domain
subdirectory violated the documented "flat by scope" rule.

Runs on an in-memory SQLite engine with the ChromaDB half mocked — no backend.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook

DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture(scope="function")
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="function")
async def seeded_session_factory(engine):
    """Session factory with the standalone enterprise + org seeded so the
    conversion-bookkeeping and knowledge_items FKs (organization_id) hold."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Enterprise",
                slug="default",
            )
        )
        session.add(
            OrganizationModel(
                organization_id=STANDALONE_ENTERPRISE_ID,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


def make_service(session_factory, *, chroma_returns: int = 5) -> KnowledgeService:
    """KnowledgeService with a real db_session_factory; the ChromaDB inner call
    is mocked so upload_document runs without booting BGE-M3/ChromaDB."""
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=chroma_returns)
    return service


@pytest.mark.asyncio
async def test_personal_upload_lands_in_user_dir_flat(
    seeded_session_factory, tmp_path, monkeypatch
):
    """personal scope -> data/knowledge/user_{owner_id}/ (not personal/, no domain subdir)."""
    monkeypatch.chdir(tmp_path)
    service = make_service(seeded_session_factory)

    await service.upload_document(
        # Gate-passing content (#1214): upload_document validates before it
        # writes, so a non-runbook body never reaches the layout logic this
        # test is about.
        content=valid_runbook("Redis Notes For The Cache Tier"),
        title="Redis Notes",
        document_type="runbook",
        scope="personal",
        owner_id="user-42",
    )

    kb_root = tmp_path / "data" / "knowledge"
    written = list(kb_root.rglob("*.md"))
    assert len(written) == 1, f"expected one runbook file, found {written}"
    assert written[0].parent == kb_root / "user_user-42"
    # The literal-scope folder and the domain subdirectory must NOT appear.
    assert not (kb_root / "personal").exists()
    assert not (kb_root / "database").exists()


@pytest.mark.asyncio
async def test_global_upload_lands_in_global_dir_flat(
    seeded_session_factory, tmp_path, monkeypatch
):
    """global scope -> data/knowledge/global/ (no domain subdir)."""
    monkeypatch.chdir(tmp_path)
    service = make_service(seeded_session_factory)

    await service.upload_document(
        content=valid_runbook("K8s Guide For The Control Plane"),
        title="K8s Guide",
        document_type="runbook",
        scope="global",
        owner_id="user-1",
    )

    kb_root = tmp_path / "data" / "knowledge"
    written = list(kb_root.rglob("*.md"))
    assert len(written) == 1, f"expected one runbook file, found {written}"
    assert written[0].parent == kb_root / "global"
    assert not (kb_root / "orchestration").exists()
