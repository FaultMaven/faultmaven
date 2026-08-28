"""A failed approval must leave NOTHING behind (#1214 review).

The first version of the compensation called ``delete_document``, which removes
the published item — the ``knowledge_items`` row, its shares and its ChromaDB
chunks. That is the whole of what a deletion means and NOT the whole of what
``upload_document`` wrote. Measured on that version, after a post-publish
failure:

* the runbook markdown was still on disk;
* the ``uploaded_files``, ``conversion_jobs`` and ``conversion_drafts`` rows all
  survived, the draft with ``status="verified"``, ``validation_passed=True`` and
  ``knowledge_item_id`` pointing at the id that had just been deleted;
* and the residue was PERMANENT rather than self-healing — the surviving draft
  keeps its ``file_path`` in ``scan_for_runbooks``'s ``tracked_paths``, so the
  reconciliation scan SKIPS the orphaned file (``discovered=0, skipped=1``;
  with the row removed the same file is re-discovered, ``discovered=1``).

So the artifact was a stale "verified" draft pointing at a deleted item, listed
by ``GET /knowledge/drafts``, counted by ``get_document_statistics``, holding a
file on disk forever.

These tests drive the REAL ``upload_document`` against a real SQLite schema
(only the vector layer is mocked) and assert the post-rollback state is clean in
every store — including through ``get_document_statistics``, which is what an
operator actually looks at.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    OrganizationModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook

pytestmark = pytest.mark.integration

DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
                organization_id=STANDALONE_ORG_ID,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def service(session_factory, tmp_path, monkeypatch) -> KnowledgeService:
    """A real KnowledgeService with only the vector layer stubbed."""
    monkeypatch.chdir(tmp_path)
    svc = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    svc._index_document_in_vector_store = AsyncMock(return_value=3)
    svc._remove_from_vector_store = AsyncMock(return_value=None)
    return svc


async def _counts(session_factory) -> dict:
    async with session_factory() as session:
        return {
            "items": len(
                (await session.execute(select(KnowledgeItemModel))).scalars().all()
            ),
            "drafts": len(
                (await session.execute(select(ConversionDraftModel))).scalars().all()
            ),
            "jobs": len(
                (await session.execute(select(ConversionJobModel))).scalars().all()
            ),
            "uploads": len(
                (await session.execute(select(UploadedFileModel))).scalars().all()
            ),
        }


async def _publish(service: KnowledgeService) -> str:
    result = await service.upload_document(
        content=valid_runbook("Rollback Completeness Sample"),
        title="Rollback Completeness Sample",
        document_type="runbook",
        scope="global",
        owner_id="user-admin",
    )
    return result["document_id"]


class TestThePublishWroteEverythingWeThinkItDid:
    """If this fails the rollback tests below are asserting nothing."""

    async def test_an_upload_writes_four_rows_and_a_file(
        self, service, session_factory, tmp_path
    ):
        await _publish(service)

        assert await _counts(session_factory) == {
            "items": 1,
            "drafts": 1,
            "jobs": 1,
            "uploads": 1,
        }
        assert len(list((tmp_path / "data" / "knowledge").rglob("*.md"))) == 1

    async def test_the_draft_claims_verified_against_the_item(
        self, service, session_factory
    ):
        """The claim that makes a partial rollback damaging rather than untidy."""
        document_id = await _publish(service)

        async with session_factory() as session:
            draft = (await session.execute(select(ConversionDraftModel))).scalar_one()
        assert draft.status == "verified"
        assert draft.validation_passed is True
        assert draft.knowledge_item_id == document_id


class TestTheRollbackLeavesNothing:
    async def test_every_row_and_the_file_are_gone(
        self, service, session_factory, tmp_path
    ):
        document_id = await _publish(service)

        result = await service.rollback_uploaded_document(document_id)

        assert result["residue"] == [], result["residue"]
        assert await _counts(session_factory) == {
            "items": 0,
            "drafts": 0,
            "jobs": 0,
            "uploads": 0,
        }
        assert list((tmp_path / "data" / "knowledge").rglob("*.md")) == []

    async def test_the_statistics_an_operator_reads_go_back_to_zero(
        self, service, session_factory
    ):
        """``get_document_statistics`` counts ``conversion_drafts`` rows with
        ``status="verified"`` — exactly the row a partial rollback left behind,
        so the KB reported a document that was not in the corpus."""
        document_id = await _publish(service)
        assert (await service.get_document_statistics())["total_documents"] == 1

        await service.rollback_uploaded_document(document_id)

        assert (await service.get_document_statistics())["total_documents"] == 0

    async def test_the_vectors_are_removed_too(self, service):
        document_id = await _publish(service)

        await service.rollback_uploaded_document(document_id)

        service._remove_from_vector_store.assert_awaited_once_with(document_id)


class TestResidueIsReportedTruthfully:
    async def test_a_failed_vector_delete_names_chromadb_not_the_row(
        self, service, session_factory
    ):
        """``delete_document`` deletes the SQL row and THEN the vectors, so a
        raise there leaves the inverse of the obvious guess: the row is gone and
        the chunks remain, retrievable with nothing listing them. The message is
        derived from probing the row, not assumed."""
        document_id = await _publish(service)
        service._remove_from_vector_store = AsyncMock(
            side_effect=RuntimeError("chromadb unreachable")
        )

        result = await service.rollback_uploaded_document(document_id)

        assert len(result["residue"]) == 1
        assert "ChromaDB chunks" in result["residue"][0]
        assert "inventory row was deleted" in result["residue"][0]
        # ...and it is TRUE: the row really is gone.
        assert (await _counts(session_factory))["items"] == 0

    async def test_a_failed_row_delete_names_the_row(self, service):
        document_id = await _publish(service)
        service.delete_document = AsyncMock(
            side_effect=RuntimeError("database is locked")
        )
        service._knowledge_item_exists = AsyncMock(return_value=True)

        result = await service.rollback_uploaded_document(document_id)

        assert any("knowledge_items row" in item for item in result["residue"])
        assert not any("ChromaDB chunks" in item for item in result["residue"])

    async def test_bookkeeping_is_still_cleaned_when_the_item_delete_fails(
        self, service, session_factory
    ):
        """Each step is independent: one store failing must not strand the
        others, or a single transient error re-creates the whole defect."""
        document_id = await _publish(service)
        service.delete_document = AsyncMock(
            side_effect=RuntimeError("database is locked")
        )

        result = await service.rollback_uploaded_document(document_id)

        assert result["residue"], "the failed item delete should be reported"
        counts = await _counts(session_factory)
        assert counts["drafts"] == 0
        assert counts["jobs"] == 0
        assert counts["uploads"] == 0

    async def test_a_file_outside_the_knowledge_tree_is_refused_not_deleted(
        self, service, session_factory, tmp_path
    ):
        """``file_path`` is read back out of the database, so it is treated as
        untrusted. Containment is anchored on the knowledge ROOT — anchoring on
        the file's own directory would be circular."""
        document_id = await _publish(service)
        outside = tmp_path / "not-the-kb.md"
        outside.write_text("# elsewhere\n")
        async with session_factory() as session:
            draft = (await session.execute(select(ConversionDraftModel))).scalar_one()
            draft.file_path = str(outside)
            await session.commit()

        result = await service.rollback_uploaded_document(document_id)

        assert outside.exists(), "a path outside the knowledge tree was deleted"
        assert any("outside" in item for item in result["residue"])


class TestTheGateRunsExactlyOnce:
    async def test_one_validation_pass_per_accepted_upload(self, service, monkeypatch):
        """The route used to validate and then ``upload_document`` validated
        again — measured at 31.5 ms per pass on the largest shipped runbook
        (48 KB), so ~63 ms of event-loop-blocking CPU for every upload. The
        route's copy is gone; this pins that the service does not grow a second
        one either."""
        import faultmaven.modules.knowledge.domain.services.knowledge_service as ks_mod

        calls = []
        real = ks_mod.enforce_runbook_quality

        def counting(content: str) -> None:
            calls.append(content)
            return real(content)

        monkeypatch.setattr(ks_mod, "enforce_runbook_quality", counting)

        await _publish(service)

        assert len(calls) == 1, f"gate ran {len(calls)} times for one upload"
