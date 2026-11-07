"""Purging a conversion's LLM traces when its draft or document is deleted (#2575).

Only a conversion that made model calls has traces. A document or case
conversion stores the failure modes its calls worked on
(``conversion_jobs.failure_modes_detected``): at least one, as a document
conversion with none is refused before its job is written, and a case
conversion stores its one. A template, a directory scan and a direct upload
store none and made no call, so their deletes erase nothing and never wait on
the trace backend. The column is written once, with the job.

The traces are keyed on the drafts' rows, read under the caller's row-level
security, never on an item's ``source_url``, which the uploader writes.
"""

from typing import List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.observability.trace_erasure import (
    UnavailableTraceEraser,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
)
from faultmaven.modules.knowledge.contracts import IConversionTraceEraser

#: The knowledge services' default eraser: a composition that does not pass
#: one fails every delete that needs a purge. ``None`` is the explicit "no
#: trace backend is configured".
TRACE_ERASER_NOT_WIRED = UnavailableTraceEraser("no trace eraser was wired")

Owner = Tuple[str, str]


def made_model_calls(job: ConversionJobModel) -> bool:
    """Whether the job's conversion made model calls (module docstring)."""
    return job.failure_modes_detected > 0


async def conversions_with_drafts(session: AsyncSession, where) -> List[Owner]:
    """The ``(conversion_id, enterprise_id)`` of each conversion that made model
    calls and has a draft matching ``where``, in any status."""
    rows = await session.execute(
        select(ConversionDraftModel.conversion_id, ConversionDraftModel.enterprise_id)
        .join(
            ConversionJobModel,
            ConversionJobModel.id == ConversionDraftModel.conversion_id,
        )
        # ``made_model_calls``, in SQL.
        .where(where, ConversionJobModel.failure_modes_detected > 0)
        .distinct()
        .order_by(ConversionDraftModel.conversion_id)
    )
    return [(conversion_id, enterprise_id) for conversion_id, enterprise_id in rows]


async def conversions_naming_item(session: AsyncSession, item_id: str) -> List[Owner]:
    """The conversions whose traces deleting the item purges."""
    return await conversions_with_drafts(
        session, ConversionDraftModel.knowledge_item_id == item_id
    )


class TracePurge:
    """The erases of one delete, or of one bulk delete: each conversion is
    erased once, and ``refused`` records that one could not be."""

    def __init__(self, eraser: Optional[IConversionTraceEraser]):
        self.eraser = eraser
        self.refused = False
        self._erased: Set[Owner] = set()

    async def erase(self, owners: List[Owner]) -> None:
        for owner in owners:
            if owner in self._erased:
                continue
            try:
                await self.eraser.erase_conversion(*owner)
            except Exception:
                self.refused = True
                raise
            self._erased.add(owner)
