"""010_evidence_model_strict

Strict evidence-model redesign per the design convergence captured in
``/home/swhouse/.claude/plans/polymorphic-booping-micali.md``.

Two-table separation:
- ``uploaded_files`` is the file-of-record for submitted data. Preprocessing
  artifacts that were previously dumped onto auto-DOCUMENT Evidence rows
  (summary, structural index, data_type, file-level coverage timestamps)
  now live where they semantically belong.
- ``evidence`` is the claim-anchored extract-of-record. Every row is the
  LLM's deliberate decision to record a specific slice as evidence for a
  specific claim. The ``form`` column (DOCUMENT vs SUBMITTED_DATA) goes
  away — there's only one creation path now.

Schema changes:

uploaded_files (additions):
  + summary           TEXT          NULL  (was on auto-DOCUMENT evidence.summary)
  + structural_index  TEXT          NULL  (was on auto-DOCUMENT evidence.extract)
  + data_type         VARCHAR(50)   NULL  (file-level data classification)
  + coverage_start_ts DATETIME      NULL  (file-level earliest timestamp)
  + coverage_end_ts   DATETIME      NULL  (file-level latest timestamp)

evidence (changes):
  - DROP form                            (dual-path model collapsed to one)
  + CHECK (source_file_id IS NOT NULL OR source_type = 'user_description')

  The existing FK (source_file_id → uploaded_files.file_id, ON DELETE
  SET NULL) is kept as-is. The new CHECK constraint naturally guards the
  invariant: if a parent file is deleted, SET NULL fires on child rows;
  for rows where source_type != 'user_description', the resulting
  source_file_id=NULL state would violate the CHECK and the parent
  DELETE is rejected. For source_type='user_description' rows (which
  legitimately have NULL source_file_id), the SET NULL is a no-op and
  the file delete succeeds.

Pre-production data handling:
  All existing evidence rows are deleted (and the hypothesis_evidence
  rows that reference them). Their ``extract`` field carried
  structural-index dumps (old semantics) which would not satisfy the
  new "claim-anchored verbatim quote" semantics. No backfill — fresh
  start.

Revision ID: 0b5e8c4f7d29
Revises: 4b7e2f9d3a18
Create Date: 2026-05-11 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0b5e8c4f7d29"
down_revision: Union[str, Sequence[str], None] = "4b7e2f9d3a18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # uploaded_files: add the five columns that hold preprocessing
    # artifacts the auto-DOCUMENT evidence path used to carry.
    # ------------------------------------------------------------------
    with op.batch_alter_table("uploaded_files") as batch:
        batch.add_column(sa.Column("summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("structural_index", sa.Text(), nullable=True))
        batch.add_column(sa.Column("data_type", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("coverage_start_ts", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("coverage_end_ts", sa.DateTime(), nullable=True))

    # ------------------------------------------------------------------
    # evidence: drop stale rows, drop form column, add CHECK.
    # ------------------------------------------------------------------
    # Drop dependent join rows first to avoid FK-cascade complications.
    op.execute("DELETE FROM hypothesis_evidence")
    op.execute("DELETE FROM evidence")

    with op.batch_alter_table("evidence") as batch:
        batch.drop_column("form")
        batch.create_check_constraint(
            "evidence_source_invariant",
            "source_file_id IS NOT NULL OR source_type = 'user_description'",
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence") as batch:
        batch.drop_constraint("evidence_source_invariant", type_="check")
        # Restore form column. Server default keeps inserts valid until a
        # writer sets it explicitly.
        batch.add_column(
            sa.Column(
                "form",
                sa.String(length=20),
                nullable=False,
                server_default="submitted_data",
            )
        )

    with op.batch_alter_table("uploaded_files") as batch:
        batch.drop_column("coverage_end_ts")
        batch.drop_column("coverage_start_ts")
        batch.drop_column("data_type")
        batch.drop_column("structural_index")
        batch.drop_column("summary")
