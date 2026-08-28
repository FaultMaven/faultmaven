"""extract → review → approve, driven through the REAL routes (#1214).

``app.state.suggestion_service`` was read in two places and written in none, so
``get_suggestion_service`` always fell through to a throwaway
``SuggestionService()``: the suggestion an extract stored lived in that
instance's private dict and the approve that followed built a DIFFERENT empty
instance and answered **404**. The whole write side of the knowledge flywheel
could not complete, and every unit test passed, because each one held its own
service object and never asked whether the app had one.

So this file wires the app the way the composition root does — ONE
``SuggestionService`` holding a real ``KnowledgeService`` on ``app.state`` — and
drives the loop over HTTP:

1. ``POST /cases/{id}/extract-knowledge`` creates the suggestion;
2. ``GET /knowledge/suggestions/{id}`` finds it — the request-to-request
   survival that was the actual defect;
3. approving the LLM-shaped draft is REFUSED 422 by the runbook quality gate,
   which now runs inside ``upload_document`` rather than only at the upload
   route, and publishes NOTHING;
4. the reviewer edits it into a valid runbook and approval succeeds, writing a
   real ``knowledge_items`` row at the platform tier;
5. re-approval is 409 (#1211, not re-litigated here — pinned so this lane
   cannot regress it).

It also reaches the ``400 "PII scan not complete"`` path from #1200, which was
unreachable while the 404 fired first: with the service wired, a suggestion
whose scan has not completed is now the one thing that 400 is about.

Runs against in-memory SQLite with the ChromaDB half mocked — the two stores'
divergence is ``ingest_runbook``'s business, not this file's.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    require_authentication,
    require_platform_admin,
)
from faultmaven.api.v1.dependencies import get_case_service
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    KnowledgeItemModel,
    OrganizationModel,
)
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.case.api.routes import router as case_router
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from tests.runbook_samples import valid_runbook

pytestmark = pytest.mark.integration

DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
CASE_ID = "case_aabb11223344"
ADMIN_ID = "user-admin"


# ---------------------------------------------------------------------------
# The app, wired the way the composition root wires it
# ---------------------------------------------------------------------------


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


def _knowledge_service(session_factory) -> KnowledgeService:
    """A REAL KnowledgeService — the point of this file.

    A double would let the quality gate and the publish be asserted only as
    "was called", which is exactly the kind of proof that let a dead seam ship.
    Only the ChromaDB half is stubbed (no BGE-M3, no vector server); the SQLite
    writes, the on-disk runbook, and the gate all run for real.
    """
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=5)
    service._remove_from_vector_store = AsyncMock(return_value=None)
    return service


class _Case:
    case_id = CASE_ID
    title = "Connection pool exhaustion"
    description = "Prod DB latency spike"
    organization_id = STANDALONE_ORG_ID


def _admin() -> DevUser:
    return DevUser(
        user_id=ADMIN_ID,
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        created_at=datetime.now(timezone.utc),
        roles=["admin", "platform_admin"],
        organization_id=STANDALONE_ORG_ID,
    )


@pytest.fixture
def wired(session_factory, tmp_path, monkeypatch):
    """The app, plus handles on the singletons the composition root would set.

    ``app.state.suggestion_service`` is assigned ONCE, exactly as main.py does
    it — no dependency_override for ``get_suggestion_service``, because the
    thing under test is that the real dependency finds a real instance.
    """
    monkeypatch.chdir(tmp_path)  # data/knowledge/ is created under the tmp dir

    knowledge_service = _knowledge_service(session_factory)
    suggestion_service = SuggestionService(
        case_repository=None,
        knowledge_service=knowledge_service,
        sanitizer=None,  # no PII engine in this deployment shape → scan CLEAN
        llm_provider=None,  # extraction falls back to its template
    )

    app = FastAPI()
    app.include_router(case_router)
    app.include_router(knowledge_router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)

    app.state.knowledge_service = knowledge_service
    app.state.suggestion_service = suggestion_service

    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_Case())
    app.dependency_overrides[get_case_service] = lambda: case_service
    app.dependency_overrides[require_authentication] = _admin
    app.dependency_overrides[require_platform_admin] = _admin

    client = TestClient(app, raise_server_exceptions=False)
    return client, app, knowledge_service, suggestion_service


async def _knowledge_rows(session_factory) -> list[Any]:
    async with session_factory() as session:
        result = await session.execute(select(KnowledgeItemModel))
        return list(result.scalars().all())


def _extract(client) -> str:
    resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["suggestion_id"]


# ---------------------------------------------------------------------------
# 1-2. The suggestion survives the request that made it
# ---------------------------------------------------------------------------


class TestTheSuggestionSurvivesTheRequest:
    def test_a_suggestion_extracted_by_one_request_is_found_by_the_next(self, wired):
        """THE defect. Pre-fix each request built its own empty service, so the
        GET below answered 404 for an id the POST had just returned."""
        client = wired[0]

        suggestion_id = _extract(client)

        resp = client.get(f"/knowledge/suggestions/{suggestion_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["suggestion_id"] == suggestion_id
        assert resp.json()["case_id"] == CASE_ID

    def test_the_route_hands_out_the_composition_roots_instance(self, wired):
        """Not merely 'an instance' — the one app.state holds, on every call."""
        client, app, _knowledge, suggestion_service = wired

        first = _extract(client)
        second = _extract(client)

        assert app.state.suggestion_service is suggestion_service
        assert {first, second} <= set(suggestion_service._suggestions_store)

    def test_it_lists_through_the_review_inbox(self, wired):
        client = wired[0]
        suggestion_id = _extract(client)

        resp = client.get("/knowledge/suggestions")
        assert resp.status_code == 200, resp.text
        ids = [s["suggestion_id"] for s in resp.json()["suggestions"]]
        assert suggestion_id in ids


# ---------------------------------------------------------------------------
# 3. The quality gate refuses LLM-shaped content, and publishes nothing
# ---------------------------------------------------------------------------


class TestTheQualityGate:
    async def test_extracted_markdown_is_refused_and_nothing_is_published(
        self, wired, session_factory
    ):
        """The PM decision: LLM-extracted content may not enter the corpus
        without the runbook gate. The extraction template is
        ``## Problem / ## Root Cause / ## Solution / ## Prevention`` — no
        frontmatter, none of the six required sections — so approval refuses."""
        client = wired[0]
        suggestion_id = _extract(client)

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "runbook quality standards" in detail
        assert "No YAML frontmatter found" in detail
        assert await _knowledge_rows(session_factory) == []

    async def test_a_refusal_leaves_the_suggestion_approvable(
        self, wired, session_factory
    ):
        """A refused approval is not a state change: the suggestion stays
        pending and unlinked, so editing and re-approving is the workflow."""
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)

        client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        stored = suggestion_service._suggestions_store[suggestion_id]
        assert stored.status.value == "pending_review"
        assert stored.knowledge_item_id is None


# ---------------------------------------------------------------------------
# 4-5. Review, approve, and the re-approval conflict
# ---------------------------------------------------------------------------


def _make_reviewable(client, suggestion_id: str) -> None:
    """The REVIEW step: the admin edits the draft into a valid runbook.

    The edit resets the PII scan, and ``update_suggestion`` re-scans, so the
    suggestion comes back ready for review — which is why approval works after
    this and not before.
    """
    resp = client.put(
        f"/knowledge/suggestions/{suggestion_id}",
        json={
            "title": "Connection Pool Exhaustion Runbook",
            "content": valid_runbook("Connection Pool Exhaustion Runbook"),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pii_scan_status"] == "clean"


class TestTheApprovalThatActuallyPublishes:
    async def test_the_full_loop_writes_a_platform_tier_knowledge_item(
        self, wired, session_factory
    ):
        client = wired[0]
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 201, resp.text
        item_id = resp.json()["knowledge_item_id"]
        assert item_id, "approval reported success without an id"

        rows = await _knowledge_rows(session_factory)
        assert [r.item_id for r in rows] == [item_id]
        # The platform tier, and the shape migration 033's CHECK requires of it:
        # a global row carries NO organization_id.
        assert rows[0].scope == "global"
        assert rows[0].organization_id is None

    def test_the_link_is_recorded_on_the_suggestion(self, wired):
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)

        item_id = client.post(
            f"/knowledge/suggestions/{suggestion_id}/approve", json={}
        ).json()["knowledge_item_id"]

        stored = suggestion_service._suggestions_store[suggestion_id]
        assert stored.status.value == "approved"
        assert stored.knowledge_item_id == item_id
        assert stored.reviewed_by == ADMIN_ID

    async def test_re_approval_is_a_409_and_publishes_nothing_more(
        self, wired, session_factory
    ):
        """#1211's guard, exercised on the path that can finally reach it."""
        client = wired[0]
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)
        assert (
            client.post(
                f"/knowledge/suggestions/{suggestion_id}/approve", json={}
            ).status_code
            == 201
        )

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 409, resp.text
        assert len(await _knowledge_rows(session_factory)) == 1


# ---------------------------------------------------------------------------
# The #1200 400 path, reachable for the first time
# ---------------------------------------------------------------------------


class TestTheNotReadyPathIsNowReachable:
    def test_an_unscanned_suggestion_is_a_400_about_pii(self, wired):
        """#1200 documented this 400 as misleading; #1211 made it truthful. It
        was still unreachable — ``get_suggestion_visible`` returned None first
        and the route answered 404. With the service wired it is reachable, and
        this asserts what it ACTUALLY returns."""
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)

        # The state a content edit leaves behind before its re-scan lands, and
        # the state a failed scan leaves permanently.
        from faultmaven.modules.knowledge.domain.models.suggestion import (
            PIIScanStatus,
        )

        suggestion_service._suggestions_store[suggestion_id].pii_scan_status = (
            PIIScanStatus.SCAN_FAILED
        )

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Cannot approve: PII scan not complete"

    def test_an_absent_id_is_still_a_404(self, wired):
        """The 404 the whole feature used to give for a real id must still be
        the answer for an id that genuinely does not exist."""
        client = wired[0]

        resp = client.post("/knowledge/suggestions/sug_nope/approve", json={})

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Compensation: a failure after the publish must not leave an orphan
# ---------------------------------------------------------------------------


class TestTheCompensatingDelete:
    async def test_a_failure_after_publish_rolls_the_knowledge_item_back(
        self, wired, session_factory
    ):
        """``suggestion.approve()`` re-checks readiness and a concurrent edit
        resets the scan, so this raises AFTER ``upload_document`` has written a
        row, its vectors and a file. Without compensation the corpus keeps a
        runbook nothing links to while the client is told the approval failed.
        """
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)

        suggestion = suggestion_service._suggestions_store[suggestion_id]

        def _raise(**_kwargs: Any) -> None:
            raise RuntimeError("concurrent edit reset the scan")

        suggestion.approve = _raise

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 500
        assert (
            await _knowledge_rows(session_factory) == []
        ), "the knowledge base kept an item the failed approval published"


# ---------------------------------------------------------------------------
# The service must be present, or the routes say so
# ---------------------------------------------------------------------------


class TestAnUnwiredDeploymentSaysSo:
    """The composition root can leave the slot empty (it logs and continues, as
    it does for the knowledge service). Both routes must then refuse rather
    than fabricate a service — the fabrication IS the bug."""

    def _bare_app(self) -> TestClient:
        app = FastAPI()
        app.include_router(case_router)
        app.include_router(knowledge_router)
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        case_service = MagicMock()
        case_service.get_case = AsyncMock(return_value=_Case())
        app.dependency_overrides[get_case_service] = lambda: case_service
        app.dependency_overrides[require_authentication] = _admin
        app.dependency_overrides[require_platform_admin] = _admin
        return TestClient(app, raise_server_exceptions=False)

    def test_extract_answers_503(self):
        resp = self._bare_app().post(f"/cases/{CASE_ID}/extract-knowledge", json={})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Knowledge suggestions unavailable"

    def test_the_review_inbox_answers_503(self):
        resp = self._bare_app().get("/knowledge/suggestions")
        assert resp.status_code == 503

    def test_approve_answers_503(self):
        resp = self._bare_app().post("/knowledge/suggestions/sug_1/approve", json={})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Health of this file's own fixtures
# ---------------------------------------------------------------------------


def test_the_wiring_fixture_is_not_vacuous(wired):
    """A broken fixture would make every error pin above pass for the wrong
    reason. This asserts the happy path reaches a 201 through the same client.
    """
    client = wired[0]
    suggestion_id = _extract(client)
    _make_reviewable(client, suggestion_id)
    resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})
    assert resp.status_code == 201, resp.text
