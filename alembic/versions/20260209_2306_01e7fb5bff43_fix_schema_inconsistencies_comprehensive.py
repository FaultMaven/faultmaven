"""fix_schema_inconsistencies_comprehensive

Fixes schema inconsistencies between SQLAlchemy models and database schema.

Root Cause Analysis:
- Migration 002 (93e6242a6b94) removed organization_id, created_by, and updated_by
  columns from hypotheses, solutions, and case_messages tables as part of normalization
- However, the SQLAlchemy ORM models (models.py) were NOT updated to reflect this change
- This created a model-schema mismatch causing SQLAlchemy to attempt SELECTs with
  non-existent columns, resulting in 500 errors on session creation

Missing Columns Identified:
1. solutions table:
   - organization_id (String(20), NOT NULL, indexed)
   - created_by (String(255), NOT NULL, indexed)
   - updated_by (String(255), nullable)

2. hypotheses table:
   - organization_id (String(20), NOT NULL, indexed)
   - created_by (String(255), NOT NULL, indexed)
   - updated_by (String(255), nullable)

3. case_messages table:
   - organization_id (String(64), NOT NULL, indexed)

Impact:
- POST /api/v1/cases/{id}/sessions fails with 500 error
- SQLAlchemy query: "SELECT solutions.organization_id ... FROM solutions" fails
- Investigation workflow completely blocked

Fix Strategy:
- Re-add the denormalized columns that SQLAlchemy models expect
- Add indexes to maintain query performance
- Populate with default organization_id for existing rows
- This is a tactical fix; strategic normalization requires model updates

Revision ID: 01e7fb5bff43
Revises: 93e6242a6b94
Create Date: 2026-02-09 23:06:57.699325

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '01e7fb5bff43'
down_revision: Union[str, Sequence[str], None] = '93e6242a6b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Re-add denormalized columns to match SQLAlchemy ORM models.

    This migration re-adds columns that were removed in migration 002
    but are still expected by the SQLAlchemy models in models.py.
    """

    # Default organization ID for single-tenant systems
    default_org_id = '00000000-0000-0000-0000-000000000001'
    default_user = 'system'

    # ===================================================================
    # SOLUTIONS TABLE - Re-add organization_id, created_by, updated_by
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'solutions',
        sa.Column(
            'organization_id',
            sa.String(length=20),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Add created_by column (NOT NULL with default)
    op.add_column(
        'solutions',
        sa.Column(
            'created_by',
            sa.String(length=255),
            nullable=False,
            server_default=default_user
        )
    )

    # Add updated_by column (nullable)
    op.add_column(
        'solutions',
        sa.Column(
            'updated_by',
            sa.String(length=255),
            nullable=True
        )
    )

    # Create indexes for solutions
    op.create_index(
        'ix_solutions_organization_id',
        'solutions',
        ['organization_id'],
        unique=False
    )
    op.create_index(
        'ix_solutions_created_by',
        'solutions',
        ['created_by'],
        unique=False
    )

    # ===================================================================
    # HYPOTHESES TABLE - Re-add organization_id, created_by, updated_by
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'hypotheses',
        sa.Column(
            'organization_id',
            sa.String(length=20),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Add created_by column (NOT NULL with default)
    op.add_column(
        'hypotheses',
        sa.Column(
            'created_by',
            sa.String(length=255),
            nullable=False,
            server_default=default_user
        )
    )

    # Add updated_by column (nullable)
    op.add_column(
        'hypotheses',
        sa.Column(
            'updated_by',
            sa.String(length=255),
            nullable=True
        )
    )

    # Create indexes for hypotheses
    op.create_index(
        'ix_hypotheses_organization_id',
        'hypotheses',
        ['organization_id'],
        unique=False
    )
    op.create_index(
        'ix_hypotheses_created_by',
        'hypotheses',
        ['created_by'],
        unique=False
    )

    # ===================================================================
    # CASE_MESSAGES TABLE - Re-add organization_id
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'case_messages',
        sa.Column(
            'organization_id',
            sa.String(length=64),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Create index for case_messages
    op.create_index(
        'ix_case_messages_organization_id',
        'case_messages',
        ['organization_id'],
        unique=False
    )

    # ===================================================================
    # EVIDENCE TABLE - Re-add organization_id
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'evidence',
        sa.Column(
            'organization_id',
            sa.String(length=64),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Create index for evidence
    op.create_index(
        'ix_evidence_organization_id',
        'evidence',
        ['organization_id'],
        unique=False
    )

    # ===================================================================
    # UPLOADED_FILES TABLE - Re-add organization_id
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'uploaded_files',
        sa.Column(
            'organization_id',
            sa.String(length=64),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Create index for uploaded_files
    op.create_index(
        'ix_uploaded_files_organization_id',
        'uploaded_files',
        ['organization_id'],
        unique=False
    )

    # ===================================================================
    # CASE_STATUS_TRANSITIONS TABLE - Re-add organization_id
    # ===================================================================

    # Add organization_id column (NOT NULL with default)
    op.add_column(
        'case_status_transitions',
        sa.Column(
            'organization_id',
            sa.String(length=64),
            nullable=False,
            server_default=default_org_id
        )
    )

    # Create index for case_status_transitions
    op.create_index(
        'ix_case_status_transitions_organization_id',
        'case_status_transitions',
        ['organization_id'],
        unique=False
    )


def downgrade() -> None:
    """Remove denormalized columns (revert to normalized schema).

    WARNING: This downgrade will fail if migration 002 is also downgraded,
    as it expects these columns to not exist. This is expected behavior -
    migrations must be downgraded in reverse order.
    """

    # Drop indexes first (required before dropping columns)

    # case_status_transitions indexes
    op.drop_index('ix_case_status_transitions_organization_id', table_name='case_status_transitions')

    # uploaded_files indexes
    op.drop_index('ix_uploaded_files_organization_id', table_name='uploaded_files')

    # evidence indexes
    op.drop_index('ix_evidence_organization_id', table_name='evidence')

    # case_messages indexes
    op.drop_index('ix_case_messages_organization_id', table_name='case_messages')

    # hypotheses indexes
    op.drop_index('ix_hypotheses_created_by', table_name='hypotheses')
    op.drop_index('ix_hypotheses_organization_id', table_name='hypotheses')

    # solutions indexes
    op.drop_index('ix_solutions_created_by', table_name='solutions')
    op.drop_index('ix_solutions_organization_id', table_name='solutions')

    # Drop columns

    # case_status_transitions columns
    op.drop_column('case_status_transitions', 'organization_id')

    # uploaded_files columns
    op.drop_column('uploaded_files', 'organization_id')

    # evidence columns
    op.drop_column('evidence', 'organization_id')

    # case_messages columns
    op.drop_column('case_messages', 'organization_id')

    # hypotheses columns
    op.drop_column('hypotheses', 'updated_by')
    op.drop_column('hypotheses', 'created_by')
    op.drop_column('hypotheses', 'organization_id')

    # solutions columns
    op.drop_column('solutions', 'updated_by')
    op.drop_column('solutions', 'created_by')
    op.drop_column('solutions', 'organization_id')
