"""046_conversion_draft_runbook_id_uniqueness

At most one LIVE ``conversion_drafts`` row per ``(organization_id, runbook_id)``.

``conversion_drafts.runbook_id`` had no uniqueness of any kind, so two drafts
could share it — and did: ``runbook_id_from_parts`` returned ``""`` for a
punctuation-only or non-latin ``(service, title)`` pair, so two such runbooks
persisted two rows with ``runbook_id = ''`` and one identical derived
``item_id_from_runbook_id('')`` (#1230). Verify/approve, dedup, and every
id-keyed lookup then cannot tell the two apart. The empty slug was only the
reproducing case; ANY two titles that slug identically collided.

The mint no longer returns an empty or non-kebab id (#1230/#1243), which
removes that reproducing case. It cannot remove the general one: the mint is a
pure function of ``(service, title)`` and must stay deterministic — the disk
scan reconciles a file to its row by this id — so it cannot see the other rows.
Uniqueness therefore belongs where the other rows are visible.

Scope qualification, and why:

- ``organization_id`` — ``conversion_drafts`` is RLS-tenanted on this column
  (migration 018). Two tenants authoring a runbook with the same title is
  ordinary, not a collision. It is also a confidentiality requirement, not only
  a correctness one: a unique index is enforced BELOW row-level security, so a
  key omitting the tenant would reject an insert because of a row the caller
  cannot see — a cross-tenant existence oracle over runbook titles.
- NOT further qualified by KB scope (personal/team/global). ``scope`` lives on
  ``conversion_jobs``, not on the draft row, so a plain index cannot reach it —
  and it should not: ``item_id_from_runbook_id`` is scope-blind, so two drafts
  sharing a ``runbook_id`` in one tenant collide on the derived item id
  whatever scope they were minted under.
- Partial on ``status <> 'discarded'``. Discard is a soft delete (the row
  stays), so including discarded rows would make a discarded draft permanently
  block re-converting the same source. Both SQLite (>= 3.8.0) and PostgreSQL
  support partial indexes; the two dialect ``where`` clauses are identical.

Pre-existing collisions: REFUSED, not resolved.

The dev database was audited clean (2 drafts, 2 distinct ids, 0 collisions),
but a tenant database was not, and every way to resolve a duplicate destroys or
rewrites operator data — discarding a draft nobody discarded, or re-minting a
persisted identifier so it no longer matches its own file's frontmatter. 034
could seed newest-wins because it was filling a NEW nullable column; here there
is no non-destructive default. So this migration inspects first and aborts with
the offending keys named, the inspection query, and a resolution, rather than
choosing on the operator's behalf. Re-running after the operator resolves them
completes normally.

Revision ID: d8e9f0a1b2c3
Revises: e9f0a1b2c3d4  (045, fm#1227's suggestion store)
Create Date: 2026-08-29 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_conversion_drafts_org_runbook_id"
LIVE_PREDICATE = "status <> 'discarded'"

#: Plain GROUP BY / HAVING — no ``GROUP_CONCAT`` or ``string_agg``, so the same
#: statement runs on SQLite and PostgreSQL.
_FIND_COLLISIONS = f"""
SELECT organization_id, runbook_id, COUNT(*) AS n
FROM conversion_drafts
WHERE {LIVE_PREDICATE}
GROUP BY organization_id, runbook_id
HAVING COUNT(*) > 1
ORDER BY n DESC, organization_id, runbook_id
"""

#: How many offending keys the abort message spells out before summarising. A
#: message an operator has to page through is not more actionable than one that
#: names the shape and hands over the query.
_MAX_REPORTED = 10


def _abort_message(rows: Sequence[sa.Row]) -> str:
    listed = "\n".join(
        f"    organization_id={org!r} runbook_id={rid!r} live_drafts={n}"
        for org, rid, n in rows[:_MAX_REPORTED]
    )
    more = (
        f"\n    ... and {len(rows) - _MAX_REPORTED} more"
        if len(rows) > _MAX_REPORTED
        else ""
    )
    return (
        f"Migration 046 cannot create {INDEX_NAME}: "
        f"{len(rows)} (organization_id, runbook_id) key(s) are already shared by "
        f"more than one live conversion draft.\n\n"
        f"{listed}{more}\n\n"
        "These rows predate the uniqueness rule and are indistinguishable to "
        "verify/approve, dedup, and every id-keyed lookup — which is the defect "
        "this index closes (#1230). Resolving one means either discarding the "
        "duplicate drafts or re-minting their runbook_id, and both destroy or "
        "rewrite data, so the migration refuses to choose.\n\n"
        "Inspect them with:\n\n"
        "    SELECT id, organization_id, conversion_id, runbook_id, title, "
        "status, file_path, created_at\n"
        "    FROM conversion_drafts\n"
        "    WHERE status <> 'discarded'\n"
        "      AND (organization_id, runbook_id) IN (\n"
        "        SELECT organization_id, runbook_id FROM conversion_drafts\n"
        "        WHERE status <> 'discarded'\n"
        "        GROUP BY organization_id, runbook_id HAVING COUNT(*) > 1);\n\n"
        "Then discard the duplicates you do not want to keep "
        "(UPDATE conversion_drafts SET status = 'discarded' WHERE id IN (...)) "
        "and re-run the migration."
    )


def upgrade() -> None:
    """Refuse on pre-existing collisions, then enforce uniqueness."""
    bind = op.get_bind()
    rows = bind.execute(sa.text(_FIND_COLLISIONS)).fetchall()
    if rows:
        raise RuntimeError(_abort_message(rows))

    op.create_index(
        INDEX_NAME,
        "conversion_drafts",
        ["organization_id", "runbook_id"],
        unique=True,
        sqlite_where=sa.text(LIVE_PREDICATE),
        postgresql_where=sa.text(LIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="conversion_drafts")
