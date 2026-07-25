"""conversion_drafts: replace status CHECK to include 'discarded'

The DraftStatus enum was updated (I7 fix) to use DISCARDED="discarded"
instead of DELETED="deleted", but the DB CHECK constraint was not updated.
Any call to conversion_service.scan_for_runbooks() that tries to set
status='discarded' raises IntegrityError.

New valid set: draft | verified | discarded
Old valid set: draft | verified | rejected | archived

Existing rows with 'rejected' or 'archived' status are mapped to 'discarded'.

Revision ID: 9c8b7a6d5e4f
Revises: 0b5e8c4f7d29
Create Date: 2026-05-12 19:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c8b7a6d5e4f"
down_revision: Union[str, Sequence[str], None] = "0b5e8c4f7d29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        # SQLite cannot modify CHECK constraints via ALTER TABLE.
        # Recreate the table with the updated constraint.
        bind.execute(sa.text("PRAGMA foreign_keys=OFF"))

        bind.execute(sa.text("""
            CREATE TABLE conversion_drafts_new (
                id VARCHAR(36) NOT NULL,
                organization_id VARCHAR(36) NOT NULL,
                conversion_id VARCHAR(36) NOT NULL,
                knowledge_item_id VARCHAR(36),
                verified_by VARCHAR(36),
                runbook_id VARCHAR(100) NOT NULL,
                title VARCHAR(255) NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                source_type VARCHAR(20) NOT NULL DEFAULT 'document',
                document_type VARCHAR(50) DEFAULT 'runbook',
                domain VARCHAR(50),
                service VARCHAR(100),
                severity VARCHAR(20),
                tags TEXT,
                validation_passed BOOLEAN NOT NULL DEFAULT 1,
                validation_errors JSON,
                validation_warnings JSON,
                quality_score NUMERIC(5, 1),
                quality_details JSON,
                created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                verified_at DATETIME,
                PRIMARY KEY (id),
                CONSTRAINT conversion_drafts_severity_check
                    CHECK (severity IS NULL OR severity IN ('low', 'medium', 'high', 'critical')),
                CONSTRAINT conversion_drafts_status_check
                    CHECK (status IN ('draft', 'verified', 'discarded')),
                FOREIGN KEY (conversion_id) REFERENCES conversion_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY (knowledge_item_id) REFERENCES knowledge_items(item_id) ON DELETE SET NULL,
                FOREIGN KEY (organization_id) REFERENCES organizations(organization_id) ON DELETE CASCADE,
                FOREIGN KEY (verified_by) REFERENCES users(user_id) ON DELETE SET NULL
            )
        """))

        # Copy data; map deprecated status values to 'discarded'
        bind.execute(sa.text("""
            INSERT INTO conversion_drafts_new
            SELECT
                id, organization_id, conversion_id, knowledge_item_id, verified_by,
                runbook_id, title, file_path,
                CASE status
                    WHEN 'rejected' THEN 'discarded'
                    WHEN 'archived' THEN 'discarded'
                    ELSE status
                END,
                source_type, document_type, domain, service, severity, tags,
                validation_passed, validation_errors, validation_warnings,
                quality_score, quality_details, created_at, verified_at
            FROM conversion_drafts
        """))

        bind.execute(sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_conversion_id"))
        bind.execute(
            sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_organization_id")
        )
        bind.execute(sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_tags"))
        bind.execute(sa.text("DROP TABLE conversion_drafts"))
        bind.execute(
            sa.text("ALTER TABLE conversion_drafts_new RENAME TO conversion_drafts")
        )

        bind.execute(
            sa.text(
                "CREATE INDEX ix_conversion_drafts_conversion_id ON conversion_drafts(conversion_id)"
            )
        )
        bind.execute(
            sa.text(
                "CREATE INDEX ix_conversion_drafts_organization_id ON conversion_drafts(organization_id)"
            )
        )

        bind.execute(sa.text("PRAGMA foreign_keys=ON"))

    else:
        # PostgreSQL: ALTER TABLE supports DROP/ADD CONSTRAINT directly.
        op.drop_constraint(
            "conversion_drafts_status_check", "conversion_drafts", type_="check"
        )
        bind.execute(
            sa.text(
                "UPDATE conversion_drafts SET status = 'discarded' WHERE status IN ('rejected', 'archived')"
            )
        )
        op.create_check_constraint(
            "conversion_drafts_status_check",
            "conversion_drafts",
            "status IN ('draft', 'verified', 'discarded')",
        )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        bind.execute(sa.text("PRAGMA foreign_keys=OFF"))

        bind.execute(sa.text("""
            CREATE TABLE conversion_drafts_old (
                id VARCHAR(36) NOT NULL,
                organization_id VARCHAR(36) NOT NULL,
                conversion_id VARCHAR(36) NOT NULL,
                knowledge_item_id VARCHAR(36),
                verified_by VARCHAR(36),
                runbook_id VARCHAR(100) NOT NULL,
                title VARCHAR(255) NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                source_type VARCHAR(20) NOT NULL DEFAULT 'document',
                document_type VARCHAR(50) DEFAULT 'runbook',
                domain VARCHAR(50),
                service VARCHAR(100),
                severity VARCHAR(20),
                tags TEXT,
                validation_passed BOOLEAN NOT NULL DEFAULT 1,
                validation_errors JSON,
                validation_warnings JSON,
                quality_score NUMERIC(5, 1),
                quality_details JSON,
                created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                verified_at DATETIME,
                PRIMARY KEY (id),
                CONSTRAINT conversion_drafts_severity_check
                    CHECK (severity IS NULL OR severity IN ('low', 'medium', 'high', 'critical')),
                CONSTRAINT conversion_drafts_status_check
                    CHECK (status IN ('draft', 'verified', 'rejected', 'archived')),
                FOREIGN KEY (conversion_id) REFERENCES conversion_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY (knowledge_item_id) REFERENCES knowledge_items(item_id) ON DELETE SET NULL,
                FOREIGN KEY (organization_id) REFERENCES organizations(organization_id) ON DELETE CASCADE,
                FOREIGN KEY (verified_by) REFERENCES users(user_id) ON DELETE SET NULL
            )
        """))

        # 'discarded' maps back to 'archived' on downgrade
        bind.execute(sa.text("""
            INSERT INTO conversion_drafts_old
            SELECT
                id, organization_id, conversion_id, knowledge_item_id, verified_by,
                runbook_id, title, file_path,
                CASE status WHEN 'discarded' THEN 'archived' ELSE status END,
                source_type, document_type, domain, service, severity, tags,
                validation_passed, validation_errors, validation_warnings,
                quality_score, quality_details, created_at, verified_at
            FROM conversion_drafts
        """))

        bind.execute(sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_conversion_id"))
        bind.execute(
            sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_organization_id")
        )
        bind.execute(sa.text("DROP INDEX IF EXISTS ix_conversion_drafts_tags"))
        bind.execute(sa.text("DROP TABLE conversion_drafts"))
        bind.execute(
            sa.text("ALTER TABLE conversion_drafts_old RENAME TO conversion_drafts")
        )

        bind.execute(
            sa.text(
                "CREATE INDEX ix_conversion_drafts_conversion_id ON conversion_drafts(conversion_id)"
            )
        )
        bind.execute(
            sa.text(
                "CREATE INDEX ix_conversion_drafts_organization_id ON conversion_drafts(organization_id)"
            )
        )

        bind.execute(sa.text("PRAGMA foreign_keys=ON"))

    else:
        op.drop_constraint(
            "conversion_drafts_status_check", "conversion_drafts", type_="check"
        )
        bind.execute(
            sa.text(
                "UPDATE conversion_drafts SET status = 'archived' WHERE status = 'discarded'"
            )
        )
        op.create_check_constraint(
            "conversion_drafts_status_check",
            "conversion_drafts",
            "status IN ('draft', 'verified', 'rejected', 'archived')",
        )
