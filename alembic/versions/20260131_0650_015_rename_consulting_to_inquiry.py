"""Rename consulting to inquiry and update status values.

Revision ID: 015_rename_consulting_to_inquiry
Revises: 014_drop_invalid_message_constraint
Create Date: 2026-01-31 06:50:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '015_rename_consulting_to_inquiry'
down_revision = '014_drop_message_constraint'
branch_labels = None
depends_on = None


def upgrade():
    # Rename 'consulting' column to 'inquiry' in 'cases' table
    # We use batch_alter_table for SQLite compatibility
    # Note: org_id was already renamed to organization_id in migration 012
    # and inquiry was renamed to consulting in migration 012 if it came from sqlite baseline.
    # Now we are standardizing on 'inquiry' everywhere.
    
    # We need to be careful with batch_alter_table as it fails if the column doesn't exist
    # in the reflected metadata.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('cases')]
    
    if 'consulting' in columns:
        with op.batch_alter_table("cases") as batch_op:
            batch_op.alter_column('consulting', new_column_name='inquiry')
    
    # Update 'cases.status' values: 'consulting' -> 'inquiry'
    op.execute("UPDATE cases SET status = 'inquiry' WHERE status = 'consulting'")


def downgrade():
    # Revert 'cases.status' values: 'inquiry' -> 'consulting'
    op.execute("UPDATE cases SET status = 'consulting' WHERE status = 'inquiry'")

    # Rename 'inquiry' column back to 'consulting' in 'cases' table
    with op.batch_alter_table("cases") as batch_op:
        batch_op.alter_column('inquiry', new_column_name='consulting')
