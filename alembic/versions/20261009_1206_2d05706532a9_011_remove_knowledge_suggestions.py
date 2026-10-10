"""011_remove_knowledge_suggestions

Retires the knowledge-suggestion subsystem's storage (#1897, owner ruling
2026-10-09): ``knowledge_suggestions`` is dropped, and with it the dead column
``knowledge_items.source_suggestion_id``.

``knowledge_suggestions`` held the review queue behind
``POST /cases/{id}/extract-knowledge`` and the six ``/knowledge/suggestions``
routes. Extraction was the queue's only writer, and the routes, the service and
the repository are removed in the same change, so nothing reads or writes the
table any more. A case becomes a runbook through the conversion service only,
which writes ``conversion_jobs`` / ``conversion_drafts``.

``knowledge_items.source_suggestion_id`` was the lineage link from a published
item back to its suggestion. Nothing ever wrote it.

Dialects
--------

``knowledge_suggestions``: ``DROP TABLE`` on both. Nothing references it by
foreign key (it references ``cases``, ``enterprises``, ``organizations``,
``users`` and ``knowledge_items``), so the drop runs no ON DELETE action on
SQLite whatever ``PRAGMA foreign_keys`` says. Its eight indexes go with it. On
PostgreSQL its RLS policy (``knowledge_suggestions_tenant_isolation``, the
baseline's plain tenant policy) is dropped first, by name and dialect-guarded,
so a database whose policy is not the one this revision expects stops here
rather than losing an unknown policy silently; the table drop would remove it
either way. No other revision enrols the table anywhere.

``knowledge_items.source_suggestion_id``: its index
(``ix_knowledge_items_source_suggestion_id``) is dropped first and then the
column, both as plain operations, not a ``batch_alter_table`` rebuild. Once the
index is gone no index, constraint or trigger uses the column, so SQLite drops
it in place (``ALTER TABLE ... DROP COLUMN``, SQLite 3.35+) and PostgreSQL
does the same. A rebuild is avoided on purpose: ``knowledge_items`` carries
CHECK constraints and a GIN index, and is referenced by
``conversion_drafts.knowledge_item_id``, none of which a rebuild needs to
touch to drop one unindexed column. The PostgreSQL RLS policies on
``knowledge_items`` read ``scope``, ``enterprise_id`` and ``organization_id``,
never this column.

``downgrade()``
---------------

Re-adds ``knowledge_items.source_suggestion_id`` (``VARCHAR(36)``, nullable)
with its index, and recreates ``knowledge_suggestions`` exactly as the baseline
(``001_enterprise_baseline``) left it at revision ``afdd293ca6ab``: columns,
CHECKs, foreign keys, primary key, the eight indexes, and on PostgreSQL
row-level security with the plain tenant policy. Both come back EMPTY: the
queue rows and the (always-NULL) column values this revision dropped are not
recoverable, which is the ruling. The re-added column sits last in
``knowledge_items`` rather than where the baseline placed it; column order is
not part of the schema anything compares.

Revision ID: 2d05706532a9
Revises: afdd293ca6ab
Create Date: 2026-10-09 12:06:40.123994

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d05706532a9"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "afdd293ca6ab"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "knowledge_suggestions"
POLICY = f"{TABLE}_tenant_isolation"
ITEMS = "knowledge_items"
COLUMN = "source_suggestion_id"
COLUMN_INDEX = "ix_knowledge_items_source_suggestion_id"

#: The baseline's tenant policy expression, verbatim.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)

#: The baseline's JSON column type: ``TEXT`` on SQLite, ``JSONB`` on PostgreSQL.
_JSON_BLOB = sa.Text().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)

#: The table's single-column indexes, as the baseline created them.
_SUGGESTION_INDEXES = (
    "case_id",
    "created_at",
    "enterprise_id",
    "extracted_by",
    "knowledge_item_id",
    "organization_id",
    "pii_scan_status",
    "status",
)


def upgrade() -> None:
    """Drop the suggestion table, then the dead lineage column."""
    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'DROP POLICY "{POLICY}" ON "{TABLE}"')
    op.drop_table(TABLE)

    op.drop_index(COLUMN_INDEX, table_name=ITEMS)
    op.drop_column(ITEMS, COLUMN)


def downgrade() -> None:
    """Recreate both, empty, exactly as the baseline defined them."""
    op.add_column(ITEMS, sa.Column(COLUMN, sa.String(length=36), nullable=True))
    op.create_index(COLUMN_INDEX, ITEMS, [COLUMN], unique=False)

    op.create_table(
        TABLE,
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column("suggested_title", sa.String(length=512), nullable=False),
        sa.Column("suggested_content", sa.Text(), nullable=False),
        sa.Column(
            "suggested_type",
            sa.String(length=64),
            server_default="troubleshooting_guide",
            nullable=False,
        ),
        sa.Column("extracted_by", sa.String(length=36), nullable=True),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("include_messages", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("include_evidence", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "pii_scan_status",
            sa.String(length=32),
            server_default="not_scanned",
            nullable=False,
        ),
        sa.Column("pii_scan_result", _JSON_BLOB, nullable=True),
        sa.Column("pii_remediated_by", sa.String(length=36), nullable=True),
        sa.Column("pii_remediated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_case_title", sa.String(length=512), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("metadata", _JSON_BLOB, server_default="{}", nullable=False),
        sa.Column("validation_passed", sa.Boolean(), nullable=True),
        sa.Column("validation_errors", _JSON_BLOB, server_default="[]", nullable=False),
        sa.Column(
            "validation_warnings", _JSON_BLOB, server_default="[]", nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
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
        sa.CheckConstraint(
            "pii_scan_status IN ('not_scanned', 'scanning', 'clean', "
            "'pii_detected', 'remediated', 'scan_failed')",
            name="knowledge_suggestions_pii_scan_status_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'draft')",
            name="knowledge_suggestions_status_check",
        ),
        sa.CheckConstraint(
            "evidence_count >= 0", name="knowledge_suggestions_evidence_count_check"
        ),
        sa.CheckConstraint(
            "message_count >= 0", name="knowledge_suggestions_message_count_check"
        ),
        sa.CheckConstraint(
            "version >= 1", name="knowledge_suggestions_version_positive"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["extracted_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"], ["knowledge_items.item_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["pii_remediated_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("suggestion_id"),
    )
    for column in _SUGGESTION_INDEXES:
        op.create_index(f"ix_{TABLE}_{column}", TABLE, [column], unique=False)

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY}" ON "{TABLE}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )
