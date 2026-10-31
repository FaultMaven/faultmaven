"""023_drop_in_flight_revisions

A revised problem statement awaiting the user's re-confirmation now lives on
the case's offer record (``cases.metadata["offer_record"]``), as the open
revision offer, and ``problem_verification.pending_revision`` is gone (#2379).

By ruling (2026-10-30, ruling 5), offers and declines in flight at deploy are
dropped, not carried over. Most of that needs no migration: the retired keys
(``metadata.pending_transition``, ``progress.deferred_disposition_declined_
signatures``, ``problem_verification.declined_revision_keys`` and
``problem_verification.invalidation.close_declined_at_turn``) are ignored when
a case loads and are gone at its next save.

One is not harmless. A case with a stored ``pending_revision`` holds
``progress.problem_status = revision_pending``, a status only the revision
offer can leave; with the payload unread, no offer is open, and the case would
stay on hold with nowhere to stage cause work. So for each such row this
revision returns ``progress.problem_status`` to the revision's
``prior_status`` (``unverified`` when it carries none, the model's default)
and removes ``pending_revision``: what the user's own close of the revision
does. The cause work staged on it is dropped with it.

Row-level security
------------------

``cases`` is tenant-scoped. On PostgreSQL the UPDATE runs under
``SET LOCAL row_security = off``, restored to ``DEFAULT`` afterwards, as
revision 007 does and for the same reason: the migrating role owns the table
and is exempt, and a role the policy would filter raises instead of silently
updating one enterprise's rows.

Downgrade
---------

A no-op. The revisions were dropped by ruling; there is nothing to restore, and
the previous image reads a row without ``pending_revision`` as a case with
none.

Revision ID: cf17b34fc8c4
Revises: 03f93180f630
Create Date: 2026-10-31 12:00:00
"""

import logging
from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "cf17b34fc8c4"
down_revision: Union[str, Sequence[str], None] = "03f93180f630"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

#: Per dialect: return the status to the revision's prior one, and remove the
#: revision. One statement, so both columns move together.
DROP_REVISIONS = {
    "sqlite": (
        "UPDATE cases SET "
        "progress = json_set(progress, '$.problem_status', COALESCE("
        "json_extract(problem_verification, '$.pending_revision.prior_status'), "
        "'unverified')), "
        "problem_verification = json_remove(problem_verification, "
        "'$.pending_revision') "
        "WHERE json_type(problem_verification, '$.pending_revision') = 'object'"
    ),
    "postgresql": (
        "UPDATE cases SET "
        "progress = jsonb_set(progress, '{problem_status}', to_jsonb(COALESCE("
        "problem_verification->'pending_revision'->>'prior_status', "
        "'unverified'))), "
        "problem_verification = problem_verification - 'pending_revision' "
        "WHERE jsonb_typeof(problem_verification->'pending_revision') = 'object'"
    ),
}


def upgrade() -> None:
    """Drop every in-flight revision, returning its case to the prior status."""
    dialect = op.get_context().dialect.name
    postgresql_dialect = dialect == "postgresql"
    if postgresql_dialect:
        op.execute(ROW_SECURITY_OFF)
    statement = text(DROP_REVISIONS[dialect])
    if op.get_context().as_sql:
        op.execute(statement)
    else:
        count = op.get_bind().execute(statement).rowcount
        _log.info("023: dropped in-flight revisions on %d row(s)", count)
    if postgresql_dialect:
        op.execute(ROW_SECURITY_RESTORED)


def downgrade() -> None:
    """Nothing to restore: the in-flight revisions were dropped by ruling."""
