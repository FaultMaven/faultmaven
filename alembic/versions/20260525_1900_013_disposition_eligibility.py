"""cases: add disposition_eligibility column for read-time UI affordance gating

Add a denormalized JSON column ``disposition_eligibility`` to ``cases`` so the
frontend can gate the case-action dropdown (Resolve / Close) on actual case
state, and so admin/observability queries can filter on it without loading
relations per row.

The column is maintained at the single chokepoint
``CaseRepository.save()`` (pattern P3): the save method calls
``derive_disposition_eligibility(case)`` immediately before persisting,
guaranteeing the column always reflects the case's current
content. No per-mutation-site write burden.

Shape:
    {
        "resolved": "ready" | "needs_info" | "not_eligible",
        "closed":   "ready" | "needs_info" | "not_eligible"
    }

Stored as JSON text (matches the existing ``inquiry`` /
``problem_verification`` / ``progress`` / ``metadata`` blob columns).
Backfill on upgrade: compute the value for every existing case row from
its current content. Existing terminal cases get ``{not_eligible,
not_eligible}``; INQUIRY rows get ``{not_eligible, ready}``;
INVESTIGATING rows are derived from their readiness verdicts.

Revision ID: b2c3d4e5f607
Revises: a1b2c3d4e5f6
Create Date: 2026-05-25 19:00:00
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f607"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Eligibility constants duplicated here so the migration doesn't depend on
# importing application code (alembic env may not have the full app on path).
# Kept in sync with ``faultmaven/core/investigation/terminal_transitions.py``.
_NOT_ELIGIBLE = "not_eligible"
_READY = "ready"
_NEEDS_INFO = "needs_info"
_SUGGESTS_ALTERNATIVE = "suggests_alternative"


def _derive_for_inquiry() -> dict[str, str]:
    return {"resolved": _NOT_ELIGIBLE, "closed": _READY}


def _derive_for_terminal() -> dict[str, str]:
    return {"resolved": _NOT_ELIGIBLE, "closed": _NOT_ELIGIBLE}


def _derive_for_investigating_from_row(row) -> dict[str, str]:
    """Approximate the runtime derivation for backfill.

    The runtime derivation lives in
    ``terminal_transitions.derive_disposition_eligibility`` and reads
    ``case.root_cause_conclusion`` and ``case.solutions`` (relationship).
    For backfill we approximate the verdict from the row's
    ``root_cause_conclusion`` JSON blob + a count query against the
    ``solutions`` table.

    The approximation matches the runtime logic for the cases that exist
    today; once the column is in place every subsequent save() refreshes
    it via the canonical helper.
    """
    has_root_cause = False
    rc_blob = row.root_cause_conclusion
    if rc_blob:
        try:
            rc = json.loads(rc_blob) if isinstance(rc_blob, str) else rc_blob
            has_root_cause = bool(rc.get("root_cause"))
        except (json.JSONDecodeError, AttributeError):
            has_root_cause = False

    conn = op.get_bind()
    solution_count_row = conn.execute(
        sa.text("SELECT COUNT(*) FROM solutions WHERE case_id = :cid"),
        {"cid": row.case_id},
    ).fetchone()
    has_solutions = bool(solution_count_row and solution_count_row[0] > 0)

    if has_root_cause and has_solutions:
        # Resolution-ready: close-side flips to suggests_alternative
        # (SUGGEST_RESOLVE pivot — distinct UX from needs_info).
        return {"resolved": _READY, "closed": _SUGGESTS_ALTERNATIVE}

    # Otherwise the readiness branch is one of:
    #   READY (impossible without both fields above — guarded by READY check)
    #   NEEDS_INFO (some content but missing root_cause OR solution)
    #   SUGGEST_CLOSE (no content)
    # Approximate via partial-content check.
    if has_root_cause or has_solutions:
        return {"resolved": _NEEDS_INFO, "closed": _READY}

    return {"resolved": _NOT_ELIGIBLE, "closed": _READY}


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("disposition_eligibility", sa.Text(), nullable=True),
    )

    # Backfill existing rows from their current content.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT case_id, status, root_cause_conclusion FROM cases")
    ).fetchall()

    for row in rows:
        if row.status == "inquiry":
            value = _derive_for_inquiry()
        elif row.status == "investigating":
            value = _derive_for_investigating_from_row(row)
        else:
            value = _derive_for_terminal()

        conn.execute(
            sa.text(
                "UPDATE cases SET disposition_eligibility = :val "
                "WHERE case_id = :cid"
            ),
            {"val": json.dumps(value), "cid": row.case_id},
        )


def downgrade() -> None:
    op.drop_column("cases", "disposition_eligibility")
