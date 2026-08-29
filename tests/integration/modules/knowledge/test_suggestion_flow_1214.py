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
unreachable while the 404 fired first. With the service wired it is reachable,
and it is now reserved for a suggestion that genuinely needs a human: detected
PII. A *failed* scan is retried on approval instead (a transient PII-engine
fault is not a verdict about the content), so it is no longer a dead end.

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
from faultmaven.api.v1.dependencies import (
    get_suggestion_service as shared_get_suggestion_service,
)
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
from faultmaven.modules.knowledge.domain.models.suggestion import KnowledgeSuggestion
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    DatabaseSuggestionRepository,
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
        # The REAL store, over the same in-memory SQLite the knowledge service
        # writes to (#1227). A dict here would leave the store the one part of
        # this flow still proved only by a double — and it is the part the
        # cross-worker defect lives in.
        suggestion_repository=DatabaseSuggestionRepository(session_factory),
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

    async def test_the_route_hands_out_the_composition_roots_instance(self, wired):
        """Not merely 'an instance' — the one app.state holds, on every call."""
        client, app, _knowledge, suggestion_service = wired

        first = _extract(client)
        second = _extract(client)

        assert app.state.suggestion_service is suggestion_service
        assert await suggestion_service.get_suggestion(first) is not None
        assert await suggestion_service.get_suggestion(second) is not None

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
        without the runbook gate.

        No LLM is wired here, so extraction falls back to the skeleton
        template. Since #1226 that skeleton is v4-SHAPED — frontmatter, the six
        required sections, the ``[Default]`` fallback Cause — because that is
        the schema the reviewer has to fill in. It is still refused, on the one
        thing that is genuinely absent: Cause A has no Statement, because
        nothing was extracted to state. A skeleton the gate accepted would let
        one click publish a blank form into the global corpus.
        """
        client = wired[0]
        suggestion_id = _extract(client)

        resp = client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        # The STRUCTURED body, not a flattened string: approval renders the
        # single gate's refusal through the same helper the upload route uses,
        # so a reviewer gets per-error detail and the authoring help.
        assert detail["message"] == "Runbook does not meet quality standards"
        assert "Cause A: **Statement:** sub-field is empty" in detail["errors"]
        assert "YAML frontmatter" in detail["help"]
        assert await _knowledge_rows(session_factory) == []

    async def test_the_reviewer_sees_the_refusal_before_pressing_approve(self, wired):
        """#1226: the same verdict, on the suggestion, at review time.

        Before this the ONLY way to learn a draft was unpublishable was to
        press approve and read a 422, which is not a review affordance."""
        client = wired[0]
        suggestion_id = _extract(client)

        detail = client.get(f"/knowledge/suggestions/{suggestion_id}").json()

        assert detail["validation"]["passed"] is False
        assert (
            "Cause A: **Statement:** sub-field is empty"
            in detail["validation"]["errors"]
        )

    async def test_upload_and_approve_render_the_same_refusal(self, wired):
        """One gate, one rendering. These two routes refuse for the same reason
        and used to describe it in two different shapes — the upload route's
        structured body and the global handler's flattened ``str(exc)``."""
        client = wired[0]
        suggestion_id = _extract(client)

        approve = client.post(
            f"/knowledge/suggestions/{suggestion_id}/approve", json={}
        )
        upload = client.post(
            "/knowledge/documents",
            data={"title": "Not A Runbook", "document_type": "runbook"},
            files={"file": ("doc.md", b"# just a heading\n", "text/markdown")},
        )

        assert approve.status_code == upload.status_code == 422
        assert set(approve.json()["detail"]) == set(upload.json()["detail"])
        assert approve.json()["detail"]["help"] == upload.json()["detail"]["help"]

    async def test_a_refusal_leaves_the_suggestion_approvable(
        self, wired, session_factory
    ):
        """A refused approval is not a state change: the suggestion stays
        pending and unlinked, so editing and re-approving is the workflow."""
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)

        client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={})

        stored = await suggestion_service.get_suggestion(suggestion_id)
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

    async def test_the_link_is_recorded_on_the_suggestion(self, wired):
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)

        item_id = client.post(
            f"/knowledge/suggestions/{suggestion_id}/approve", json={}
        ).json()["knowledge_item_id"]

        stored = await suggestion_service.get_suggestion(suggestion_id)
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
    async def test_a_suggestion_with_detected_pii_is_a_400_about_pii(self, wired):
        """#1200 documented this 400 as misleading; #1211 made it truthful. It
        was still unreachable — ``get_suggestion_visible`` returned None first
        and the route answered 404. With the service wired it is reachable, and
        this asserts what it ACTUALLY returns.

        PII_DETECTED, not SCAN_FAILED: a failed scan is now retried on approval
        (a transient engine fault is not a verdict about the content), so it is
        no longer a state that stays not-ready. Detected PII is — it needs a
        human to remediate, which is the point of the gate.
        """
        client, _app, _knowledge, suggestion_service = wired
        suggestion_id = _extract(client)

        from faultmaven.modules.knowledge.domain.models.suggestion import (
            PIIScanStatus,
        )

        # Load, mutate, save — the store is the database now, so poking the
        # object a read handed back changes nothing (#1227).
        stored = await suggestion_service.get_suggestion(suggestion_id)
        stored.pii_scan_status = PIIScanStatus.PII_DETECTED
        await suggestion_service._repository.save(stored)

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
        self, wired, session_factory, monkeypatch
    ):
        """``suggestion.approve()`` re-checks readiness and a concurrent edit
        resets the scan, so this raises AFTER ``upload_document`` has written a
        row, its vectors and a file. Without compensation the corpus keeps a
        runbook nothing links to while the client is told the approval failed.
        """
        client, _app, _knowledge, _suggestion_service = wired
        suggestion_id = _extract(client)
        _make_reviewable(client, suggestion_id)

        def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("concurrent edit reset the scan")

        # On the CLASS: the approval loads its own instance out of the database,
        # so an attribute set on some other copy of the row would never be seen
        # and this test would silently assert the happy path (#1227).
        monkeypatch.setattr(KnowledgeSuggestion, "approve", _raise, raising=True)

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

    def test_the_extract_route_resolves_it_as_an_overridable_dependency(self):
        """It used to be an inline ``app.state`` lookup guarded by a
        ``request: Request = None`` default FastAPI never passes, so the route
        could not be overridden in a test the way every sibling can. Now it
        takes Depends(get_suggestion_service) — the SAME shared dependency the
        knowledge-side routes use, so the 503 policy exists once."""
        app = FastAPI()
        app.include_router(case_router)
        case_service = MagicMock()
        case_service.get_case = AsyncMock(return_value=_Case())
        app.dependency_overrides[get_case_service] = lambda: case_service
        app.dependency_overrides[require_authentication] = _admin

        stub = MagicMock()
        stub.extract_knowledge_from_case = AsyncMock(
            side_effect=AssertionError("the override was not honoured")
        )
        app.dependency_overrides[shared_get_suggestion_service] = lambda: stub

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 500  # the override ran and raised
        stub.extract_knowledge_from_case.assert_awaited_once()


class TestAFullReviewInboxRefusesHonestly:
    """The store is bounded (#1214 review) and never evicts unreviewed work, so
    a queue full of pending reviews has to refuse rather than silently drop
    something a reviewer has not seen. Since #1227 the ceiling is per
    organization, over the durable table."""

    async def test_extract_answers_503_when_the_queue_is_full_of_pending_reviews(
        self, wired
    ):
        client, _app, _knowledge, suggestion_service = wired
        suggestion_service._max_unreviewed_suggestions = 1
        _extract(client)  # fills the single slot with a PENDING_REVIEW entry

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 503, resp.text
        assert "queue is full" in resp.json()["detail"]
        assert (
            await suggestion_service._repository.count_for_organization(
                STANDALONE_ORG_ID
            )
            == 1
        )


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
