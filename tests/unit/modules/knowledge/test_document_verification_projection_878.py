"""#878 — every KB document read reports the stored ``verification_level``.

``_document_dto`` and the ``list_documents`` row builder used to drop the
trust fields, so the single read and the snippet reported ``experimental``
for every document. Invariant: single, list and snippet all carry the stored
row's level, and the status comes from ``KnowledgeItem.get_verification_status``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from tests.unit.modules.knowledge.test_document_read_visibility import (
    _app,
    _read_service,
    _seed,
    _service_over,
    _user,
    db_factory,
)

# stored level -> (verification_level, verification_status)
TABLE = [(0, "experimental"), (1, "community"), (2, "verified")]


def _item(item_id: str, level: int) -> KnowledgeItem:
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id="ent-1",
        organization_id=None,
        title=f"Runbook {level}",
        content="alpha\nbeta\ngamma",
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.GLOBAL,
        owner_id=None,
        is_published=True,
        verification_level=level,
    )


@pytest.mark.unit
@pytest.mark.knowledge_base
@pytest.mark.asyncio
class TestServiceProjection:
    async def test_single_and_list_carry_the_stored_level(self, db_factory):
        svc = _service_over(db_factory)
        for level, _ in TABLE:
            await _seed(db_factory, _item(f"kb_lvl{level}", level))
        user = SimpleNamespace(user_id="user-1", enterprise_id="ent-1")

        for level, status in TABLE:
            doc = await svc.get_document_visible(
                f"kb_lvl{level}", user=user, team_ids=[]
            )
            assert doc["verification_level"] == level
            assert doc["verification_status"] == status

        listing = await svc.list_documents(user=user, team_ids=[])
        rows = {d["document_id"]: d for d in listing["documents"]}
        assert len(rows) == len(TABLE)
        for level, status in TABLE:
            row = rows[f"kb_lvl{level}"]
            assert row["verification_level"] == level
            assert row["verification_status"] == status

    async def test_status_comes_from_the_one_rule(self, db_factory, monkeypatch):
        # The status is KnowledgeItem.get_verification_status's answer, never a
        # re-derivation from the level: a sentinel only the rule can produce
        # must reach the single read and every list row.
        sentinel = "sentinel-from-get_verification_status"
        monkeypatch.setattr(
            KnowledgeItem, "get_verification_status", lambda self: sentinel
        )
        svc = _service_over(db_factory)
        for level, _ in TABLE:
            await _seed(db_factory, _item(f"kb_lvl{level}", level))
        user = SimpleNamespace(user_id="user-1", enterprise_id="ent-1")

        for level, _ in TABLE:
            doc = await svc.get_document_visible(
                f"kb_lvl{level}", user=user, team_ids=[]
            )
            assert doc["verification_status"] == sentinel

        listing = await svc.list_documents(user=user, team_ids=[])
        rows = listing["documents"]
        assert len(rows) == len(TABLE)
        for row in rows:
            assert row["verification_status"] == sentinel


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestRoutesFedTheRealDto:
    @pytest.mark.parametrize("level,status", TABLE)
    @pytest.mark.parametrize("suffix", ["", "/snippet"])
    def test_route_reports_the_stored_level(self, level, status, suffix):
        from fastapi.testclient import TestClient

        dto = KnowledgeService._document_dto(_item("doc1", level))
        app = _app(_read_service(dto), _user())
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/knowledge/documents/doc1{suffix}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verification_level"] == level
        assert body["verification_status"] == status

    @pytest.mark.parametrize("suffix", ["", "/snippet"])
    def test_route_reads_the_dto_status_rather_than_recomputing_it(self, suffix):
        # A DTO whose status disagrees with its level: a route that re-derives
        # the status from the level answers "verified" here, not the DTO's
        # "community".
        from fastapi.testclient import TestClient

        dto = {
            **KnowledgeService._document_dto(_item("doc1", 2)),
            "verification_status": "community",
        }
        app = _app(_read_service(dto), _user())
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/knowledge/documents/doc1{suffix}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verification_level"] == 2
        assert body["verification_status"] == "community"
