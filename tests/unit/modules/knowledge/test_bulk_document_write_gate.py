"""#866: per-document write policy on the bulk KB document routes.

``POST /knowledge/documents/bulk-update`` and ``bulk-delete`` were the ungated
twins of the routes #834 gated: ``require_platform_admin`` and no per-document
check, so the single-document policy could be bypassed wholesale (and owners
were locked out of a surface that is semantically just a loop over their own
gated writes).

Both routes now authenticate any caller and run
``ensure_document_write_allowed`` per target before anything reaches the
service — only permitted ids are passed on. Refusals carry no existence
oracle: a target the caller cannot see reports the same string as an absent
one. The 500 handler no longer echoes ``str(e)``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

BULK_UPDATE = "/knowledge/documents/bulk-update"
BULK_DELETE = "/knowledge/documents/bulk-delete"

# The batch used across the mixed-authorization cases.
OWN = "own"  # caller's personal runbook       → permitted
OTHERS = "others"  # another user's personal runbook → invisible ⇒ "not found"
GLOBAL = "globaldoc"  # platform corpus            → visible ⇒ "not authorized"
ABSENT = "absent"  # no such row                 → "not found"
BATCH = [OWN, OTHERS, GLOBAL, ABSENT]


def _user(*, user_id="u1", roles=("user",)) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
        roles=list(roles),
    )


def _doc(doc_id, *, scope, owner_id):
    return {
        "document_id": doc_id,
        "title": "T",
        "content": "C",
        "document_type": "runbook",
        "tags": [],
        "scope": scope,
        "owner_id": owner_id,
        "source_url": None,
        "created_at": "",
        "updated_at": "",
        "metadata": {},
    }


DOCS = {
    OWN: _doc(OWN, scope="personal", owner_id="u1"),
    OTHERS: _doc(OTHERS, scope="personal", owner_id="someone_else"),
    GLOBAL: _doc(GLOBAL, scope="global", owner_id=None),
}

# What the caller may READ. The global platform tier is readable by everyone;
# another user's personal runbook is not.
VISIBLE = {OWN: DOCS[OWN], GLOBAL: DOCS[GLOBAL]}


def _service():
    """Knowledge service whose bulk methods report exactly what they receive."""
    service = MagicMock()
    service.get_document = AsyncMock(side_effect=lambda doc_id: DOCS.get(doc_id))
    service.get_document_visible = AsyncMock(
        side_effect=lambda doc_id, user=None, team_ids=None: VISIBLE.get(doc_id)
    )

    async def _bulk_update(document_ids, updates):
        return {
            "success": True,
            "updated_count": len(document_ids),
            "total_requested": len(document_ids),
            "errors": [],
        }

    async def _bulk_delete(document_ids):
        return {
            "success": True,
            "deleted_count": len(document_ids),
            "total_requested": len(document_ids),
            "errors": [],
        }

    service.bulk_update_documents = AsyncMock(side_effect=_bulk_update)
    service.bulk_delete_documents = AsyncMock(side_effect=_bulk_delete)
    return service


def _client(knowledge_service, user):
    """Test client. ``user=None`` exercises the real 401 path."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import (
        get_current_user_optional,
        require_authentication,
    )
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service
    if user is None:
        app.dependency_overrides[get_current_user_optional] = lambda: None
    else:
        app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def single_tenant(monkeypatch):
    monkeypatch.setattr(
        global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
    )


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestBulkRoutesAuthentication:
    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_unauthenticated_is_rejected(self, path):
        service = _service()
        resp = _client(service, None).post(path, json={"document_ids": [OWN]})
        assert resp.status_code == 401
        service.bulk_update_documents.assert_not_awaited()
        service.bulk_delete_documents.assert_not_awaited()

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_empty_batch_is_rejected(self, path, single_tenant):
        service = _service()
        resp = _client(service, _user()).post(path, json={"document_ids": []})
        assert resp.status_code == 400


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestBulkBatchBounds:
    """The per-target loop is bounded and de-duplicated.

    Every target costs a document load, and a refused-but-visible one costs a
    second query — so an unbounded, un-deduplicated batch is both a
    work-amplifier and a timing oracle (repeat an id N times and the
    existing-but-invisible case separates from the absent one even though the
    response strings are identical).
    """

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_over_cap_batch_is_rejected_before_any_work(self, path, single_tenant):
        from faultmaven.modules.knowledge.api.routes import MAX_BULK_DOCUMENT_IDS

        service = _service()
        resp = _client(service, _user()).post(
            path, json={"document_ids": [OWN] * (MAX_BULK_DOCUMENT_IDS + 1)}
        )

        assert resp.status_code == 400
        assert str(MAX_BULK_DOCUMENT_IDS) in resp.json()["detail"]
        service.get_document.assert_not_awaited()
        service.bulk_update_documents.assert_not_awaited()
        service.bulk_delete_documents.assert_not_awaited()

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_cap_applies_to_the_raw_list_not_the_deduped_one(self, path, single_tenant):
        # Otherwise a caller submits an unbounded list and the server pays for
        # the dedupe of it.
        from faultmaven.modules.knowledge.api.routes import MAX_BULK_DOCUMENT_IDS

        service = _service()
        resp = _client(service, _user()).post(
            path, json={"document_ids": [OWN, OTHERS] * MAX_BULK_DOCUMENT_IDS}
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_at_cap_batch_is_accepted(self, path, single_tenant):
        from faultmaven.modules.knowledge.api.routes import MAX_BULK_DOCUMENT_IDS

        service = _service()
        resp = _client(service, _user()).post(
            path, json={"document_ids": [OWN] * MAX_BULK_DOCUMENT_IDS}
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_duplicate_ids_collapse_to_one_evaluation_and_one_error(
        self, path, single_tenant
    ):
        service = _service()
        resp = _client(service, _user(user_id="u1")).post(
            path, json={"document_ids": [OTHERS, OWN, OTHERS, OWN, OTHERS]}
        )

        assert resp.status_code == 200
        body = resp.json()
        # One gate evaluation per unique id — not per occurrence.
        assert service.get_document.await_count == 2
        assert body["errors"] == [f"Document {OTHERS} not found"]
        assert body["total_requested"] == 2
        permitted = (
            service.bulk_update_documents.await_args.kwargs["document_ids"]
            if path == BULK_UPDATE
            else service.bulk_delete_documents.await_args.args[0]
        )
        assert permitted == [OWN]

    @pytest.mark.parametrize("path", [BULK_UPDATE, BULK_DELETE])
    def test_non_string_ids_are_rejected(self, path, single_tenant):
        service = _service()
        resp = _client(service, _user()).post(
            path, json={"document_ids": [{"not": "an id"}]}
        )
        # 400 from the route, or 422 from the declared List[str] body model —
        # either way it never reaches the per-target loop as a 500.
        assert resp.status_code in (400, 422)
        service.get_document.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestBulkUpdateGate:
    def test_mixed_batch_as_plain_user(self, single_tenant):
        service = _service()
        resp = _client(service, _user(user_id="u1")).post(
            BULK_UPDATE, json={"document_ids": BATCH, "updates": {"title": "X"}}
        )

        assert resp.status_code == 200
        body = resp.json()
        # Only the caller's own runbook was touched.
        assert body["updated_count"] == 1
        # The caller asked for the whole batch; that count is not rewritten.
        assert body["total_requested"] == len(BATCH)
        assert set(body["errors"]) == {
            f"Document {OTHERS} not found",  # invisible ⇒ same string as absent
            f"Document {GLOBAL}: not authorized",
            f"Document {ABSENT} not found",
        }
        # The gate must actually cut the list the service sees.
        service.bulk_update_documents.assert_awaited_once_with(
            document_ids=[OWN], updates={"title": "X"}
        )

    def test_platform_admin_single_tenant_passes_every_target(self, single_tenant):
        service = _service()
        resp = _client(
            service, _user(user_id="op", roles=("admin", "platform_admin"))
        ).post(BULK_UPDATE, json={"document_ids": [OWN, OTHERS, GLOBAL]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated_count"] == 3
        assert body["errors"] == []
        service.bulk_update_documents.assert_awaited_once_with(
            document_ids=[OWN, OTHERS, GLOBAL], updates={}
        )

    def test_global_target_refused_under_multi_even_for_admin(self, monkeypatch):
        # Under multi there is no standing operator override, and global
        # authoring is refused from any tenant session.
        monkeypatch.setattr(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_MULTI
        )
        service = _service()
        resp = _client(
            service, _user(user_id="op", roles=("admin", "platform_admin"))
        ).post(BULK_UPDATE, json={"document_ids": [GLOBAL, OTHERS]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated_count"] == 0
        assert set(body["errors"]) == {
            f"Document {GLOBAL}: not authorized",
            f"Document {OTHERS} not found",
        }
        service.bulk_update_documents.assert_awaited_once_with(
            document_ids=[], updates={}
        )

    def test_500_does_not_echo_exception_text(self, single_tenant):
        service = _service()
        service.bulk_update_documents = AsyncMock(
            side_effect=RuntimeError("postgres://user:secret@host/db exploded")
        )
        resp = _client(service, _user(user_id="u1")).post(
            BULK_UPDATE, json={"document_ids": [OWN]}
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Bulk update failed"
        assert "secret" not in resp.text
        assert "exploded" not in resp.text


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestBulkDeleteGate:
    def test_mixed_batch_as_plain_user(self, single_tenant):
        service = _service()
        resp = _client(service, _user(user_id="u1")).post(
            BULK_DELETE, json={"document_ids": BATCH}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 1
        assert body["total_requested"] == len(BATCH)
        assert set(body["errors"]) == {
            f"Document {OTHERS} not found",
            f"Document {GLOBAL}: not authorized",
            f"Document {ABSENT} not found",
        }
        service.bulk_delete_documents.assert_awaited_once_with([OWN])

    def test_platform_admin_single_tenant_passes_every_target(self, single_tenant):
        service = _service()
        resp = _client(
            service, _user(user_id="op", roles=("admin", "platform_admin"))
        ).post(BULK_DELETE, json={"document_ids": [OWN, OTHERS, GLOBAL]})

        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 3
        service.bulk_delete_documents.assert_awaited_once_with([OWN, OTHERS, GLOBAL])

    def test_global_target_refused_under_multi_even_for_admin(self, monkeypatch):
        monkeypatch.setattr(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_MULTI
        )
        service = _service()
        resp = _client(
            service, _user(user_id="op", roles=("admin", "platform_admin"))
        ).post(BULK_DELETE, json={"document_ids": [GLOBAL]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 0
        assert body["errors"] == [f"Document {GLOBAL}: not authorized"]
        service.bulk_delete_documents.assert_awaited_once_with([])

    def test_500_does_not_echo_exception_text(self, single_tenant):
        service = _service()
        service.bulk_delete_documents = AsyncMock(
            side_effect=RuntimeError("postgres://user:secret@host/db exploded")
        )
        resp = _client(service, _user(user_id="u1")).post(
            BULK_DELETE, json={"document_ids": [OWN]}
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Bulk delete failed"
        assert "secret" not in resp.text
        assert "exploded" not in resp.text
