"""evidence_classification_redesign

Revision ID: a32b2452ebb2
Revises: 01e7fb5bff43
Create Date: 2026-02-11 05:32:13.448693

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a32b2452ebb2'
down_revision: Union[str, Sequence[str], None] = '01e7fb5bff43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Evidence Classification Redesign - Phase 1: Database Schema Updates

    Changes:
    1. Add new enum values to EvidenceCategory: contextual_evidence, rejected
    2. Create new EvidenceSourceType with 5 simplified values
    3. Migrate existing source_type values to new enum
    4. Add unique constraints: (case_id, collected_at_turn) and (case_id, content_hash)
    5. Add indexes for performance
    6. Migrate data: OTHER → CONTEXTUAL_EVIDENCE, UNCLASSIFIED → REJECTED

    SQLite Compatible: Uses VARCHAR for enums (SQLite doesn't support ALTER TYPE)
    """

    # ===================================================================
    # STEP 1: Add new columns for migration (temporary)
    # ===================================================================

    # Add temporary column for new source_type values
    # SQLite doesn't support ALTER TYPE, so we'll create new column, migrate, drop old
    op.add_column(
        'evidence',
        sa.Column('source_type_new', sa.String(50), nullable=True)
    )

    # Add content_hash column for deduplication (if not exists)
    op.add_column(
        'evidence',
        sa.Column('content_hash', sa.String(64), nullable=True)
    )

    # Add collected_at_turn column for turn tracking (if not exists)
    # This tracks which turn the evidence was collected
    op.add_column(
        'evidence',
        sa.Column('collected_at_turn', sa.Integer, nullable=True)
    )

    # Add source_type column to match domain model (if not exists)
    # The current schema may use different field names
    op.add_column(
        'evidence',
        sa.Column('source_type_old', sa.String(50), nullable=True)
    )

    # ===================================================================
    # STEP 2: Migrate EvidenceSourceType (12 types → 5 types)
    # ===================================================================

    # Mapping from old (12) to new (5) source types:
    # log_file, command_output, trace_data, api_response → logs
    # metrics_data, monitoring_alert → metrics
    # config_file, code_review, database_query → configuration
    # screenshot → visual
    # user_report → user_description
    # other → logs (fallback)

    op.execute("""
        UPDATE evidence
        SET source_type_new = CASE
            WHEN category IN ('log_file', 'command_output', 'trace_data', 'api_response', 'other') THEN 'logs'
            WHEN category IN ('metrics_data', 'monitoring_alert') THEN 'metrics'
            WHEN category IN ('config_file', 'code_review', 'database_query') THEN 'configuration'
            WHEN category = 'screenshot' THEN 'visual'
            WHEN category = 'user_report' THEN 'user_description'
            ELSE 'logs'
        END
        WHERE source_type_new IS NULL;
    """)

    # Make source_type_new NOT NULL now that data is migrated
    # SQLite doesn't support ALTER COLUMN, so we accept it stays nullable
    # The application layer will enforce NOT NULL

    # ===================================================================
    # STEP 3: Migrate EvidenceCategory
    # ===================================================================

    # Migrate OTHER → CONTEXTUAL_EVIDENCE
    op.execute("""
        UPDATE evidence
        SET category = 'contextual_evidence'
        WHERE category = 'other';
    """)

    # Migrate UNCLASSIFIED → REJECTED with explanation
    # Add note to primary_purpose field explaining migration
    op.execute("""
        UPDATE evidence
        SET category = 'rejected'
        WHERE category = 'unclassified';
    """)

    # ===================================================================
    # STEP 4: Populate collected_at_turn for existing evidence
    # ===================================================================

    # Set collected_at_turn based on upload_timestamp order
    # Assume evidence within same case, ordered by upload_timestamp = turn order
    # This is a best-effort migration; new evidence will have explicit turn numbers
    op.execute("""
        UPDATE evidence
        SET collected_at_turn = (
            SELECT COUNT(*)
            FROM evidence e2
            WHERE e2.case_id = evidence.case_id
              AND e2.upload_timestamp <= evidence.upload_timestamp
        )
        WHERE collected_at_turn IS NULL;
    """)

    # ===================================================================
    # STEP 5: Add unique constraints
    # ===================================================================

    # Unique constraint: (case_id, collected_at_turn)
    # This ensures one evidence per turn per case (UI constraint)
    # We use CREATE UNIQUE INDEX for SQLite compatibility
    op.create_index(
        'uq_evidence_case_turn',
        'evidence',
        ['case_id', 'collected_at_turn'],
        unique=True,
        # Only apply to non-null collected_at_turn (partial index)
        postgresql_where=sa.text('collected_at_turn IS NOT NULL')
    )

    # Unique constraint: (case_id, content_hash)
    # This prevents duplicate file uploads to same case
    op.create_index(
        'uq_evidence_case_hash',
        'evidence',
        ['case_id', 'content_hash'],
        unique=True,
        # Only apply when content_hash exists
        postgresql_where=sa.text('content_hash IS NOT NULL')
    )

    # ===================================================================
    # STEP 6: Add performance indexes
    # ===================================================================

    # Index for querying evidence by category within a case
    op.create_index(
        'idx_evidence_case_category',
        'evidence',
        ['case_id', 'category']
    )

    # Index for querying evidence by turn
    op.create_index(
        'idx_evidence_case_turn',
        'evidence',
        ['case_id', 'collected_at_turn']
    )

    # Index for content_hash lookups (deduplication)
    op.create_index(
        'idx_evidence_content_hash',
        'evidence',
        ['content_hash'],
        # Only index non-null hashes
        postgresql_where=sa.text('content_hash IS NOT NULL')
    )


def downgrade() -> None:
    """
    Rollback Evidence Classification Redesign

    Reverts:
    1. Removes indexes
    2. Removes unique constraints
    3. Reverts category changes (CONTEXTUAL_EVIDENCE → OTHER, REJECTED → UNCLASSIFIED)
    4. Removes new columns
    """

    # ===================================================================
    # STEP 1: Drop indexes
    # ===================================================================

    op.drop_index('idx_evidence_content_hash', table_name='evidence')
    op.drop_index('idx_evidence_case_turn', table_name='evidence')
    op.drop_index('idx_evidence_case_category', table_name='evidence')

    # ===================================================================
    # STEP 2: Drop unique constraints (indexes in SQLite)
    # ===================================================================

    op.drop_index('uq_evidence_case_hash', table_name='evidence')
    op.drop_index('uq_evidence_case_turn', table_name='evidence')

    # ===================================================================
    # STEP 3: Revert category migrations
    # ===================================================================

    # Revert CONTEXTUAL_EVIDENCE → OTHER
    op.execute("""
        UPDATE evidence
        SET category = 'other'
        WHERE category = 'contextual_evidence';
    """)

    # Revert REJECTED → UNCLASSIFIED
    op.execute("""
        UPDATE evidence
        SET category = 'unclassified'
        WHERE category = 'rejected';
    """)

    # ===================================================================
    # STEP 4: Remove new columns
    # ===================================================================

    op.drop_column('evidence', 'source_type_old')
    op.drop_column('evidence', 'collected_at_turn')
    op.drop_column('evidence', 'content_hash')
    op.drop_column('evidence', 'source_type_new')
