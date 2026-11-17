"""026_scrub_convert_receipt_bodies

Before contract 15.3.0 (#2856) a keyed ``POST /knowledge/convert`` stored its
whole 201 body in ``kb_request_receipts.response``: every draft's ``content``
and the analysis. Text a user later edited out of a draft stayed there. Since
15.3.0 a convert receipt stores its job's id only, and a replay answers from
the job, so nothing reads the old bodies; they would leave only with their job.

By ruling (2026-11-17, #2452) they are scrubbed. This revision sets the
``response`` of every receipt with ``route = 'convert'`` to
``{"conversion_id": <its conversion_id>}``, the shape the current writer
stores. Every convert row is rewritten, those already in shape included, so
the old body is never read, only replaced, and a second run changes nothing.
The ``route = 'convert'`` CHECK holds ``conversion_id`` non-NULL, so no row
gets ``{}``. Upload receipts are not touched: their body holds no document
text.

What "at rest" covers: afterwards no live row holds the text. The old bytes
can outlive the row. On PostgreSQL a plain VACUUM marks a dead tuple's space
reusable without overwriting it, so they stay in the heap and TOAST files
until that space is reused or the table is rewritten (VACUUM FULL). On SQLite
a freed page keeps them unless ``secure_delete`` is on. WAL and backups
already taken keep theirs.

Rolling deploys
---------------

Run this release with no pod on an image older than 15.3.0 serving keyed
``POST /knowledge/convert`` after the revision has run (a Recreate rollout, or
scale the old replicas down first). Such a pod still stores the full body, and
a body it writes afterwards keeps its drafts' text until its job is deleted:
Alembic never runs 026 again.

Row-level security
------------------

``kb_request_receipts`` is tenant-scoped. On PostgreSQL the UPDATE runs under
``SET LOCAL row_security = off``, restored to ``DEFAULT`` afterwards, as
revisions 007 and 023 do and for the same reason: the migrating role owns the
table and is exempt, and a role the policy would filter raises instead of
rewriting one enterprise's rows and reporting success.

Downgrade
---------

A no-op: the removed text cannot be restored. Revision 025's image is either
15.3.0 or later, which reads only the id, or older. An older image replays
from the stored body, and ``{"conversion_id": ...}`` does not validate as its
convert answer, so that replay is 409 ``IDEMPOTENCY_REPLAY_UNAVAILABLE``: the
old code's own path for an unreadable body. Nothing publishes twice.

Revision ID: 4492ab088c62
Revises: 45970adab01d
Create Date: 2026-11-17 12:00:00
"""

import logging
from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "4492ab088c62"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "45970adab01d"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

#: Per dialect: replace every convert receipt's body with its job id only.
SCRUB_CONVERT_BODIES = {
    "sqlite": (
        "UPDATE kb_request_receipts SET "
        "response = json_object('conversion_id', conversion_id) "
        "WHERE route = 'convert'"
    ),
    "postgresql": (
        "UPDATE kb_request_receipts SET "
        "response = json_build_object('conversion_id', conversion_id) "
        "WHERE route = 'convert'"
    ),
}


def upgrade() -> None:
    """Replace every convert receipt's body with its job id, under
    ``row_security = off`` on PostgreSQL, and log how many rows it rewrote."""
    dialect = op.get_context().dialect.name
    postgresql_dialect = dialect == "postgresql"
    if postgresql_dialect:
        op.execute(ROW_SECURITY_OFF)
    statement = text(SCRUB_CONVERT_BODIES[dialect])
    if op.get_context().as_sql:
        op.execute(statement)
    else:
        count = op.get_bind().execute(statement).rowcount
        _log.info("026: rewrote %d convert receipt(s)", count)
    if postgresql_dialect:
        op.execute(ROW_SECURITY_RESTORED)


def downgrade() -> None:
    """Change nothing: the scrubbed text cannot be restored, and an image
    older than 15.3.0 answers a replay of a scrubbed receipt with 409."""
