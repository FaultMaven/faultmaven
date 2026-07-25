"""009_evidence_solution_coherence

Closes the silent-data-loss gap on Evidence and Solution persistence
identified by the schema-redesign coherence audit (2026-05-10).

Background:
- Pydantic ``Evidence`` carries five fields with no ORM column:
  ``primary_purpose``, ``analysis``, ``processing_mode``,
  ``advances_milestones``, ``collected_by``. Every persist silently
  dropped them.
- Pydantic ``Solution`` carries seven fields whose persistence either
  has no ORM column (``verification_method``, ``verification_evidence_id``,
  ``effectiveness``) or is hardcoded ``None`` in the repository despite
  semantically-similar columns existing under different names
  (``proposed_by``, ``applied_at``, ``applied_by``, ``verified_at``).
- ``solutions.created_by`` and ``solutions.updated_by`` were FK-to-users
  columns the repo always wrote ``None`` for — dead columns. Per
  ``feedback_no_backcompat_pre_data.md``, drop them rather than
  repurpose; the sentinel-actor fields (``proposed_by`` defaults to
  ``"agent"``, ``applied_by`` allows ``None`` or ``"agent"``) need
  free-form VARCHAR per the case_actions.triggered_by precedent
  (migration 008) — FK to users would force inventing sentinel rows.

Schema changes:

evidence:
  + primary_purpose VARCHAR(100) NOT NULL DEFAULT 'legacy'
  + analysis TEXT NULL
  + processing_mode VARCHAR(50) NULL
  + advances_milestones TEXT[]/TEXT NULL  (TagsArray-shaped)
  + collected_by VARCHAR(50) NOT NULL DEFAULT 'system'

solutions:
  - DROP created_by  (FK to users; always written NULL — dead)
  - DROP updated_by  (FK to users; always written NULL — dead)
  ~ RENAME implemented_at -> applied_at      (match domain naming)
  ~ RENAME verification_timestamp -> verified_at  (match domain)
  + proposed_by VARCHAR(50) NOT NULL DEFAULT 'agent'
  + applied_by VARCHAR(50) NULL
  + verification_method VARCHAR(500) NULL
  + verification_evidence_id VARCHAR(36) NULL
      FK -> evidence(evidence_id) ON DELETE SET NULL
  + effectiveness FLOAT NULL
      CHECK (effectiveness IS NULL OR effectiveness BETWEEN 0 AND 1)

Server defaults on the two NOT NULL evidence columns are intentional:
existing rows pre-date the schema fix and have no domain-truth value to
backfill. The defaults ('legacy', 'system') satisfy NOT NULL without
losing the rows. New writers must populate explicitly; a future
migration can drop the defaults once that's verified.

Revision ID: 4b7e2f9d3a18
Revises: 317a8c329673
Create Date: 2026-05-10 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "4b7e2f9d3a18"
down_revision: Union[str, Sequence[str], None] = "317a8c329673"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # evidence: add 5 missing columns
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch:
        batch.add_column(
            sa.Column(
                "primary_purpose",
                sa.String(length=100),
                nullable=False,
                server_default="legacy",
            )
        )
        batch.add_column(sa.Column("analysis", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("processing_mode", sa.String(length=50), nullable=True)
        )
        # TagsArray-shaped: TEXT[] on PG, comma-encoded TEXT on SQLite.
        # The TypeDecorator on the ORM side (_TagsArrayType) handles the
        # cross-dialect serialization. Stored type is plain Text/ARRAY at
        # the migration layer.
        batch.add_column(sa.Column("advances_milestones", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "collected_by",
                sa.String(length=50),
                nullable=False,
                server_default="system",
            )
        )

    # ------------------------------------------------------------------
    # solutions: drop dead FK columns, rename to match domain, add
    # missing fields
    # ------------------------------------------------------------------
    # Drop the index on created_by first; SQLite batch_alter_table tries
    # to recreate indexes during table-rebuild and would fail when the
    # column is gone.
    op.drop_index("ix_solutions_created_by", table_name="solutions")

    with op.batch_alter_table("solutions") as batch:
        # Drop dead FK columns that the repo always wrote NULL for.
        batch.drop_column("created_by")
        batch.drop_column("updated_by")

        # Rename to match Pydantic domain field names (Solution.applied_at
        # / Solution.verified_at). Eliminates the read/write mismatch the
        # repo currently bridges by silently dropping the values.
        batch.alter_column("implemented_at", new_column_name="applied_at")
        batch.alter_column("verification_timestamp", new_column_name="verified_at")

        # Sentinel-friendly actor columns (per case_actions.triggered_by
        # precedent — VARCHAR not FK so 'agent' is a legal value).
        batch.add_column(
            sa.Column(
                "proposed_by",
                sa.String(length=50),
                nullable=False,
                server_default="agent",
            )
        )
        batch.add_column(sa.Column("applied_by", sa.String(length=50), nullable=True))

        # Verification metadata that the Pydantic model has been carrying
        # but the schema never stored.
        batch.add_column(
            sa.Column("verification_method", sa.String(length=500), nullable=True)
        )
        batch.add_column(
            sa.Column("verification_evidence_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(sa.Column("effectiveness", sa.Float(), nullable=True))

        batch.create_check_constraint(
            "solutions_effectiveness_range",
            "effectiveness IS NULL OR (effectiveness >= 0 AND effectiveness <= 1)",
        )

    # FK created outside the batch context: SQLite batch mode handles FK
    # creation via table-rebuild, but for cross-table FKs we add it as a
    # separate statement so PG can link directly without rebuild.
    with op.batch_alter_table("solutions") as batch:
        batch.create_foreign_key(
            "fk_solutions_verification_evidence",
            "evidence",
            ["verification_evidence_id"],
            ["evidence_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("solutions") as batch:
        batch.drop_constraint("fk_solutions_verification_evidence", type_="foreignkey")

    with op.batch_alter_table("solutions") as batch:
        batch.drop_constraint("solutions_effectiveness_range", type_="check")
        batch.drop_column("effectiveness")
        batch.drop_column("verification_evidence_id")
        batch.drop_column("verification_method")
        batch.drop_column("applied_by")
        batch.drop_column("proposed_by")
        batch.alter_column("verified_at", new_column_name="verification_timestamp")
        batch.alter_column("applied_at", new_column_name="implemented_at")
        # Restore the dropped FK columns. Inline ForeignKey objects must
        # carry an explicit name in batch mode (alembic's batch rebuild
        # raises "Constraint must have a name" otherwise), so add the
        # columns first then create the FKs explicitly with names.
        batch.add_column(sa.Column("updated_by", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("created_by", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_solutions_updated_by",
            "users",
            ["updated_by"],
            ["user_id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_solutions_created_by",
            "users",
            ["created_by"],
            ["user_id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_solutions_created_by", "solutions", ["created_by"], unique=False
    )

    with op.batch_alter_table("evidence") as batch:
        batch.drop_column("collected_by")
        batch.drop_column("advances_milestones")
        batch.drop_column("processing_mode")
        batch.drop_column("analysis")
        batch.drop_column("primary_purpose")
