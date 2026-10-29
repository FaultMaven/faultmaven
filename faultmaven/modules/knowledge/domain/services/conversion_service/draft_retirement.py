"""Retiring drafts: discarding the drafts a deleted runbook leaves, and
releasing a case job's live-conversion claim once its last live draft goes.

A published draft keeps its row, ``verified`` and naming its knowledge item
through ``conversion_drafts.knowledge_item_id``. That key is ``ON DELETE SET
NULL``, so a draft is discarded BEFORE its item's ``DELETE`` runs: afterwards
no draft names the item, and a verified draft naming nothing would stay listed
(#2369). Both deleters do so: ``KnowledgeService.delete_document`` and
``fm-reset-kb``, which then deletes exactly the items locked here. Each helper
writes in the caller's session and commits nothing, so the caller's rollback
undoes it with the rest of its transaction."""

from typing import Iterable, List, Tuple

from sqlalchemy import func, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KnowledgeItemModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus


async def release_live_case_key_if_drained(
    session: AsyncSession,
    job: "ConversionJobModel",
    discarded_draft_id: str,
) -> None:
    """Clear ``job.live_case_id`` once the job holds no more live drafts.

    The unique-index slot on ``conversion_jobs.live_case_id`` is the case's
    one live-conversion claim; it must be released in the same transaction
    as the last live draft leaving so a later regeneration can take it. The
    clearing is general (count the OTHER non-discarded drafts of this job) so
    a job carrying more than one live draft keeps the key until the last one
    is gone. No-op for jobs that never held the key (document jobs, failed
    no-draft jobs)."""
    if job.live_case_id is None:
        return
    remaining_live = await session.execute(
        select(func.count())
        .select_from(ConversionDraftModel)
        .where(
            ConversionDraftModel.conversion_id == job.id,
            ConversionDraftModel.id != discarded_draft_id,
            ConversionDraftModel.status != DraftStatus.DISCARDED.value,
        )
    )
    if remaining_live.scalar_one() == 0:
        job.live_case_id = None


async def release_drained_case_keys(
    session: AsyncSession, discarded: Iterable[Tuple[str, str]]
) -> None:
    """Release the live case key of each job left with no live draft.

    ``discarded`` holds the ``(draft_id, conversion_id)`` of the drafts this
    session has just discarded. Flushed first, so the count sees every status
    flipped even where one job lost several drafts; then one release per job.
    """
    await session.flush()
    one_per_job = {job_id: draft_id for draft_id, job_id in discarded}
    for job_id, draft_id in one_per_job.items():
        job = await session.get(ConversionJobModel, job_id)
        if job is not None:
            await release_live_case_key_if_drained(session, job, draft_id)


async def release_case_keys_without_a_live_draft(session: AsyncSession) -> int:
    """Clear ``live_case_id`` on each job that holds one with no non-discarded
    draft, in one ``UPDATE``; return how many it cleared.

    ``fm-reset-kb`` runs it after its draft step, on either path, so a key an
    earlier reset left with no draft behind it is released too (#2387).
    """
    live_draft = (
        select(ConversionDraftModel.id)
        .where(
            ConversionDraftModel.conversion_id == ConversionJobModel.id,
            ConversionDraftModel.status != DraftStatus.DISCARDED.value,
        )
        .exists()
    )
    result = await session.execute(
        update(ConversionJobModel)
        .where(ConversionJobModel.live_case_id.is_not(None), ~live_draft)
        .values(live_case_id=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


async def discard_drafts_naming_item(session: AsyncSession, item_id: str) -> List[str]:
    """Discard the drafts naming ``item_id``, ahead of its ``DELETE``
    (``KnowledgeService.delete_document``); return their ids."""
    _, discarded = await _discard_drafts_naming(
        session, KnowledgeItemModel.item_id == item_id
    )
    return discarded


async def discard_drafts_naming_any_item(
    session: AsyncSession,
) -> Tuple[List[str], List[str]]:
    """Lock every item and discard the drafts naming one, ahead of deleting
    them (``fm-reset-kb``); return the locked item ids and the discarded draft
    ids. The caller deletes exactly those items: an item written after the
    lock was not locked and its drafts were not discarded, so deleting it
    would orphan them."""
    return await _discard_drafts_naming(session, true())


async def _discard_drafts_naming(
    session: AsyncSession, items
) -> Tuple[List[str], List[str]]:
    """Lock the items ``items`` matches and discard the live drafts naming
    them; return the locked item ids and the discarded draft ids.

    The items' rows are locked first (``FOR UPDATE``; SQLite has no row locks,
    and its single writer already serialises). A transaction linking a draft
    to one of them holds a key-share lock on its row until it commits, so the
    lock waits for that link and this ``UPDATE`` then sees it. Unlocked, the
    ``UPDATE`` missed it, the ``DELETE`` waited for it, and the key's ``SET
    NULL`` left it verified, naming nothing.

    The text is kept, the draft leaves the drafts list, and its ``runbook_id``
    slot is freed. One statement, as no unique key bounds the drafts naming one
    item, then one key release per job. A draft already discarded is not
    counted again.
    """
    locked = list(
        (
            await session.execute(
                select(KnowledgeItemModel.item_id).where(items).with_for_update()
            )
        ).scalars()
    )
    if not locked:
        return [], []
    discarded = (
        await session.execute(
            update(ConversionDraftModel)
            .where(
                ConversionDraftModel.knowledge_item_id.in_(locked),
                ConversionDraftModel.status != DraftStatus.DISCARDED.value,
            )
            .values(status=DraftStatus.DISCARDED.value, knowledge_item_id=None)
            .returning(ConversionDraftModel.id, ConversionDraftModel.conversion_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    await release_drained_case_keys(session, discarded)
    await session.flush()
    return locked, [draft_id for draft_id, _ in discarded]
