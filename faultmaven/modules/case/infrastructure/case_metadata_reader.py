"""The cross-enterprise case metadata read (ADR-012 D9) — PostgreSQL only.

Backs ``GET /api/v1/admin/cases`` under ``TENANT_PROVIDER=multi``, and nothing
else. It reads through the two ``SECURITY DEFINER`` functions revision
``003_admin_case_metadata`` creates; that revision's docstring says why a definer
function, and what bounds it: the result type, and ``EXECUTE`` granted to the
runtime role rather than ``PUBLIC``. This module adds no rule of its own: every
derived field comes from :meth:`CaseMetadata.from_stored`, which applies the
rules a loaded case applies.

There is deliberately no SQLite implementation and no method on
``ICaseRepository``: SQLite is single-tenant and has no row-level security, so
there the operator list reads cases directly and needs no bypass.
"""

from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.metadata import (
    CaseMetadata,
    CaseMetadataNotGrantedError,
    CaseMetadataUnavailableError,
)

#: PostgreSQL ``undefined_function``: the revision that creates the functions
#: has not been applied to this database.
_UNDEFINED_FUNCTION = "42883"

#: PostgreSQL ``insufficient_privilege``: the connected role was not granted
#: ``EXECUTE`` on the functions (they are not executable by ``PUBLIC``).
_INSUFFICIENT_PRIVILEGE = "42501"

# ``SELECT *`` on purpose: every column the function returns must be a keyword
# ``CaseMetadata.from_stored`` accepts, so a column added to the function without
# being classified there fails loudly instead of being read and dropped.
_PAGE = text("SELECT * FROM admin_case_metadata_page(:state, :source, :limit, :offset)")
_COUNT = text("SELECT admin_case_metadata_count(:state, :source)")


def _sqlstate(exc: DBAPIError) -> Optional[str]:
    """Identified by SQLSTATE, not by message text. ``exc.orig`` is SQLAlchemy's
    DBAPI wrapper; the driver exception carrying the code is its ``__cause__``."""
    cause = getattr(exc.orig, "__cause__", None)
    return getattr(cause, "sqlstate", None)


class PostgreSQLCaseMetadataReader:
    """Reads case metadata across every enterprise over one session."""

    def __init__(self, db_session):
        self.db = db_session

    async def list_case_metadata(
        self,
        *,
        state: Optional[CaseState],
        source: Optional[str],
        limit: int,
        offset: int,
    ) -> Tuple[List[CaseMetadata], int]:
        # A falsy source means "no filter", as it does on the case list.
        filters = {"state": state.value if state else None, "source": source or None}
        try:
            total = (await self.db.execute(_COUNT, filters)).scalar_one()
            rows = (
                (
                    await self.db.execute(
                        _PAGE, {**filters, "limit": limit, "offset": offset}
                    )
                )
                .mappings()
                .all()
            )
        except DBAPIError as exc:
            sqlstate = _sqlstate(exc)
            if sqlstate == _UNDEFINED_FUNCTION:
                raise CaseMetadataUnavailableError(
                    "the cross-enterprise case metadata functions are not "
                    "installed; apply the database migrations"
                ) from exc
            if sqlstate == _INSUFFICIENT_PRIVILEGE:
                raise CaseMetadataNotGrantedError(
                    "the application's database role lacks EXECUTE on the "
                    "cross-enterprise case metadata functions; grant it"
                ) from exc
            raise
        return [CaseMetadata.from_stored(**row) for row in rows], int(total)


class SessionlessCaseMetadataReader:
    """Opens a session per call, like the other sessionless repositories."""

    async def list_case_metadata(
        self,
        *,
        state: Optional[CaseState],
        source: Optional[str],
        limit: int,
        offset: int,
    ) -> Tuple[List[CaseMetadata], int]:
        async with get_db_session() as session:
            return await PostgreSQLCaseMetadataReader(session).list_case_metadata(
                state=state, source=source, limit=limit, offset=offset
            )
