"""037_case_message_author_id

Add ``author_id`` to ``case_messages`` — per-turn authorship capture (ADR-013
D4, as amended; ADR-011 D5).

The ``CaseMessage`` domain model, both API schemas, and the service write path
have carried ``author_id`` since their introduction
(``case_service.py`` stamps ``author_id=user_id`` on every user turn), but the
table never had the column and the SQL writer's INSERT column list dropped it
on the floor. Harmless while a case had exactly one possible author — its
owner, recoverable from ``cases.user_id``.

Team sharing ended that. Since the case-side visible-id allowlist landed, every
write endpoint gates on ``get_case(case_id, user_id)``, which resolves
``owned ∪ shared-to-my-teams`` — so any member of a team the case is shared to
can submit turns, upload evidence and close the case. A Slack-originated case
is auto-shared to the workspace's Team at creation, so the service account and
an identified Copilot member routinely interleave on one transcript. Without
this column "who said what" is unrecoverable, and unlike most data it cannot be
reconstructed later: ADR-011 D5's capture-now warning is about exactly this
column.

``author_id`` is **nullable**, for two reasons that both stand on their own:
assistant and system turns have no human author at all, and rows written before
this migration have an author we genuinely do not know (no backfill — inventing
one would be worse than admitting the gap).

It is **deliberately not a foreign key**, following ``operator_access_audit``
rather than ``cases.user_id``. Attribution must outlive the account it
describes: an ``ON DELETE SET NULL`` would erase, on user deletion, precisely
the record ADR-011 D5 calls un-backfillable, and ``ON DELETE RESTRICT`` would
make any user who ever wrote a turn undeletable. A dangling id is the correct
outcome here — the transcript keeps saying *who* wrote the turn even after that
account is gone. (This also avoids a SQLite batch rebuild of the transcript
table, which is a consequence of the choice, not the reason for it.)

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-07-26 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-turn author column the domain model already stamps."""
    op.add_column(
        "case_messages",
        sa.Column("author_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    """Drop the per-turn author column."""
    op.drop_column("case_messages", "author_id")
