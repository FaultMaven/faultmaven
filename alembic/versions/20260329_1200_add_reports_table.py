"""Add reports table for persistent case documentation storage

Revision ID: c8d4e2f1a3b7
Revises: b7f3c1d2e5a8
Create Date: 2026-03-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d4e2f1a3b7'
down_revision: Union[str, Sequence[str], None] = 'b7f3c1d2e5a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create reports table.

    Columns match the raw SQL queries in sqlite_case_repository.py and
    postgresql_hybrid_case_repository.py:
        report_id, case_id, report_type, version, is_current,
        linked_to_closure, title, content, format,
        generation_status, generation_time_ms, metadata,
        generated_at, updated_at
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    if "reports" not in existing_tables:
        op.create_table(
            'reports',
            sa.Column('report_id', sa.String(length=36), nullable=False),
            sa.Column('case_id', sa.String(length=17), nullable=False),
            sa.Column('report_type', sa.String(length=30), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('is_current', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('linked_to_closure', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('format', sa.String(length=20), nullable=False, server_default='markdown'),
            sa.Column('generation_status', sa.String(length=20), nullable=False),
            sa.Column('generation_time_ms', sa.Integer(), nullable=False),
            sa.Column('metadata', sa.JSON(), nullable=True),
            sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('report_id'),
            sa.CheckConstraint('version >= 1 AND version <= 5', name='reports_version_check'),
            sa.CheckConstraint('generation_time_ms >= 0 AND generation_time_ms <= 120000', name='reports_gen_time_check'),
        )
        op.create_index('idx_reports_case', 'reports', ['case_id'], unique=False)
        op.create_index('idx_reports_type_version', 'reports', ['case_id', 'report_type', sa.text('version DESC')], unique=False)


def downgrade() -> None:
    """Drop reports table."""
    op.drop_index('idx_reports_type_version', table_name='reports')
    op.drop_index('idx_reports_case', table_name='reports')
    op.drop_table('reports')
