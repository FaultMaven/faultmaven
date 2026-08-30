"""047_conversion_job_partial_status

Admit ``'partial'`` in ``conversion_jobs_status_check``.

``ConversionStatus`` has carried ``PARTIAL = "partial"`` since the conversion
pipeline shipped, and ``ConversionService.convert_document`` selects it for the
ordinary outcome of a multi-failure-mode document: at least one draft produced
AND at least one failure mode refused. The CHECK constraint created by the
clean baseline never admitted it — ``('processing', 'completed', 'failed',
'cancelled')`` — so that outcome could not be written. The commit failed with a
bare ``IntegrityError``, i.e. a 500 that says nothing, having already written
the successful drafts' files to disk.

Measured on ``e1cf27371`` against the real schema, with an accepted control:

    direct insert status='completed': ACCEPTED
    direct insert status='partial':   REJECTED CHECK constraint failed:
                                      conversion_jobs_status_check

and reachable through ``convert_document`` with no id collision involved — two
failure modes, one of whose LLM bodies trips the length gate.

Found while closing #1258 (the intra-job ``runbook_id`` collision), which
refuses the colliding failure mode per-mode and therefore lands on exactly this
status. Fixed in the same change because without it #1258's fix would replace
one bare ``IntegrityError`` with another and the user-visible outcome — a 500
— would be unchanged.

``'cancelled'`` is left in the set although no writer produces it. Removing a
value is a tightening that can reject an existing row, and nothing is broken by
its presence; that is a separate decision from admitting a status the
application already emits.

Revision ID: f0a1b2c3d4e5
Revises: d8e9f0a1b2c3
Create Date: 2026-08-29 16:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW = "status IN ('processing', 'completed', 'partial', 'failed', 'cancelled')"
_OLD = "status IN ('processing', 'completed', 'failed', 'cancelled')"


def upgrade() -> None:
    """Add ``'partial'`` to the permitted set.

    ``batch_alter_table`` is required for SQLite (no native ALTER for a CHECK
    constraint — the table is rebuilt); on PostgreSQL it is a passthrough.
    Widening only, so no data transform is needed: every row that satisfies the
    old CHECK satisfies the new one.
    """
    with op.batch_alter_table("conversion_jobs") as batch:
        batch.drop_constraint("conversion_jobs_status_check", type_="check")
        batch.create_check_constraint("conversion_jobs_status_check", _NEW)


def downgrade() -> None:
    """Restore the narrower set.

    Rows already written as ``'partial'`` are moved to ``'completed'`` first,
    or the SQLite table rebuild would copy them into a table whose CHECK
    rejects them and the downgrade would fail on exactly the deployments that
    used the feature. ``'completed'`` is the lossy-but-truthful choice of the
    two survivors: the job did produce drafts (that is what distinguishes
    ``partial`` from ``failed``), and the per-failure-mode errors are carried in
    the response, not in this column.
    """
    import sqlalchemy as sa

    op.get_bind().execute(
        sa.text(
            "UPDATE conversion_jobs SET status = 'completed' WHERE status = 'partial'"
        )
    )
    with op.batch_alter_table("conversion_jobs") as batch:
        batch.drop_constraint("conversion_jobs_status_check", type_="check")
        batch.create_check_constraint("conversion_jobs_status_check", _OLD)
