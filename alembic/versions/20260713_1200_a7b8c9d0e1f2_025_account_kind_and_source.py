"""025: account_kind on users + derived source on cases (ADR-012 two-account model)

Adds:
- ``users.account_kind`` ∈ {individual, slack} (default 'individual')
- ``cases.source`` ∈ {copilot, slack, api} (default 'copilot'), stamped at
  case creation from the creator's account_kind.

Data backfill (pre-production, clean forward migration — no shims):
- the ``slack-agent`` service user becomes ``account_kind='slack'``
- existing cases owned by a slack account become ``source='slack'``; everything
  else stays 'copilot' (the column default).

Origin is a data field, never encoded into ``case_id`` (ADR-012).

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
"""

import sqlalchemy as sa

from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "account_kind",
            sa.String(20),
            nullable=False,
            server_default="individual",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "source",
            sa.String(20),
            nullable=False,
            server_default="copilot",
        ),
    )
    op.create_index("ix_cases_source", "cases", ["source"])

    # Backfill: tag the Slack service account, then its cases.
    op.execute("UPDATE users SET account_kind = 'slack' WHERE username = 'slack-agent'")
    op.execute(
        "UPDATE cases SET source = 'slack' "
        "WHERE user_id IN (SELECT user_id FROM users WHERE account_kind = 'slack')"
    )


def downgrade() -> None:
    op.drop_index("ix_cases_source", table_name="cases")
    op.drop_column("cases", "source")
    op.drop_column("users", "account_kind")
