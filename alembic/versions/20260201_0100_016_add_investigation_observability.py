"""Add investigation workflow observability fields

Revision ID: 016_add_investigation_observability
Revises: 015_rename_consulting_to_inquiry
Create Date: 2026-02-01

This migration adds new observability fields and renames fields for
investigation workflow alignment:

New Fields:
- turn_progress.stagnation_detected (VARCHAR)
- turn_progress.validation_repairs (JSON/TEXT)
- turn_progress.momentum (VARCHAR)
- turn_progress.blocked_reasons (JSON/TEXT)
- turn_progress.next_steps (JSON/TEXT)

Renamed Fields:
- hypothesis_evidence_link.completeness -> stance_confidence
- investigation_progress.resolution_applied -> solution_applied
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '016_add_investigation_observability'
down_revision = '015_rename_consulting_to_inquiry'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add observability fields to turn_progress table
    # Note: Using batch_alter_table for SQLite compatibility

    with op.batch_alter_table('turn_progress', schema=None) as batch_op:
        # Add stagnation_detected field
        batch_op.add_column(sa.Column(
            'stagnation_detected',
            sa.String(50),
            nullable=True,
            comment='Stagnation type if detected: no_progress, hypothesis_anchoring, action_loop, hypothesis_deadlock'
        ))

        # Add validation_repairs field (JSON stored as TEXT for SQLite compat)
        batch_op.add_column(sa.Column(
            'validation_repairs',
            sa.Text(),
            nullable=True,
            default='[]',
            comment='JSON array of state repairs made by StateValidator'
        ))

        # Add momentum field
        batch_op.add_column(sa.Column(
            'momentum',
            sa.String(20),
            nullable=True,
            comment='Investigation momentum: high, moderate, low, blocked'
        ))

        # Add blocked_reasons field (JSON stored as TEXT for SQLite compat)
        batch_op.add_column(sa.Column(
            'blocked_reasons',
            sa.Text(),
            nullable=True,
            default='[]',
            comment='JSON array of reasons investigation is blocked'
        ))

        # Add next_steps field (JSON stored as TEXT for SQLite compat)
        batch_op.add_column(sa.Column(
            'next_steps',
            sa.Text(),
            nullable=True,
            default='[]',
            comment='JSON array of suggested next steps'
        ))

    # Rename completeness -> stance_confidence in hypothesis_evidence_link
    with op.batch_alter_table('hypothesis_evidence_link', schema=None) as batch_op:
        batch_op.alter_column(
            'completeness',
            new_column_name='stance_confidence',
            existing_type=sa.Float(),
            comment='Confidence in stance assessment (0.0-1.0)'
        )

    # Rename resolution_applied -> solution_applied in investigation_progress
    # Note: investigation_progress may be stored as JSON in cases table
    # This handles the case where it's a separate table
    try:
        with op.batch_alter_table('investigation_progress', schema=None) as batch_op:
            batch_op.alter_column(
                'resolution_applied',
                new_column_name='solution_applied',
                existing_type=sa.Boolean(),
                comment='Solution has been applied by user'
            )
    except Exception:
        # Table may not exist if progress is stored as JSON in cases
        pass


def downgrade() -> None:
    # Revert solution_applied -> resolution_applied
    try:
        with op.batch_alter_table('investigation_progress', schema=None) as batch_op:
            batch_op.alter_column(
                'solution_applied',
                new_column_name='resolution_applied',
                existing_type=sa.Boolean()
            )
    except Exception:
        pass

    # Revert stance_confidence -> completeness
    with op.batch_alter_table('hypothesis_evidence_link', schema=None) as batch_op:
        batch_op.alter_column(
            'stance_confidence',
            new_column_name='completeness',
            existing_type=sa.Float()
        )

    # Remove observability fields from turn_progress
    with op.batch_alter_table('turn_progress', schema=None) as batch_op:
        batch_op.drop_column('next_steps')
        batch_op.drop_column('blocked_reasons')
        batch_op.drop_column('momentum')
        batch_op.drop_column('validation_repairs')
        batch_op.drop_column('stagnation_detected')
