"""KB conversion sources store no path, and no conversion response carries one (#836).

``uploaded_files.storage_ref`` is a storage-backend key or NULL (#689). Two KB
writers stored a filesystem path in it, and the conversion API echoed server
paths back to the client: ``source_file.retained_path``, and every draft's
absolute ``file_path``. Each property is driven here through the code that has
it, against a real SQLite schema:

* **Both writers store NULL.** ``KnowledgeService.upload_document``, and the
  disk scan through ``_persist_job``.
* **No conversion response carries a server path.** The routes, end to end:
  ``/scan``, ``GET /conversions/{id}``, ``GET /drafts``,
  ``PUT /conversions/{id}/drafts/{draft_id}``, ``/runbooks/create``, and
  ``/convert``, whose real path needs an LLM, so its service is a double that
  returns the real response models. Every key and every string in each body
  is checked, not only the two field names.
* **The draft's file is still found.** ``conversion_drafts.file_path`` is
  load-bearing (``resolve_runbook_path``, deletion), so it stays on the row and
  on the model. It is only never serialised. After the round trip through the
  routes the row still names the file, and the edit landed in it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    UploadedFileModel,
)
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook

pytestmark = pytest.mark.integration

API = "/api/v1/knowledge"

#: The two fields this issue stops serialising, and the column it stops filling.
_PATH_KEYS = {"retained_path", "file_path", "storage_ref"}


def _leaks(payload, root: Path, where: str = "$") -> list[str]:
    """Every place in ``payload`` that names a server path: a key from
    ``_PATH_KEYS``, or any string holding the knowledge root."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _PATH_KEYS:
                found.append(f"{where}.{key}")
            found += _leaks(value, root, f"{where}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            found += _leaks(item, root, f"{where}[{i}]")
    elif isinstance(payload, str) and (
        str(root) in payload or "data/knowledge" in payload
    ):
        found.append(f"{where} = {payload!r}")
    return found


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                name="Default Enterprise",
                slug="default",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def kb_root(tmp_path, monkeypatch) -> Path:
    """``data/knowledge`` under ``tmp_path``, with one global runbook on disk.

    ``chdir`` because ``knowledge_root()`` is deliberately relative.
    """
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data" / "knowledge"
    (root / "global").mkdir(parents=True)
    (root / "global" / "pool-exhausted.md").write_text(
        valid_runbook("Connection Pool Exhausted On The API"), encoding="utf-8"
    )
    return root


@pytest.fixture
def conversion_service(session_factory, kb_root):
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    service = ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=session_factory,
    )
    with patch.object(type(service), "_data_dir", new=property(lambda self: kb_root)):
        yield service


def _operator() -> DevUser:
    """A platform operator: global-scope drafts are theirs to scan and edit."""
    return DevUser(
        user_id="user-op",
        username="operator",
        email="operator@example.com",
        display_name="Operator",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        roles=["user", "admin", "platform_admin"],
        organization_id=None,
    )


def _client(service) -> AsyncClient:
    """The real conversion router and exception handlers, in the test's loop."""
    from faultmaven.api.exception_handlers import get_exception_handlers

    app = FastAPI()
    app.include_router(cr.router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    user = _operator()
    app.dependency_overrides[cr._require_auth] = lambda: user
    app.dependency_overrides[cr._get_conversion_service] = lambda: service
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _upload_rows(session_factory) -> list[UploadedFileModel]:
    async with session_factory() as session:
        return list((await session.execute(select(UploadedFileModel))).scalars())


async def _draft_row(session_factory, draft_id: str) -> ConversionDraftModel:
    async with session_factory() as session:
        return await session.get(ConversionDraftModel, draft_id)


# ---------------------------------------------------------------------------
# Both writers store NULL
# ---------------------------------------------------------------------------


class TestBothWritersStoreNoPath:
    async def test_upload_document_stores_no_path(self, session_factory, kb_root):
        service = KnowledgeService(
            knowledge_ingester=MagicMock(),
            sanitizer=MagicMock(),
            tracer=MagicMock(),
            vector_store=MagicMock(),
            db_session_factory=session_factory,
        )
        service._index_document_in_vector_store = AsyncMock(return_value=3)

        result = await service.upload_document(
            content=valid_runbook("Redis Evictions Under Memory Pressure"),
            title="Redis Evictions",
            document_type="runbook",
            scope="global",
            owner_id="user-op",
        )
        assert result["status"] == "completed", result

        (upload,) = await _upload_rows(session_factory)
        assert (upload.upload_source, upload.case_id) == ("conversion_source", None)
        assert upload.storage_ref is None
        # What locates the runbook is the draft row, and the file is there.
        async with session_factory() as session:
            (draft,) = (await session.execute(select(ConversionDraftModel))).scalars()
        assert Path(draft.file_path).is_file(), draft.file_path

    async def test_the_disk_scan_stores_no_path(
        self, conversion_service, session_factory
    ):
        result = await conversion_service.scan_for_runbooks(
            user_id=None, enterprise_id=None, is_platform_admin=True
        )
        assert result["discovered"] == 1, result

        (upload,) = await _upload_rows(session_factory)
        assert (upload.upload_source, upload.case_id) == ("conversion_source", None)
        assert upload.storage_ref is None


# ---------------------------------------------------------------------------
# No conversion response carries a server path
# ---------------------------------------------------------------------------


class TestNoConversionResponseCarriesAPath:
    async def test_scan_and_every_read_and_edit_of_its_draft(
        self, conversion_service, session_factory, kb_root
    ):
        on_disk = kb_root / "global" / "pool-exhausted.md"
        async with _client(conversion_service) as client:
            scanned = await client.post(f"{API}/scan")
            assert scanned.status_code == 200, scanned.text
            body = scanned.json()
            assert body["discovered"] == 1, body
            assert _leaks(body, kb_root) == []
            (found,) = body["drafts"]
            conversion_id, draft_id = found["conversion_id"], found["draft_id"]

            fetched = await client.get(f"{API}/conversions/{conversion_id}")
            assert fetched.status_code == 200, fetched.text
            body = fetched.json()
            assert set(body["source_file"]) == {
                "filename",
                "size_bytes",
                "content_type",
            }
            assert [d["draft_id"] for d in body["drafts"]] == [draft_id]
            assert body["drafts"][0]["content"], "the read must still load the file"
            assert _leaks(body, kb_root) == []

            listed = await client.get(f"{API}/drafts")
            assert listed.status_code == 200, listed.text
            assert [d["draft_id"] for d in listed.json()] == [draft_id]
            assert _leaks(listed.json(), kb_root) == []

            edited_content = valid_runbook("Connection Pool Exhausted Edited Body")
            edited = await client.put(
                f"{API}/conversions/{conversion_id}/drafts/{draft_id}",
                json={"content": edited_content},
            )
            assert edited.status_code == 200, edited.text
            assert _leaks(edited.json(), kb_root) == []

        # The round trip changed what the client sees, not what the row holds:
        # it still names the file, and the edit landed in it.
        row = await _draft_row(session_factory, draft_id)
        assert row.file_path == str(on_disk)
        assert on_disk.read_text(encoding="utf-8") == edited_content

    async def test_runbook_create(self, conversion_service, session_factory, kb_root):
        async with _client(conversion_service) as client:
            created = await client.post(
                f"{API}/runbooks/create",
                json={
                    "title": "Disk Full On The Ingest Nodes",
                    "domain": "infrastructure",
                    "service": "ingest",
                    "symptom_class": ["availability"],
                    "severity": "high",
                    "scope": "personal",
                    "symptom_recognition": "Writes fail with ENOSPC on every node.",
                    "applicability": "Any ingest node with a local spool disk.",
                    "diagnostic_steps": "### Step 1. Check free space with df -h",
                    "causes": "### Cause A: Spool growth\nStatement: the spool grew.",
                    "prevention": "Alert on disk usage above eighty percent.",
                },
            )
        assert created.status_code == 201, created.text
        body = created.json()
        assert _leaks(body, kb_root) == []

        row = await _draft_row(session_factory, body["draft"]["draft_id"])
        assert Path(row.file_path).is_file(), row.file_path

    async def test_convert(self, tmp_path):
        """The real ``/convert`` path needs an LLM, so its service is a double
        returning the real response models, carrying a real server path in the
        draft as the service does. The route's own dump must drop it."""
        server_path = tmp_path / "data" / "knowledge" / "user_u" / "nginx-502.md"
        draft = ConversionDraft(
            draft_id="draft_836",
            runbook_id="nginx-502",
            title="Nginx 502 Bad Gateway",
            scope="personal",
            status=DraftStatus.DRAFT,
            validation=ValidationResult(passed=True),
            quality_score=QualityScore(
                overall=80.0,
                grade="B",
                completeness=80.0,
                clarity=80.0,
                actionability=80.0,
                comprehensiveness=80.0,
            ),
            file_path=str(server_path),
            content_preview="---\nid: nginx-502\n---",
        )
        assert draft.file_path == str(server_path), "the server still reads it"
        service = MagicMock()
        service.convert_document = AsyncMock(
            return_value=ConversionResponse(
                conversion_id="conv_836",
                status=ConversionStatus.COMPLETED,
                source_file=SourceFileInfo(
                    filename="nginx.txt", size_bytes=12, content_type="text/plain"
                ),
                analysis=AnalysisResult(
                    is_actionable=True,
                    failure_modes=[],
                    source_assessment=SourceAssessment(
                        content_type="troubleshooting_guide",
                        actionability_rating="high",
                        missing_information=[],
                    ),
                ),
                drafts=[draft],
                warnings=[],
                created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
            )
        )
        async with _client(service) as client:
            converted = await client.post(
                f"{API}/convert",
                files={"file": ("nginx.txt", BytesIO(b"nginx 502 ..."), "text/plain")},
                data={"scope": "personal"},
            )
        assert converted.status_code == 201, converted.text
        body = converted.json()
        assert [d["draft_id"] for d in body["drafts"]] == ["draft_836"]
        assert _leaks(body, tmp_path) == []

    async def test_list_drafts_for_case(
        self, conversion_service, session_factory, kb_root
    ):
        """The case surfaces (the case UI's runbook badges, the Report tab's
        runbook rows) project named fields out of this list, and none of them
        is a path. The list itself no longer holds one either."""
        await conversion_service.scan_for_runbooks(
            user_id=None, enterprise_id=None, is_platform_admin=True
        )
        async with session_factory() as session:
            await session.execute(update(ConversionJobModel).values(case_id="case_836"))
            await session.commit()

        drafts = await conversion_service.list_drafts_for_case("case_836")
        assert len(drafts) == 1, drafts
        assert _leaks(drafts, kb_root) == []
