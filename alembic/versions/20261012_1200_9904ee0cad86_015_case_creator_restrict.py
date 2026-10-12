"""015_case_creator_restrict

An account that created cases cannot be deleted (#1923). Owner ruling
(2026-10-12): refuse until reassigned. ``DELETE /auth/users/{username}`` refuses
while the account is the creator of any case and points to
``fm-reassign-cases``; this revision makes the database refuse it on every
other path too, raw SQL included, so no operation leaves a ``cases`` row whose
creator account no longer exists.

Before it, ``cases.user_id`` was ``ON DELETE SET NULL`` (the baseline): deleting
a creator nulled the column, and every read that projects the case (whose
published ``user_id`` is required) failed on the row.

The DRIVER is unchanged. ``cases.driver_id`` stays ``ON DELETE SET NULL``
(012): deleting an account that only drives other people's cases hands each
back to its creator (ADR-020 D3).

PostgreSQL
----------
``cases_user_id_fkey`` (PostgreSQL's own name for the baseline's unnamed
single-column key, the same default 012 relied on for ``cases_driver_id_fkey``)
is dropped and re-added ``ON DELETE RESTRICT``. Referential checks bypass
row-level security, so the key refuses the delete whatever tenant the deleting
session is bound to. Deleting an enterprise still removes its cases and users
together: its cascade reaches both tables in the one statement, and that is
pinned by a test, not inferred from ``RESTRICT``.

SQLite
------
Changing a key's ``ON DELETE`` clause on SQLite means rebuilding ``cases``
(create, copy, drop, rename), and 16 tables reference ``cases``: dropping the
old table under ``PRAGMA foreign_keys=ON`` would cascade into their rows. So
the stored clause stays ``SET NULL`` and two triggers stand in for
``RESTRICT``:

- ``users_delete_refuses_case_creator``, ``BEFORE DELETE ON users``, aborts the
  delete of any account a case names as its creator, so ``SET NULL`` never
  fires on a ``DELETE``. It holds whether or not the connection turned foreign
  keys on.
- ``cases_creator_never_nulled``, ``BEFORE UPDATE OF user_id ON cases``, aborts
  any write that turns a set creator into NULL. ``INSERT OR REPLACE`` /
  ``REPLACE INTO users`` removes the conflicting row by an implicit delete that
  fires delete triggers only under ``PRAGMA recursive_triggers`` (off by
  default, as on a raw sqlite3 or alembic connection), but its ``SET NULL``
  action still runs as an UPDATE of ``cases``, which this trigger refuses.
  Moving a case to another account (``fm-reassign-cases``: non-NULL to
  non-NULL) passes.

With foreign keys OFF, a ``REPLACE`` (or any rewrite of ``users.user_id``) runs
no key action at all: the row goes and ``cases.user_id`` keeps an id that names
no account. Every reference in the schema dangles the same way under foreign
keys off; the app's connections turn them on.

PostgreSQL has no REPLACE. Its nearest equivalent, ``INSERT … ON CONFLICT DO
UPDATE``, updates the row in place and deletes nothing; one that changes
``users.user_id`` is refused by the same key (its ``ON UPDATE`` is ``NO
ACTION``), and ``MERGE … WHEN MATCHED THEN DELETE`` by ``RESTRICT``.

The ORM states ``RESTRICT``, which is the PostgreSQL truth. Alembic compares
``ondelete`` when it autogenerates, so autogenerate against a SQLite database
reports this key as drift (``SET NULL`` stored, ``RESTRICT`` declared). No CI
gate runs autogenerate; a revision generated against SQLite must drop that
spurious key change by hand.

Rows orphaned before this revision stay NULL; nothing here recovers them
(``SELECT count(*) FROM cases WHERE user_id IS NULL;`` counts them).

``downgrade()`` restores ``ON DELETE SET NULL`` on PostgreSQL and drops both
triggers on SQLite, and nothing else.

Revision ID: 9904ee0cad86
Revises: 9b3e5c71d2a4
Create Date: 2026-10-12 12:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "9904ee0cad86"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "9b3e5c71d2a4"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "cases"
COLUMN = "user_id"
#: PostgreSQL's own name for the baseline's unnamed single-column key.
FOREIGN_KEY = "cases_user_id_fkey"
TRIGGER = "users_delete_refuses_case_creator"
NULLING_TRIGGER = "cases_creator_never_nulled"
#: The SQLite refusal's text. Callers tell the refusal apart by its type
#: (``IntegrityError``), never by this text.
REFUSAL = (
    "an account that created cases cannot be deleted; move them to another "
    "account with fm-reassign-cases first"
)
#: The SQLite refusal of a write that nulls a set creator.
NULLING_REFUSAL = (
    "a case cannot lose its creator; move it to another account with "
    "fm-reassign-cases"
)


def _key(ondelete: str) -> None:
    op.drop_constraint(FOREIGN_KEY, TABLE, type_="foreignkey")
    op.create_foreign_key(
        FOREIGN_KEY, TABLE, "users", [COLUMN], ["user_id"], ondelete=ondelete
    )


def upgrade() -> None:
    """Refuse deleting a case's creator: the key on PostgreSQL, two triggers
    on SQLite."""
    if op.get_context().dialect.name == "sqlite":
        op.execute(f"""
            CREATE TRIGGER {TRIGGER}
            BEFORE DELETE ON users
            FOR EACH ROW WHEN EXISTS (
                SELECT 1 FROM {TABLE} WHERE {COLUMN} = OLD.user_id
            )
            BEGIN SELECT RAISE(ABORT, '{REFUSAL}'); END
            """)
        op.execute(f"""
            CREATE TRIGGER {NULLING_TRIGGER}
            BEFORE UPDATE OF {COLUMN} ON {TABLE}
            FOR EACH ROW WHEN OLD.{COLUMN} IS NOT NULL AND NEW.{COLUMN} IS NULL
            BEGIN SELECT RAISE(ABORT, '{NULLING_REFUSAL}'); END
            """)
    else:
        _key("RESTRICT")


def downgrade() -> None:
    """Restore ``ON DELETE SET NULL`` (PostgreSQL) or drop both triggers
    (SQLite)."""
    if op.get_context().dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {NULLING_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
    else:
        _key("SET NULL")
