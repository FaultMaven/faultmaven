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

The two richer reasons are deliberately NOT inferred, so this is a SUBSET of the
engine's precedence rather than a reproduction of it. `solution_deferred` needs
an established cause plus a solution record, spread across blobs whose shape has
changed over the life of these rows. `closed_rca_infeasible` would be as cheap
to read as the mitigation flag, but a row carrying both it and a verified
mitigation would land on `mitigation_sufficient` here while the engine ranks
rca-infeasible higher — so inferring one and not the other would look like the
engine's ordering while not being it. Both omissions understate rather than
fabricate, which is the direction to err.
"""

import json
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

    # A verified mitigation is recorded on the progress blob. Read it in Python
    # rather than with a JSON operator, because the accessor syntax differs
    # between backends — but the value's TYPE differs too, and that is the trap:
    # `progress` is `JsonBlob = Text().with_variant(JSONB, "postgresql")`, so
    # psycopg2 hands back a dict on PostgreSQL while SQLite hands back the raw
    # JSON string. A textual SELECT bypasses SQLAlchemy's result coercion, so an
    # `isinstance(progress, dict)` test alone is False for EVERY SQLite row —
    # silently mapping every verified-mitigation case to
    # `closed_insufficient_evidence` on the default standalone backend, and
    # persisting a false statement about the user's case.
    rows = bind.execute(
        sa.text("SELECT case_id, progress FROM cases WHERE closure_reason = :retired"),
        {"retired": _RETIRED},
    ).fetchall()

    for case_id, progress in rows:
        if isinstance(progress, (str, bytes)):
            try:
                progress = json.loads(progress)
            except (ValueError, TypeError):
                # Unparseable blob: fall through to the bare mapping rather than
                # failing the migration. Understating is recoverable; a stuck
                # upgrade on a user's database is not.
                progress = None
        mitigation = progress.get("mitigation") if isinstance(progress, dict) else None
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
