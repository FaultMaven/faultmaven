"""007_problem_status_single_source

Hypotheses are formed only on a verified problem, and "is the problem
verified" has one stored source.

Two changes, both on data the engine writes as JSON or as a constrained column.

``cases.progress``
------------------

The progress blob stored ``symptom_verified`` as a boolean. It now stores
``problem_status`` (``unverified`` | ``verified``), and ``symptom_verified`` is a
read-only property derived from it, so the key is moved: ``true`` becomes
``verified``, anything else ``unverified``, and the old key is removed. A blob
without the key is left alone; it loads with the field's default,
``unverified``, which is what a missing key meant before.

``hypotheses.state``
--------------------

The ``captured`` state is gone. It was the queue for hypotheses formed before
the symptom was verified; the engine now refuses those instead. Rows still in it
were never activated, so they become ``retired`` with a reason naming this
revision, and the CHECK and the column default lose the value
(``hypotheses_state_check``; default ``active``).

PostgreSQL alters the constraint and the default in place. SQLite cannot, so
the table is rebuilt (``batch_alter_table`` from a frozen copy of the baseline
definition — migrations are history, so it is not built from the ORM).
``hypothesis_evidence`` (``ON DELETE CASCADE``) and ``solutions``
(``ON DELETE SET NULL``) reference this table, and SQLite runs those actions
when a referenced table is dropped if foreign keys are enforced. ``env.py`` opens its own engine without
``PRAGMA foreign_keys=ON``, so they are not, and the rebuild deletes nothing;
the revision checks that and refuses to run otherwise rather than cascade.

Row-level security
------------------

``cases`` and ``hypotheses`` are tenant-scoped. On PostgreSQL the UPDATEs run
under ``SET LOCAL row_security = off``, restored to ``DEFAULT`` afterwards — the
same choice and reasoning as revisions 003 and 006: the migrating role owns the
tables and is exempt, and a role the policy would filter raises instead of
silently updating one enterprise's rows.

What it cannot reach
--------------------

``case_checkpoints`` snapshots keep the old key. They are written once and
never restored, so nothing loads them as a case. Pods still running the
previous image during a rolling deploy write the old key after this revision
has run; such a blob loads ``unverified`` (the key is ignored), and the next
symptom claim verifies it again.

Revision ID: 497ae8900ae2
Revises: f37066de2792
Create Date: 2026-10-06 12:00:00
"""

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "497ae8900ae2"
down_revision: Union[str, Sequence[str], None] = "f37066de2792"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

#: The reason a queued row is retired with; downgrade() reads it back.
RETIRED_FROM_QUEUE_REASON = (
    "Formed before the symptom was verified and never activated "
    "(the pre-verification queue was removed in revision 007)."
)

STATE_CHECK = "hypotheses_state_check"
STATES_WITHOUT_CAPTURED = (
    "state IN ('active', 'validated', 'refuted', 'inconclusive', 'retired')"
)
STATES_WITH_CAPTURED = (
    "state IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', "
    "'retired')"
)

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

RETIRE_QUEUED = text(
    "UPDATE hypotheses SET state = 'retired', retirement_reason = :reason "
    "WHERE state = 'captured'"
).bindparams(reason=RETIRED_FROM_QUEUE_REASON)
REQUEUE_RETIRED = text(
    "UPDATE hypotheses SET state = 'captured', retirement_reason = NULL "
    "WHERE state = 'retired' AND retirement_reason = :reason"
).bindparams(reason=RETIRED_FROM_QUEUE_REASON)

#: Per dialect: move ``symptom_verified`` to ``problem_status`` and back.
PROGRESS_FORWARD = {
    "sqlite": (
        "UPDATE cases SET progress = json_remove("
        "json_set(progress, '$.problem_status', "
        "CASE WHEN json_extract(progress, '$.symptom_verified') "
        "THEN 'verified' ELSE 'unverified' END), "
        "'$.symptom_verified') "
        "WHERE json_type(progress, '$.symptom_verified') IS NOT NULL"
    ),
    "postgresql": (
        "UPDATE cases SET progress = (progress - 'symptom_verified') "
        "|| jsonb_build_object('problem_status', "
        "CASE WHEN (progress->>'symptom_verified')::boolean "
        "THEN 'verified' ELSE 'unverified' END) "
        "WHERE progress->'symptom_verified' IS NOT NULL"
    ),
}
PROGRESS_BACKWARD = {
    "sqlite": (
        "UPDATE cases SET progress = json_remove("
        "json_set(progress, '$.symptom_verified', "
        "json(CASE WHEN json_extract(progress, '$.problem_status') = 'verified' "
        "THEN 'true' ELSE 'false' END)), "
        "'$.problem_status') "
        "WHERE json_type(progress, '$.problem_status') IS NOT NULL"
    ),
    "postgresql": (
        "UPDATE cases SET progress = (progress - 'problem_status') "
        "|| jsonb_build_object('symptom_verified', "
        "progress->>'problem_status' = 'verified') "
        "WHERE progress->'problem_status' IS NOT NULL"
    ),
}


def _json_column() -> sa.types.TypeEngine:
    return sa.Text().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _frozen_hypotheses(state_check: str, state_default: str) -> sa.Table:
    """The baseline ``hypotheses`` table, with the given state CHECK/default.

    Frozen here because migrations are history: the ORM moves on, this does not.
    """
    meta = sa.MetaData()
    table = sa.Table(
        "hypotheses",
        meta,
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("root_node_id", sa.String(length=36), nullable=True),
        sa.Column("path", _json_column(), server_default="[]", nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default=state_default,
            nullable=False,
        ),
        sa.Column(
            "likelihood",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column(
            "initial_likelihood",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "generation_mode",
            sa.String(length=20),
            server_default="systematic",
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("retirement_reason", sa.Text(), nullable=True),
        sa.Column("refutation_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "generated_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_updated_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_progress_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "iterations_without_progress",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("metadata", _json_column(), server_default="{}", nullable=False),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(state_check, name=STATE_CHECK),
        sa.CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="hypotheses_statement_not_empty"
        ),
        sa.CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="hypotheses_likelihood_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["root_node_id"], ["causal_nodes.node_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("hypothesis_id"),
    )
    for column in (
        "case_id",
        "category",
        "created_by",
        "enterprise_id",
        "organization_id",
        "root_node_id",
        "state",
    ):
        sa.Index(f"ix_hypotheses_{column}", table.c[column])
    return table


def _execute_counted(statement, label: str) -> None:
    """Run one data UPDATE; log its row count when there is a connection."""
    if op.get_context().as_sql:
        op.execute(statement)
        return
    count = op.get_bind().execute(statement).rowcount
    _log.info("007: %s on %d row(s)", label, count)


def _refuse_cascading_rebuild() -> None:
    """SQLite runs ON DELETE actions when a referenced table is dropped if
    foreign keys are enforced; the rebuild would then delete every
    hypothesis_evidence row and unlink every solution. env.py does not enforce them — refuse if
    something has turned them on."""
    if op.get_context().as_sql:
        return
    enforced = op.get_bind().execute(text("PRAGMA foreign_keys")).scalar()
    if enforced:
        raise RuntimeError(
            "007 rebuilds the hypotheses table on SQLite and must run with "
            "PRAGMA foreign_keys=OFF: with it on, dropping the old table runs "
            "the ON DELETE actions of hypothesis_evidence (CASCADE) and "
            "solutions (SET NULL)."
        )


def _set_state_constraint(*, check: str, default: str, old_check: str) -> None:
    if op.get_context().dialect.name == "postgresql":
        op.drop_constraint(STATE_CHECK, "hypotheses", type_="check")
        op.create_check_constraint(STATE_CHECK, "hypotheses", check)
        op.alter_column("hypotheses", "state", server_default=default)
        return
    _refuse_cascading_rebuild()
    old_default = "captured" if default == "active" else "active"
    with op.batch_alter_table(
        "hypotheses",
        copy_from=_frozen_hypotheses(old_check, old_default),
        recreate="always",
    ) as batch:
        batch.drop_constraint(STATE_CHECK, type_="check")
        batch.create_check_constraint(STATE_CHECK, check)
        batch.alter_column(
            "state",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            server_default=default,
        )


def upgrade() -> None:
    """Move ``symptom_verified`` to ``problem_status``; retire queued
    hypotheses and drop ``captured`` from the state CHECK and default."""
    dialect = op.get_context().dialect.name
    postgresql_dialect = dialect == "postgresql"
    if postgresql_dialect:
        op.execute(ROW_SECURITY_OFF)
    _execute_counted(text(PROGRESS_FORWARD[dialect]), "moved symptom_verified")
    _execute_counted(RETIRE_QUEUED, "retired queued hypotheses")
    if postgresql_dialect:
        op.execute(ROW_SECURITY_RESTORED)
    _set_state_constraint(
        check=STATES_WITHOUT_CAPTURED,
        default="active",
        old_check=STATES_WITH_CAPTURED,
    )


def downgrade() -> None:
    """Restore ``captured`` to the CHECK and default, re-queue exactly the rows
    this revision retired, and move ``problem_status`` back to
    ``symptom_verified``."""
    dialect = op.get_context().dialect.name
    postgresql_dialect = dialect == "postgresql"
    _set_state_constraint(
        check=STATES_WITH_CAPTURED,
        default="captured",
        old_check=STATES_WITHOUT_CAPTURED,
    )
    if postgresql_dialect:
        op.execute(ROW_SECURITY_OFF)
    _execute_counted(REQUEUE_RETIRED, "re-queued hypotheses")
    _execute_counted(text(PROGRESS_BACKWARD[dialect]), "restored symptom_verified")
    if postgresql_dialect:
        op.execute(ROW_SECURITY_RESTORED)
