"""A case delete erases the answers the replay cache holds for it (#2624):
the copilot keys case creation, and a create retried after its case was
deleted was answered with the deleted case.

Built as ``test_kb_request_receipts_2433.py`` builds its app, since the
deployed app mounts no middleware under ``SKIP_SERVICE_CHECKS``: the real case
router and the ``CaseService`` ``create_case_service`` composes for the profile
that mounts it, with ONE FakeRedis held by the middleware and the erasers, as
the composition root's shared client is.
"""

from unittest.mock import AsyncMock

import chromadb
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.idempotency_key import IDEMPOTENCY_REPLAYED_HEADER
from faultmaven.config.settings import get_settings
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.container.providers.services import create_case_service
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.api.routes.cases import router
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.case_erasure import (
    RunbookConversionEraser,
)
from tests.chroma_clients import closed_at_teardown

pytestmark = [pytest.mark.integration]

OWNER = "u_2624"
HEADERS = {
    "Idempotency-Key": "create-2624-aaaaaaaa",
    "Authorization": "Bearer token-2624-aaaaaaaa",
}


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id("ent_2624")
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def test_a_deleted_cases_keyed_create_runs_again(tmp_path, monkeypatch):
    # The conversion eraser's SQL is test_case_deletion_repository_1016's.
    monkeypatch.setattr(
        RunbookConversionEraser, "erase_case", AsyncMock(return_value=0)
    )
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    settings = get_settings().model_copy(deep=True)
    settings.server.skip_service_checks = False
    chroma = closed_at_teardown(chromadb.PersistentClient(path=str(tmp_path / "c")))
    service = create_case_service(
        InMemoryCaseRepository(),
        RedisSessionStore(fake, session_ttl_seconds=1800),
        CaseVectorStore(chroma),
        settings,
        file_storage_service=FileStorageService(
            backend=FilesystemStorageBackend(str(tmp_path / "blobs"))
        ),
        redis_client=fake,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    owner = UserDTO(OWNER, OWNER, f"{OWNER}@example.com", OWNER, is_active=True)
    app.dependency_overrides[require_authentication] = lambda: owner
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
    app.dependency_overrides[_di_get_session_service_dependency] = lambda: None
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:

        async def create() -> httpx.Response:
            return await client.post(
                "/api/v1/cases", headers=HEADERS, json={"title": "Disk full"}
            )

        first, replay = await create(), await create()
        case_id = first.json()["case_id"]
        assert first.status_code == replay.status_code == 201
        assert replay.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true"
        assert replay.json()["case_id"] == case_id

        assert (await client.delete(f"/api/v1/cases/{case_id}")).status_code == 204
        again = await create()

    assert again.status_code == 201
    assert IDEMPOTENCY_REPLAYED_HEADER not in again.headers
    assert again.json()["case_id"] != case_id
