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
does. The cause work staged on it is dropped with it. Only a held row moves
(``problem_status = revision_pending``): a ``pending_revision`` left behind on
a row whose hold already ended is not touched.

Row-level security
------------------

``cases`` is tenant-scoped. On PostgreSQL the UPDATE runs under
``SET LOCAL row_security = off``, restored to ``DEFAULT`` afterwards, as
revision 007 does and for the same reason: the migrating role owns the table
and is exempt, and a role the policy would filter raises instead of silently
updating one enterprise's rows.

Downgrade
---------

The same in reverse. The previous image does not read the offer record, so a
case holding a revision opened on this image would sit at ``revision_pending``
with no ``pending_revision`` there, and a turn carrying cause work would fail.
For each row whose ``metadata.offer_record.open`` is a revision, the downgrade
returns ``progress.problem_status`` to that revision's ``prior_status`` and
drops the open revision. The revisions dropped on upgrade stay dropped.

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

#: Per dialect: return a held case's status to the revision's prior one, and
#: remove the revision. One statement, so both columns move together.
DROP_REVISIONS = {
    "sqlite": (
        "UPDATE cases SET "
        "progress = json_set(progress, '$.problem_status', COALESCE("
        "json_extract(problem_verification, '$.pending_revision.prior_status'), "
        "'unverified')), "
        "problem_verification = json_remove(problem_verification, "
        "'$.pending_revision') "
        "WHERE json_type(problem_verification, '$.pending_revision') = 'object' "
        "AND json_extract(progress, '$.problem_status') = 'revision_pending'"
    ),
    "postgresql": (
        "UPDATE cases SET "
        "progress = jsonb_set(progress, '{problem_status}', to_jsonb(COALESCE("
        "problem_verification->'pending_revision'->>'prior_status', "
        "'unverified'))), "
        "problem_verification = problem_verification - 'pending_revision' "
        "WHERE jsonb_typeof(problem_verification->'pending_revision') = 'object' "
        "AND progress->>'problem_status' = 'revision_pending'"
    ),
}

#: The reverse, per dialect: return the status of a case whose open offer is a
#: revision to that revision's prior one, and drop the open revision.
DROP_OPEN_REVISIONS = {
    "sqlite": (
        "UPDATE cases SET "
        "progress = json_set(progress, '$.problem_status', COALESCE("
        "json_extract(metadata, '$.offer_record.open.revision.prior_status'), "
        "'unverified')), "
        "metadata = json_set(metadata, '$.offer_record.open', json('null')) "
        "WHERE json_extract(metadata, '$.offer_record.open.kind') = 'revision'"
    ),
    "postgresql": (
        "UPDATE cases SET "
        "progress = jsonb_set(progress, '{problem_status}', to_jsonb(COALESCE("
        "metadata->'offer_record'->'open'->'revision'->>'prior_status', "
        "'unverified'))), "
        "metadata = jsonb_set(metadata, '{offer_record,open}', 'null'::jsonb) "
        "WHERE metadata->'offer_record'->'open'->>'kind' = 'revision'"
    ),
}


def _run(statements: dict, done: str) -> None:
    """Run this dialect's statement, under ``row_security = off`` on
    PostgreSQL, and log how many rows it moved."""
    dialect = op.get_context().dialect.name
    postgresql_dialect = dialect == "postgresql"
    if postgresql_dialect:
        op.execute(ROW_SECURITY_OFF)
    statement = text(statements[dialect])
    if op.get_context().as_sql:
        op.execute(statement)
    else:
        count = op.get_bind().execute(statement).rowcount
        _log.info("023: %s on %d row(s)", done, count)
    if postgresql_dialect:
        op.execute(ROW_SECURITY_RESTORED)


def upgrade() -> None:
    """Drop every in-flight revision, returning its case to the prior status."""
    _run(DROP_REVISIONS, "dropped in-flight revisions")


def downgrade() -> None:
    """Drop every open revision offer, returning its case to the prior
    status, so the previous image finds no hold it cannot leave."""
    _run(DROP_OPEN_REVISIONS, "dropped open revision offers")
