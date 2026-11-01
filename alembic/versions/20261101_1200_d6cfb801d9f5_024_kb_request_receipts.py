"""024_kb_request_receipts

The KB request receipt (#2433): ``kb_request_receipts``, one row per keyed
``POST /knowledge/documents`` or ``POST /knowledge/convert`` that published,
written in the transaction that publishes it: the author, the route, the key,
a sha256 fingerprint of the form fields and the file's content, and the
response the client was sent. A retry with the key is answered from it.

The key is ``(enterprise_id, author_id, route, idempotency_key)``, the
enterprise first because RLS scopes the table on it. ``author_id`` has no
foreign key. ``response`` is ``json``, not ``jsonb``, on PostgreSQL, so a
replay keeps its key order. The subject is ``knowledge_item_id`` (an upload)
or ``conversion_id`` (a conversion), exactly the one ``route`` names (a CHECK),
each ``ON DELETE CASCADE`` and indexed: a receipt lives exactly as long as what
it describes, with no retention job.

PostgreSQL: the plain tenant policy, enrolled as 010 enrols ``turn_receipts``;
no GRANT (the app role's default privileges cover it). SQLite gets the table,
the CHECKs and the indexes. ``downgrade()`` drops the table.

Revision ID: d6cfb801d9f5
Revises: cf17b34fc8c4
Create Date: 2026-11-01 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d6cfb801d9f5"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "cf17b34fc8c4"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "kb_request_receipts"

#: Frozen copy of the baseline's policy predicate. Migrations are history; they
#: state the text they were written against rather than importing it.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)


def upgrade() -> None:
    """Create ``kb_request_receipts`` and, on PostgreSQL, enrol it in RLS."""
    op.create_table(
        TABLE,
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=False),
        sa.Column("route", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
        sa.Column("conversion_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "route IN ('upload', 'convert')", name="kb_request_receipts_route_check"
        ),
        sa.CheckConstraint(
            "(route = 'upload' AND knowledge_item_id IS NOT NULL "
            "AND conversion_id IS NULL) OR (route = 'convert' "
            "AND conversion_id IS NOT NULL AND knowledge_item_id IS NULL)",
            name="kb_request_receipts_subject_check",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"], ["knowledge_items.item_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversion_id"], ["conversion_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "enterprise_id", "author_id", "route", "idempotency_key"
        ),
    )
    op.create_index(
        "ix_kb_request_receipts_item", TABLE, ["knowledge_item_id"], unique=False
    )
    op.create_index(
        "ix_kb_request_receipts_conversion", TABLE, ["conversion_id"], unique=False
    )

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{TABLE}_tenant_isolation" ON "{TABLE}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )


def downgrade() -> None:
    """Drop ``kb_request_receipts``. Its policy and indexes go with it."""
    op.drop_index("ix_kb_request_receipts_conversion", table_name=TABLE)
    op.drop_index("ix_kb_request_receipts_item", table_name=TABLE)
    op.drop_table(TABLE)
