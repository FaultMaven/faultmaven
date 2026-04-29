"""allow_zero_byte_uploads

Relax the ``uploaded_files.size_bytes`` constraint from ``> 0`` to ``>= 0`` so
0-byte files can be persisted and routed to the unanalyzable classification
path. Empty files are themselves diagnostic (e.g. a log a user has confirmed
to be empty), and the storage layer should accept them — graceful "this file
is empty" classification beats an opaque 400 at upload.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-29 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace size_positive with size_nonnegative on uploaded_files."""
    with op.batch_alter_table("uploaded_files") as batch_op:
        batch_op.drop_constraint(
            "uploaded_files_size_positive", type_="check"
        )
        batch_op.create_check_constraint(
            "uploaded_files_size_nonnegative", "size_bytes >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("uploaded_files") as batch_op:
        batch_op.drop_constraint(
            "uploaded_files_size_nonnegative", type_="check"
        )
        batch_op.create_check_constraint(
            "uploaded_files_size_positive", "size_bytes > 0"
        )
