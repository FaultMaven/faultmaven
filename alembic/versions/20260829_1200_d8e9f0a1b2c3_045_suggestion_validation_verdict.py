"""045_suggestion_validation_verdict

Adds the runbook quality gate's verdict to ``knowledge_suggestions`` —
``validation_passed`` / ``validation_errors`` / ``validation_warnings`` (fm#1227).

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

Type follows the table's own ``metadata`` column and ``JsonBlob``:
``Text().with_variant(JSONB, "postgresql")``. The repository writes them with
``json.dumps`` and reads them through ``decode_json_blob``-style handling, which
accepts both the SQLite string and the PostgreSQL decoded shape.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-29 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_BLOB = sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    """Add the three verdict columns. Batch mode for SQLite."""
    with op.batch_alter_table("knowledge_suggestions") as batch:
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
    """Drop the three verdict columns."""
    with op.batch_alter_table("knowledge_suggestions") as batch:
        batch.drop_column("validation_warnings")
        batch.drop_column("validation_errors")
        batch.drop_column("validation_passed")
