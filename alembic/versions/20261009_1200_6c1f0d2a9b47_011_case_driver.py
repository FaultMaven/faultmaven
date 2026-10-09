"""011_case_driver

The case driver (ADR-020, #1898): ``cases.driver_id``, who holds the case's
investigation writes.

One nullable column beside the unchanged ``cases.user_id`` (the creator). NULL
means the creator drives: the effective driver is
``COALESCE(driver_id, user_id)``, so the column needs no backfill and "the
creator drives" has exactly one stored form. A NOT NULL column backfilled from
``user_id`` was rejected (ADR-020): ``user_id`` is itself nullable and set NULL
when an account is deleted, so some rows have nothing to copy.

Column
------
``VARCHAR(36)``, a foreign key to ``users.user_id`` with ``ON DELETE SET
NULL``: deleting the driver's account hands each of its cases back to the
creator through the key itself. ``ix_cases_driver_id`` serves the release
queries (``WHERE driver_id = :account``, ADR-020 D3) and the key's own ON
DELETE scan.

Dialects
--------
Both dialects add the column in place, with no table rebuild. SQLite takes the
reference inline (``ADD COLUMN ... REFERENCES``, allowed for a column whose
default is NULL); Alembic's SQLite dialect would instead emit the key as a
separate ``ALTER``, which SQLite cannot run. PostgreSQL adds the column and then
``cases_driver_id_fkey``.

``cases`` is already enterprise-scoped under row-level security on PostgreSQL;
a new column on it is covered by the existing policy, so there is no RLS
change.

``downgrade()`` drops the index and then the column, and nothing else. SQLite
(3.35+) drops a column carrying a column-level foreign key in place, as long as
no index names it, which is why the index goes first.

Revision ID: 6c1f0d2a9b47
Revises: afdd293ca6ab
Create Date: 2026-10-09 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "6c1f0d2a9b47"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "afdd293ca6ab"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "cases"
COLUMN = "driver_id"
INDEX = "ix_cases_driver_id"
#: PostgreSQL's own name for an unnamed single-column key, as the baseline's
#: ``cases_user_id_fkey`` has.
FOREIGN_KEY = "cases_driver_id_fkey"


def upgrade() -> None:
    """Add ``cases.driver_id``, its foreign key and its index."""
    if op.get_context().dialect.name == "sqlite":
        # Alembic's SQLite dialect adds the column and then refuses the foreign
        # key as a separate ALTER; SQLite takes the reference inline instead.
        op.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(36) "
            "REFERENCES users (user_id) ON DELETE SET NULL"
        )
    else:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=36), nullable=True))
        op.create_foreign_key(
            FOREIGN_KEY,
            TABLE,
            "users",
            [COLUMN],
            ["user_id"],
            ondelete="SET NULL",
        )
    op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    """Drop the index, then the column (its foreign key goes with it)."""
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
