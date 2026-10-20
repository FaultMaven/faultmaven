"""The case delete's eraser for runbook conversion (#1016).

A case-source conversion job keeps the case's description and root cause
verbatim in ``analysis_result``, beside its drafts and a source
``uploaded_files`` row. ``conversion_jobs.case_id`` is ``ON DELETE SET NULL``,
so without this they would outlive the case. A PUBLISHED runbook (knowledge
item) is the abstraction, not the case's text, and is kept: no foreign key
points from an item to a draft or a job, and ``conversion_drafts
.knowledge_item_id`` points the other way.

Plain SQL over the session factory, needing no ``ConversionService``, so a
failed construction of that service does not skip the erase.
"""

from typing import Any, Callable

from sqlalchemy import bindparam, text

_JOBS = text("SELECT id, source_file_id FROM conversion_jobs WHERE case_id = :case_id")

_SHARES = text(
    "DELETE FROM resource_shares WHERE resource_type = 'conversion_job' "
    "AND resource_id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_DRAFTS = text("DELETE FROM conversion_drafts WHERE conversion_id IN :ids").bindparams(
    bindparam("ids", expanding=True)
)

_DELETE_JOBS = text("DELETE FROM conversion_jobs WHERE id IN :ids").bindparams(
    bindparam("ids", expanding=True)
)

# ``source_file_id`` is ``ON DELETE RESTRICT``: a source another job still
# names stays.
_SOURCES = text(
    "DELETE FROM uploaded_files WHERE file_id IN :file_ids "
    "AND upload_source = 'conversion_source' AND case_id IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM conversion_jobs j "
    "WHERE j.source_file_id = uploaded_files.file_id)"
).bindparams(bindparam("file_ids", expanding=True))


class RunbookConversionEraser:
    """Deletes the case's conversion jobs, their share rows, their drafts and
    their source upload rows, in ONE transaction. Returns the number of rows
    deleted."""

    store_name = "runbook_conversion"

    def __init__(self, db_session_factory: Callable[[], Any]):
        self.db_session_factory = db_session_factory

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        async with self.db_session_factory() as session:
            jobs = (await session.execute(_JOBS, {"case_id": case_id})).fetchall()
            if not jobs:
                return 0
            ids = [row[0] for row in jobs]
            deleted = 0
            for statement in (_SHARES, _DRAFTS, _DELETE_JOBS):
                deleted += (await session.execute(statement, {"ids": ids})).rowcount
            sources = sorted({row[1] for row in jobs})
            deleted += (await session.execute(_SOURCES, {"file_ids": sources})).rowcount
            await session.commit()
            return deleted
