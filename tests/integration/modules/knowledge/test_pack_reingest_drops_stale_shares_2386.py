"""A pack row's shares are exactly the pack's own once its transaction commits (#2386).

A pack re-ingest replaced a runbook's ``knowledge_items`` row but kept every
``resource_shares`` row of its id, and the inventory clause and the vector
path's id resolver both grant by share whatever the row's scope: a team the
runbook moved away from kept reading it. ``_insert_pack_row`` now deletes the
id's shares that are not the pack's own, after the INSERT and in the same
transaction, and issues that DELETE only when the read before it finds one.
``kb_init`` re-ingests an entry that moves to another scope or team whatever
its hash, and a skipped row has its stale shares deleted.

Driven through ``kb_init._ingest_pack_runbook``, the entry the KB bootstrap
uses, on FK-on SQLite (RLS off) with the real share repository, an on-disk
ChromaDB and the production ``KnowledgeService``; only the embedder is fixed.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import delete, event, select

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_pack import PackChunk, PackRunbook
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.models import (
    KnowledgeItemModel,
    ResourceShareModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    resolve_shared_kb_ids,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import item_id_from_runbook_id
from tests.chroma_clients import closed_at_teardown
from tests.integration.modules.knowledge.test_row_checked_retrieval_2397 import (
    OWNER,
    QUERY,
    REAL_VECTORS,
    _fixed_query_embedding,  # noqa: F401 - autouse: every query embeds to Q
    factory,  # noqa: F401 - the FK-on SQLite world, OWNER seeded
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: E501
    _Shares,
)
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

DOC = item_id_from_runbook_id("pack-shares-2386")
T1 = "11111111-2386-4111-8111-111111111111"
T2 = "22222222-2386-4222-8222-222222222222"
T3 = "33333333-2386-4333-8333-333333333333"


class _ReadableShares(_Shares):
    """``_Shares`` plus the read the vector path resolves team ids with."""

    async def list_resource_ids(self, **kwargs):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).list_resource_ids(**kwargs)


def _world(factory_, tmp_path) -> SimpleNamespace:
    chroma = closed_at_teardown(
        chromadb.PersistentClient(
            path=str(tmp_path / "chroma"),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
        )
    )
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=KnowledgeVectorStore(chroma),
        db_session_factory=factory_,
        share_repository=_ReadableShares(factory_),
    )
    return SimpleNamespace(
        service=service, factory=factory_, shares=service._share_repo
    )


def _pack(version: str, scope: str, team_id: Optional[str] = None) -> PackRunbook:
    content = f"# Runbook {version}\nConnection pool exhausted; queries time out."
    return PackRunbook(
        item_id=DOC,
        content_hash=f"hash-{version}",
        title=f"Runbook {version}",
        scope=scope,
        relpath=f"{scope}/shares.md",
        content=content,
        tags=[],
        source_url=None,
        owner_id=None if scope == "global" else OWNER,
        team_id=team_id,
        chunks=[PackChunk(chunk_index=0, text=content, embedding=REAL_VECTORS[0])],
    )


async def _seed(world, runbook: PackRunbook) -> str:
    return await kb_init._ingest_pack_runbook(
        runbook=runbook,
        knowledge_service=world.service,
        db_session_factory=world.factory,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        role_may_update=True,
    )


async def _shares(world) -> List[tuple]:
    async with world.factory() as session:
        rows = await session.execute(
            select(
                ResourceShareModel.scope_type,
                ResourceShareModel.scope_id,
                ResourceShareModel.share_id,
            ).where(ResourceShareModel.resource_id == DOC)
        )
        return sorted(tuple(r) for r in rows)


async def _sees(world, team: str) -> bool:
    """A member of ``team`` (and of no other), on the inventory read."""
    async with world.factory() as session:
        items = await DatabaseKnowledgeItemRepository(session).list_for_inventory(
            STANDALONE_ENTERPRISE_ID, user_id=f"member-of-{team}", team_ids=[team]
        )
    return DOC in {i.item_id for i in items}


class _ShareStatements:
    """Every statement the engine runs, as ``(verb, table)`` in order; ``verbs``
    are those against ``resource_shares``."""

    def __init__(self, factory_) -> None:
        self.seen: List[tuple] = []
        self._engine = factory_.kw["bind"].sync_engine

    @property
    def verbs(self) -> List[str]:
        return [verb for verb, table in self.seen if table == "resource_shares"]

    def _record(self, _conn, _cursor, statement, *_args) -> None:
        table = re.search(r"\b(?:INTO|FROM|UPDATE)\s+(\w+)", statement, re.I)
        self.seen.append((statement.split(None, 1)[0].upper(), table and table[1]))

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)


async def test_t1_to_t2_moves_the_runbook_off_t1(factory, tmp_path):  # noqa: F811
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    assert await _sees(world, T1)

    assert await _seed(world, _pack("v2", "team", T2)) == "ingested"

    assert [s[:2] for s in await _shares(world)] == [("team", T2)]
    assert not await _sees(world, T1)
    assert await _sees(world, T2)


async def test_t1_to_t2_leaves_t1_no_vector_hit(factory, tmp_path):  # noqa: F811
    """The vector path: a T1 member's resolved share ids and KB search."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    await _seed(world, _pack("v2", "team", T2))

    async def found_by(team: str) -> tuple:
        member = SimpleNamespace(
            user_id=f"member-of-{team}", enterprise_id=STANDALONE_ENTERPRISE_ID
        )
        ids = await resolve_shared_kb_ids(
            world.shares, [team], STANDALONE_ENTERPRISE_ID
        )
        hits = await world.service.search_documents(QUERY, user=member, team_ids=[team])
        return DOC in ids, DOC in {r["document_id"] for r in hits["results"]}

    assert await found_by(T1) == (False, False)
    assert await found_by(T2) == (True, True)


@pytest.mark.parametrize("scope", ["global", "personal"])
async def test_t1_to_an_unshared_scope_drops_every_share(
    factory, tmp_path, scope  # noqa: F811
):
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))

    await _seed(world, _pack("v2", scope))

    assert await _shares(world) == []
    # Global: everyone sees it. Personal: its owner only, no team member.
    assert await _sees(world, T1) is (scope == "global")


async def test_a_share_left_on_an_id_with_no_row_is_dropped(
    factory, tmp_path  # noqa: F811
):
    world = _world(factory, tmp_path)
    await world.shares.share(
        resource_type="knowledge_item",
        resource_id=DOC,
        scope_type="team",
        scope_id=T1,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )

    await _seed(world, _pack("v1", "team", T2))

    assert [s[:2] for s in await _shares(world)] == [("team", T2)]
    assert not await _sees(world, T1)


async def test_a_global_reingest_only_reads_the_shares(factory, tmp_path):  # noqa: F811
    """Pin: on SQLite this proves the statements' shape, not the privilege.
    The ``kb_seed`` maintenance role holds only SELECT on ``resource_shares``,
    so a global row's transaction must issue nothing else there."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "global"))

    with _ShareStatements(factory) as spy:
        assert await _seed(world, _pack("v2", "global")) == "ingested"

    assert spy.verbs and set(spy.verbs) == {"SELECT"}


async def test_a_first_team_ingest_deletes_no_share(factory, tmp_path):  # noqa: F811
    world = _world(factory, tmp_path)
    with _ShareStatements(factory) as spy:
        await _seed(world, _pack("v1", "team", T1))

    assert "DELETE" not in spy.verbs and "INSERT" in spy.verbs
    assert [s[:2] for s in await _shares(world)] == [("team", T1)]


async def test_t1_to_t1_keeps_its_one_share(factory, tmp_path):  # noqa: F811
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    before = await _shares(world)

    with _ShareStatements(factory) as spy:
        assert await _seed(world, _pack("v2", "team", T1)) == "ingested"

    assert "DELETE" not in spy.verbs
    assert len(before) == 1 and await _shares(world) == before  # same share_id


async def test_a_stale_replace_rolls_back_and_leaves_the_peers_shares(
    factory, tmp_path  # noqa: F811
):
    """A peer replaced the row first: this transaction's guarded DELETE matches
    nothing, its INSERT conflicts, and the peer's shares stand."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    async with factory() as session:
        observed = (await session.get(KnowledgeItemModel, DOC)).vector_generation
    await _seed(world, _pack("v2", "team", T2))
    peers = await _shares(world)

    loser = KnowledgeItem(
        item_id=DOC,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        title="Runbook v3",
        content="# Runbook v3\nConnection pool exhausted.",
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.TEAM,
        owner_id=OWNER,
    )
    with pytest.raises(ValueError, match="already exists"):
        await world.service._insert_pack_row(
            loser, replaces_generation=observed, team_id=T3, created_by=OWNER
        )

    assert await _shares(world) == peers
    assert [s[:2] for s in peers] == [("team", T2)]


@pytest.mark.parametrize("scope, team", [("team", T2), ("personal", None)])
async def test_an_unchanged_runbook_that_moves_is_reingested(
    factory, tmp_path, scope, team  # noqa: F811
):
    """The same content (and hash) at another team or scope is a change."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))

    assert await _seed(world, _pack("v1", scope, team)) == "ingested"

    assert [s[:2] for s in await _shares(world)] == ([("team", T2)] if team else [])
    async with factory() as session:
        assert (await session.get(KnowledgeItemModel, DOC)).scope == scope


async def test_a_skipped_row_drops_a_stale_share(factory, tmp_path):  # noqa: F811
    """A share an earlier ingest left is repaired on the next boot, without a
    new pack version; a boot that finds none stale deletes nothing."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T2))
    own = await _shares(world)
    await world.shares.share(
        resource_type="knowledge_item",
        resource_id=DOC,
        scope_type="team",
        scope_id=T1,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )

    assert await _seed(world, _pack("v1", "team", T2)) == "skipped"
    assert await _shares(world) == own

    with _ShareStatements(factory) as spy:
        assert await _seed(world, _pack("v1", "team", T2)) == "skipped"
    assert set(spy.verbs) == {"SELECT"}


async def test_the_share_clean_up_runs_after_the_insert(
    factory, tmp_path  # noqa: F811
):
    """The INSERT is the transaction's first write, so a duplicate id fails
    before any share statement; the clean-up's read and delete follow it."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))

    with _ShareStatements(factory) as spy:
        await _seed(world, _pack("v2", "team", T2))

    order = spy.seen
    insert = order.index(("INSERT", "knowledge_items"))
    dropped = order.index(("DELETE", "resource_shares"))
    read = max(i for i in range(dropped) if order[i] == ("SELECT", "resource_shares"))
    assert insert < read < dropped, order


async def _share_to(world, team: str) -> None:
    await world.shares.share(
        resource_type="knowledge_item",
        resource_id=DOC,
        scope_type="team",
        scope_id=team,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )


async def test_a_move_is_seen_when_the_old_share_is_gone(
    factory, tmp_path  # noqa: F811
):
    """A team row with no share at all still lacks the pack's team share."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    async with factory() as session:
        await session.execute(
            delete(ResourceShareModel).where(ResourceShareModel.resource_id == DOC)
        )
        await session.commit()

    assert await _seed(world, _pack("v1", "team", T2)) == "ingested"

    assert [s[:2] for s in await _shares(world)] == [("team", T2)]


async def test_the_skip_repair_spares_a_share_written_after_its_read(
    factory, tmp_path  # noqa: F811
):
    """The repair deletes only the share ids the skip read as stale: a peer's
    share written between that read and the repair survives."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v1", "team", T1))
    await _share_to(world, T3)
    real = kb_init._drop_stale_shares

    async def peer_writes_first(*args):
        await _share_to(world, T2)
        await real(*args)

    with patch.object(kb_init, "_drop_stale_shares", peer_writes_first):
        assert await _seed(world, _pack("v1", "team", T1)) == "skipped"

    assert [s[:2] for s in await _shares(world)] == [("team", T1), ("team", T2)]
