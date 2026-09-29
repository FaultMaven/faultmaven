"""002_llm_usage_ledger

The LLM usage ledger (#640): ``llm_usage_daily`` and ``llm_turn_spend``.

The chain's first additive revision on top of ``001_enterprise_baseline``
(ruled 2026-09-28): the baseline is not amended in place, a deployment receives
these two tables through its normal migration run, and no data moves.

* ``llm_usage_daily`` — billed LLM calls summed per (enterprise, UTC day,
  billing subject, actor, provider, model, outcome): tokens in four disjoint
  buckets, the cost estimated at call time, the call count and the calls that
  had no price.
* ``llm_turn_spend`` — one row per engine turn that made a billed call,
  addressed by the message clock and deleted with its case.

**No key column is nullable** on either table: ``actor_user_id`` is ``''`` when
there is no actor and ``billing_subject_id`` is ``''`` exactly when the kind is
``none`` (a CHECK ties the two). A NULL in an ``ON CONFLICT`` target never
conflicts on SQLite, so a nullable key would insert a row per write instead of
incrementing one.

**No foreign key on the actor or the billing subject**, as for
``turn_usage.billing_subject_id``: user deletion is a hard delete, ``SET NULL``
would merge rows and ``CASCADE`` would erase spend from the enterprise totals.

Both tables carry ``enterprise_id`` NOT NULL with a foreign key to
``enterprises`` and, on PostgreSQL, the plain tenant-isolation policy the
baseline gives every tenant-scoped table (no ``FOR`` clause, so ``USING`` is also
the ``WITH CHECK``). SQLite (standalone) gets the tables and the CHECKs and no
RLS, the line every migration draws. No GRANT: the app role's ``ALTER DEFAULT
PRIVILEGES`` covers tables a later migration creates.

``downgrade()`` drops both tables; their policies go with them.

Revision ID: 65913afe773c
Revises: a1e0c17bd001
Create Date: 2026-09-29 00:19:54.524372

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "65913afe773c"
down_revision: Union[str, Sequence[str], None] = "a1e0c17bd001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Frozen copy of the baseline's policy predicate. Migrations are history; they
#: state the text they were written against rather than importing it.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)

_TABLES = ("llm_usage_daily", "llm_turn_spend")

_SUBJECT_KIND_CHECK = "billing_subject_kind IN ('organization', 'account', 'none')"
_SUBJECT_ID_CHECK = "(billing_subject_kind = 'none') = (billing_subject_id = '')"


def _counter(name: str, kind=sa.Integer) -> sa.Column:
    return sa.Column(name, kind(), server_default=sa.text("0"), nullable=False)


def upgrade() -> None:
    """Create the two ledger tables and, on PostgreSQL, enrol them in RLS."""
    op.create_table(
        "llm_usage_daily",
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("billing_subject_kind", sa.String(length=20), nullable=False),
        sa.Column("billing_subject_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        _counter("input_tokens", sa.BigInteger),
        _counter("output_tokens", sa.BigInteger),
        _counter("cache_read_tokens", sa.BigInteger),
        _counter("cache_write_tokens", sa.BigInteger),
        _counter("estimated_cost_usd", sa.Float),
        _counter("calls"),
        _counter("unpriced_calls"),
        sa.CheckConstraint(
            _SUBJECT_KIND_CHECK, name="llm_usage_daily_subject_kind_check"
        ),
        sa.CheckConstraint(_SUBJECT_ID_CHECK, name="llm_usage_daily_subject_id_check"),
        sa.CheckConstraint(
            "outcome IN ('kept', 'low_confidence')",
            name="llm_usage_daily_outcome_check",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND calls >= 0 AND unpriced_calls >= 0",
            name="llm_usage_daily_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "enterprise_id",
            "usage_date",
            "billing_subject_kind",
            "billing_subject_id",
            "actor_user_id",
            "provider",
            "model",
            "outcome",
        ),
    )
    op.create_table(
        "llm_turn_spend",
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        _counter("investigation_turn"),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("billing_subject_kind", sa.String(length=20), nullable=False),
        sa.Column(
            "billing_subject_id",
            sa.String(length=36),
            server_default=sa.text("''"),
            nullable=False,
        ),
        _counter("input_tokens", sa.BigInteger),
        _counter("output_tokens", sa.BigInteger),
        _counter("cache_read_tokens", sa.BigInteger),
        _counter("cache_write_tokens", sa.BigInteger),
        _counter("spend_weighted_tokens", sa.BigInteger),
        _counter("calls"),
        _counter("low_confidence_calls"),
        _counter("unpriced_calls"),
        _counter("estimated_cost_usd", sa.Float),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _SUBJECT_KIND_CHECK, name="llm_turn_spend_subject_kind_check"
        ),
        sa.CheckConstraint(_SUBJECT_ID_CHECK, name="llm_turn_spend_subject_id_check"),
        sa.CheckConstraint(
            "turn_number >= 0 AND investigation_turn >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND spend_weighted_tokens >= 0 "
            "AND calls >= 0 AND low_confidence_calls >= 0 AND unpriced_calls >= 0",
            name="llm_turn_spend_non_negative",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("enterprise_id", "case_id", "turn_number"),
    )
    op.create_index(
        "ix_llm_turn_spend_enterprise_occurred",
        "llm_turn_spend",
        ["enterprise_id", "occurred_at"],
        unique=False,
    )

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
                f"USING ({_ENTERPRISE_MATCHES_SESSION})"
            )


def downgrade() -> None:
    """Drop both tables. Each table's policy is dropped with it."""
    op.drop_index("ix_llm_turn_spend_enterprise_occurred", table_name="llm_turn_spend")
    op.drop_table("llm_turn_spend")
    op.drop_table("llm_usage_daily")
