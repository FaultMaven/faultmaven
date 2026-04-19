"""phase_3_drop_sessions_table_and_session_id

Storage redesign 2026-04 — Phase 3: drop the SQL ``sessions`` table and
the ``cases.session_id`` foreign-key column.

Per ``case-and-session-concepts.md`` v2.1, sessions are Redis-only —
``RedisSessionStore`` over real Redis in cloud and FakeRedis in local.
The SQL ``sessions`` table was an unused artifact and a documented
anti-pattern. Cases must not bind to sessions (Anti-Pattern 1) — the
``cases.session_id`` column and its FK / index are removed at the same
time so cases can never reference the deleted table.

PG indexes are dropped explicitly first (PostgreSQL enforces this; SQLite
is permissive). On SQLite, the ``cases.session_id`` column is dropped via
``batch_alter_table`` to copy/rebuild the table without that column or
its FK constraint. The ``SessionModel`` ORM class has been deleted from
``faultmaven/infrastructure/persistence/models.py`` together with the
``CaseModel.session`` relationship and the SQL ``SessionRepository`` /
``DatabaseSessionRepository`` / ``InMemorySessionRepository`` hierarchy
in ``faultmaven/modules/auth/infrastructure/repositories``.

See deployment-schema-strategy.md §11.1 + §8.1 + §12 decisions #14, #15.

Revision ID: 575bb832f512
Revises: 7a2da68429da
Create Date: 2026-04-19 20:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "575bb832f512"
down_revision: Union[str, Sequence[str], None] = "7a2da68429da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop cases.session_id (with its FK + index) then drop the sessions table."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Drop cases.session_id (FK references sessions.session_id; must go
    #    before the sessions table is dropped).
    if dialect == "postgresql":
        op.drop_index("ix_cases_session_id", table_name="cases")
        op.drop_constraint(
            "cases_session_id_fkey", "cases", type_="foreignkey"
        )
        op.drop_column("cases", "session_id")
    else:
        # SQLite (and any other dialect): use batch mode so the table is
        # rebuilt without the column / FK / index. ``batch_alter_table``
        # transparently handles the SQLite "no DROP COLUMN" limitation.
        with op.batch_alter_table("cases") as batch_op:
            batch_op.drop_index("ix_cases_session_id")
            batch_op.drop_column("session_id")

    # 2. Drop the sessions table itself.
    if dialect == "postgresql":
        op.drop_index("ix_sessions_user_id", table_name="sessions")
        op.drop_index("ix_sessions_organization_id", table_name="sessions")

    op.drop_table("sessions")


def downgrade() -> None:
    """Recreate the sessions table and the cases.session_id column.

    DDL copied from the 001 baseline migration so a stepwise rollback
    restores the prior schema. Note: rollback only restores the empty
    table and the column — it does NOT restore the deleted application
    code (``SessionModel``, ``SessionRepository``,
    ``DatabaseSessionRepository``, ``InMemorySessionRepository``,
    ``CaseModel.session`` relationship, case-session-binding methods on
    the case repository). In practice, post-redesign rollback should be
    a full schema reset rather than a stepwise downgrade.
    """
    # 1. Recreate the sessions table first so the FK on cases.session_id
    #    has a valid target.
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "last_accessed",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "LENGTH(TRIM(user_id)) > 0", name="sessions_user_id_not_empty"
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_sessions_organization_id", "sessions", ["organization_id"], unique=False
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    # 2. Add cases.session_id back with FK + index.
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.add_column(
            "cases",
            sa.Column("session_id", sa.String(length=36), nullable=True),
        )
        op.create_foreign_key(
            "cases_session_id_fkey",
            "cases",
            "sessions",
            ["session_id"],
            ["session_id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_cases_session_id", "cases", ["session_id"], unique=False)
    else:
        with op.batch_alter_table("cases") as batch_op:
            batch_op.add_column(
                sa.Column("session_id", sa.String(length=36), nullable=True),
            )
            batch_op.create_foreign_key(
                "cases_session_id_fkey",
                "sessions",
                ["session_id"],
                ["session_id"],
                ondelete="SET NULL",
            )
            batch_op.create_index("ix_cases_session_id", ["session_id"], unique=False)
