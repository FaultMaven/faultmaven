"""Deleting a published runbook discards the draft that published it (#2369).

A published draft keeps its row: ``verified``, naming its item through
``conversion_drafts.knowledge_item_id``, whose key is ``ON DELETE SET NULL``.
``delete_document`` left that row as it was, so ``list_all_drafts`` went on
listing a verified draft that named nothing. The delete now discards it in its
own transaction, before its ``DELETE`` runs: the text is kept, the draft leaves
the list, and a case job's live key is released. A delete that does not
complete leaves the draft as it was.

Each test runs on the #2365 tests' database, which enforces foreign keys as the
app's engine does, so a discard issued after the ``DELETE`` would match nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    UploadedFileModel,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    OWNER,
    _drafts,
    _FailingCommit,
    _knowledge,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

CASE_KEY = "case-2369"


async def _publish(factory, knowledge, kind: str) -> tuple[str, str]:
    """Publish a runbook as ``kind`` does; return its item and draft ids."""
    if kind == "upload":
        document_id = (await _upload(knowledge))["document_id"]
        (listed,) = await _drafts(factory)
        return document_id, listed["draft_id"]
    async with factory() as session:
        session.add(
            UploadedFileModel(
                file_id="file_2369",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                filename="f",
                size_bytes=1,
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id="conv_2369",
                user_id=OWNER,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="personal",
                status="completed",
                source_file_id="file_2369",
                source_type=kind,
                live_case_id=CASE_KEY if kind == "case" else None,
            )
        )
        session.add(
            ConversionDraftModel(
                id="draft_2369",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                conversion_id="conv_2369",
                runbook_id="rb-2369",
                title="Redis Evictions",
                content=valid_runbook("Redis Evictions Under Memory Pressure"),
                status="draft",
                validation_passed=True,
            )
        )
        await session.commit()
    published = await ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=AsyncMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=knowledge,
    ).verify_draft("conv_2369", "draft_2369", OWNER)
    return published.knowledge_item_id, "draft_2369"


async def _draft_and_key(factory, draft_id: str):
    """The draft's row, and its job's live case key."""
    async with factory() as session:
        draft = await session.get(ConversionDraftModel, draft_id)
        job = await session.get(ConversionJobModel, draft.conversion_id)
    return draft, job.live_case_id


@pytest.mark.parametrize("kind", ["document", "case", "upload"])
async def test_deleting_a_published_runbook_discards_its_draft(session_factory, kind):
    knowledge = _knowledge(session_factory)
    item_id, draft_id = await _publish(session_factory, knowledge, kind)
    published, key = await _draft_and_key(session_factory, draft_id)
    assert (published.status, published.knowledge_item_id) == ("verified", item_id)
    assert key == (CASE_KEY if kind == "case" else None)

    result = await knowledge.delete_document(item_id)

    assert result["action"] == "deleted"
    assert await _drafts(session_factory) == []
    draft, key = await _draft_and_key(session_factory, draft_id)
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)
    assert draft.content == published.content
    # The case is free to be converted again.
    assert key is None


@pytest.mark.parametrize(
    "failure,raised",
    [("vectors", KnowledgeBaseError), ("commit", RuntimeError)],
)
async def test_a_delete_that_does_not_complete_keeps_its_draft(
    session_factory, failure, raised
):
    """The vector delete fails, or the commit does: the row change rolls back,
    and the discard with it."""
    factory = _FailingCommit(session_factory, (RuntimeError("commit failed"), False))
    knowledge = _knowledge(factory)
    item_id, draft_id = await _publish(session_factory, knowledge, "case")
    if failure == "vectors":
        knowledge._vector_store.delete_documents_by_parent_id.side_effect = OSError()
    else:
        factory.armed = True

    with pytest.raises(raised):
        await knowledge.delete_document(item_id)

    draft, key = await _draft_and_key(session_factory, draft_id)
    assert (draft.status, draft.knowledge_item_id, key) == (
        "verified",
        item_id,
        CASE_KEY,
    )
    assert [d["draft_id"] for d in await _drafts(session_factory)] == [draft_id]
