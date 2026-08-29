"""045_suggestion_validation_verdict

Adds the runbook quality gate's verdict to ``knowledge_suggestions`` —
``validation_passed`` / ``validation_errors`` / ``validation_warnings`` — plus
the ``version`` counter that makes concurrent writes to the row safe (fm#1227).

``KnowledgeSuggestion`` grew those three fields in #1226 so a reviewer sees WHY a
draft cannot be published instead of discovering it as a 422 on approve. The
table predates them, so it had nowhere to put them: while the store was an
in-process dict that did not matter, but #1227 moves the store into this table
and a column-less field is a field that is silently lost on every read. The
inbox would then show ``passed: null`` — "not yet evaluated" — about a draft the
extractor had already evaluated and rejected, which is worse than showing
nothing, because ``null`` is the value the API documents as "never read this as
fine".

``validation_passed`` is NULLABLE on purpose and has no server default: the
domain's three-valued reading (``None`` = not yet evaluated, ``False`` =
evaluated and refused, ``True`` = clears the gate) is load-bearing, and a
``False`` default would backfill every pre-existing row with a verdict nobody
reached. The two list columns are NOT NULL with a ``[]`` default, matching the
domain's ``default_factory=list`` — an unevaluated row has no errors to show,
which is true whichever way ``validation_passed`` reads.

``version`` is the optimistic-concurrency token, mirroring ``cases.version``
(``Integer NOT NULL DEFAULT 1`` with a ``>= 1`` CHECK). #1227 moves the store
from one live in-process object shared by every caller in a worker to a table
read as detached copies, and that is what creates the lost-update class: two
reviewers (or two pods) load the same row at version N, and the second write
would otherwise replay its whole stale snapshot over the first — reverting a
rejection, or resetting ``knowledge_item_id`` to NULL while the knowledge item
stays published, which is exactly the orphan ``_rollback_published_item``
exists to prevent. The repository's UPDATE carries ``WHERE version = :loaded``
and bumps it, so the second write fails loudly instead of silently winning.

Type follows the table's own ``metadata`` column and ``JsonBlob``:
``Text().with_variant(JSONB, "postgresql")``. The repository writes them with
``json.dumps`` and reads them through ``decode_json_blob``-style handling, which
accepts both the SQLite string and the PostgreSQL decoded shape.

Revision ID: e9f0a1b2c3d4
Revises: c7d8e9f0a1b2
Create Date: 2026-08-29 13:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_BLOB = sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    """Add the three verdict columns and the version counter.

    Batch mode for SQLite. The CHECK is added inside the same batch as the
    column so SQLite's table rebuild emits it with the rest of the constraints
    rather than in a second rebuild.
    """
    with op.batch_alter_table("knowledge_suggestions") as batch:
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_check_constraint(
            "knowledge_suggestions_version_positive", "version >= 1"
        )
        batch.add_column(sa.Column("validation_passed", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column(
                "validation_errors",
                _JSON_BLOB,
                nullable=False,
                server_default="[]",
            )
        )
        batch.add_column(
            sa.Column(
                "validation_warnings",
                _JSON_BLOB,
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    """Drop the version counter and the three verdict columns."""
    with op.batch_alter_table("knowledge_suggestions") as batch:
        batch.drop_constraint("knowledge_suggestions_version_positive", type_="check")
        batch.drop_column("version")
        batch.drop_column("validation_warnings")
        batch.drop_column("validation_errors")
        batch.drop_column("validation_passed")
