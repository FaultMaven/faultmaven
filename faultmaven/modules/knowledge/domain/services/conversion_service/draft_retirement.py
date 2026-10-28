"""Retiring drafts: discarding the drafts a deleted runbook leaves, and
releasing a case job's live-conversion claim once its last live draft goes.

A published draft keeps its row, ``verified`` and naming its knowledge item
through ``conversion_drafts.knowledge_item_id``. That key is ``ON DELETE SET
NULL``, so a draft is discarded BEFORE its item's ``DELETE`` runs: afterwards
no draft names the item, and a verified draft naming nothing would stay listed
(#2369). Each helper writes in the caller's session and commits nothing, so the
caller's rollback undoes it with the rest of its transaction."""

from typing import List

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
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


async def discard_drafts_naming_item(session: AsyncSession, item_id: str) -> List[str]:
    """Discard every draft that names ``item_id``; return their ids.

    Called by ``KnowledgeService.delete_document`` ahead of the item's
    ``DELETE``, in its transaction. The text is kept, the draft leaves the
    drafts list, and its ``runbook_id`` slot is freed. One statement, as no
    unique key bounds the drafts naming one item, then one key release per job.
    """
    discarded = (
        await session.execute(
            update(ConversionDraftModel)
            .where(ConversionDraftModel.knowledge_item_id == item_id)
            .values(status=DraftStatus.DISCARDED.value, knowledge_item_id=None)
            .returning(ConversionDraftModel.id, ConversionDraftModel.conversion_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    one_per_job = {job_id: draft_id for draft_id, job_id in discarded}
    for job_id, draft_id in one_per_job.items():
        job = await session.get(ConversionJobModel, job_id)
        if job is not None:
            await release_live_case_key_if_drained(session, job, draft_id)
    await session.flush()
    return [draft_id for draft_id, _ in discarded]
