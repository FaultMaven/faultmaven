"""040: migrate the retired closed_after_investigation closure_reason

`closed_after_investigation` named the state a case closed FROM rather than why
it ended, and covered four unrelated outcomes: a documented-but-deferred fix, a
structurally unreachable cause, a successful mitigation, and a plain failure to
establish anything. It has been removed from VALID_CLOSURE_REASONS.

That set is enforced by a Pydantic field validator on `Case`, which runs on
READ — so any surviving row carrying the retired value fails hydration rather
than merely looking stale, taking case reads down with it. This is not
back-compat (the code no longer accepts the value); it is a one-time forward
migration of rows written before the rename.

Rows are mapped by the SAME precedence the engine now derives, most-specific
first, so a migrated row gets the label it would get if it closed today:

  1. a verified mitigation on record   -> mitigation_sufficient
  2. otherwise                         -> closed_insufficient_evidence

The two richer reasons are deliberately NOT inferred here. `solution_deferred`
needs an established cause plus a solution record, and `closed_rca_infeasible`
needs a declared rationale; both live in JSON blobs whose shape has changed over
the life of these rows, so guessing would fabricate an outcome. Landing an
honest `closed_insufficient_evidence` understates those cases rather than
inventing a conclusion for them.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RETIRED = "closed_after_investigation"


def upgrade() -> None:
    bind = op.get_bind()

    # A verified mitigation is recorded on the progress JSON blob. Read it in
    # Python rather than with a JSON operator: the column is JSON on SQLite and
    # JSONB on PostgreSQL, and the accessor syntax differs between them.
    rows = bind.execute(
        sa.text("SELECT case_id, progress FROM cases WHERE closure_reason = :retired"),
        {"retired": _RETIRED},
    ).fetchall()

    for case_id, progress in rows:
        mitigation = (
            (progress or {}).get("mitigation") if isinstance(progress, dict) else None
        )
        verified = bool(mitigation and mitigation.get("verified"))
        bind.execute(
            sa.text("UPDATE cases SET closure_reason = :reason WHERE case_id = :cid"),
            {
                "reason": (
                    "mitigation_sufficient"
                    if verified
                    else "closed_insufficient_evidence"
                ),
                "cid": case_id,
            },
        )


def downgrade() -> None:
    """Not reversible.

    The retired value conflated four outcomes, so restoring it would have to
    pick which of the current reasons to collapse — and would re-introduce rows
    the application layer now refuses to load. Cases keep their migrated
    reasons; nothing is lost by staying on the newer vocabulary.
    """
    pass
