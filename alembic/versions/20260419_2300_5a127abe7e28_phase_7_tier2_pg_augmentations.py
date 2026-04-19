"""phase_7_tier2_pg_augmentations

Storage redesign 2026-04 — Phase 7: PostgreSQL-only augmentations.

This migration adds Tier 2 (cloud-only) enhancements that strengthen
integrity, performance, and queryability for the PostgreSQL deployment.
All work is **dialect-guarded** — on SQLite (local deployment) the
migration is a no-op. Per
``deployment-schema-strategy.md`` §1.1 (Tier policy), §1.3 (dialect-guard
pattern), §3.3 (enum value lists), §7.1 (evidence Tier 2 deferred items).

Augmentations added on PostgreSQL only:

CHECK constraints (strict enum-value enforcement):

* ``cases.status`` ∈ {``inquiry``, ``investigating``, ``resolved``,
  ``closed``}
* ``hypotheses.status`` ∈ {``captured``, ``active``, ``validated``,
  ``refuted``, ``inconclusive``, ``retired``} — domain
  ``HypothesisStatus`` values per Phase 5.
* ``solutions.status`` ∈ {``proposed``, ``in_progress``,
  ``implemented``, ``verified``, ``rejected``} — domain
  ``SolutionStatus`` values per Phase 5.
* ``evidence.form`` ∈ {``text``, ``image``, ``metric``, ``structured``}
  — ``EvidenceFormEnum`` values added in Phase 6.
* ``evidence.category`` ∈ {``symptom_evidence``, ``causal_evidence``,
  ``mitigation_evidence``, ``solution_evidence``,
  ``resolution_evidence``, ``contextual_evidence``, ``rejected``} —
  domain ``EvidenceCategory`` values.
* ``evidence.reliability_score`` IS NULL OR (BETWEEN 0 AND 1) —
  Tier 2 enhancement deferred from Phase 6.

JSONB column type conversions (TEXT/JSON → JSONB):

* ``cases``: ``metadata``, ``inquiry``, ``problem_verification``,
  ``working_conclusion``, ``root_cause_conclusion``, ``path_selection``,
  ``documentation``, ``progress``, ``escalation_state``.
* ``evidence``: ``metadata``.
* ``hypotheses``: ``metadata``.
* ``solutions``: ``metadata``.

Native JSONB enables operator support (``||``, ``@>``, ``->``, ``->>``,
``jsonb_path_query``, etc.) that the field-merge methods rely on per
strategy doc §6.

evidence.tags: ``TEXT`` (CSV) → ``TEXT[]`` (PostgreSQL array) with GIN
index for efficient tag-based search. Deferred Phase 6 → here.

GIN indexes on JSONB:

* ``idx_cases_progress_gin`` on ``cases.progress`` — for stuck-case
  detection queries.
* ``idx_cases_working_conclusion_gin`` on ``cases.working_conclusion``
  — for legacy ``closure_reason`` lookups in historical data (the
  first-class column added Phase 6 covers new rows).
* ``idx_evidence_tags_gin`` on ``evidence.tags`` — tag search.

Partial indexes (narrow indexes for common queries):

* ``idx_cases_active_org_status`` on ``(organization_id, status)``
  WHERE ``status IN ('inquiry', 'investigating')`` —
  active-cases-per-org dashboard queries.
* ``idx_cases_active_last_activity`` on ``last_activity_at`` WHERE
  ``status IN ('inquiry', 'investigating')`` — staleness queries on
  active cases only.
* ``idx_evidence_primary_only`` on ``(case_id, is_primary)`` WHERE
  ``is_primary = TRUE`` — primary-evidence-per-case lookups (more
  efficient than the full Phase 6 composite for the common query).

Expression indexes:

* ``idx_cases_title_lower`` on ``LOWER(title)`` — case-insensitive
  title search. (Full-text ``tsvector`` GIN deferred — out of scope
  for Phase 7.)

PG-specific column types:

* ``user_audit_log.ip_address``: ``VARCHAR(45)`` → ``INET`` (PG only;
  stays VARCHAR(45) on SQLite). Per §1.1 PG-only column types.

Revision ID: 5a127abe7e28
Revises: f820958e06f2
Create Date: 2026-04-19 23:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "5a127abe7e28"
down_revision: Union[str, Sequence[str], None] = "f820958e06f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Domain enum value lists (per Phase 5 reconciliation; strategy doc §3.3).
# Source of truth: faultmaven/modules/case/domain/models.py.
# Wrong values would cause CHECK violations on existing data — these strings
# must match the domain enum `.value` exactly.
# ---------------------------------------------------------------------------

CASE_STATUS_VALUES = ("inquiry", "investigating", "resolved", "closed")
HYPOTHESIS_STATUS_VALUES = (
    "captured",
    "active",
    "validated",
    "refuted",
    "inconclusive",
    "retired",
)
SOLUTION_STATUS_VALUES = (
    "proposed",
    "in_progress",
    "implemented",
    "verified",
    "rejected",
)
EVIDENCE_FORM_VALUES = ("text", "image", "metric", "structured")
EVIDENCE_CATEGORY_VALUES = (
    "symptom_evidence",
    "causal_evidence",
    "mitigation_evidence",
    "solution_evidence",
    "resolution_evidence",
    "contextual_evidence",
    "rejected",
)


# Tables and columns whose JSON/TEXT-as-JSON storage is converted to JSONB.
# (table_name, column_name) — column names use the SQL identifier, not the
# Python attribute (so "metadata", not "case_metadata").
JSONB_CONVERSIONS = [
    # cases — flexible JSON state blobs (per §7 cases shape)
    ("cases", "metadata"),
    ("cases", "inquiry"),
    ("cases", "problem_verification"),
    ("cases", "working_conclusion"),
    ("cases", "root_cause_conclusion"),
    ("cases", "path_selection"),
    ("cases", "documentation"),
    ("cases", "progress"),
    ("cases", "escalation_state"),
    # case-domain children
    ("evidence", "metadata"),
    ("hypotheses", "metadata"),
    ("solutions", "metadata"),
]


def _quote_in_list(values: tuple[str, ...]) -> str:
    """Render an enum value tuple as a SQL IN-list literal."""
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    """Apply Tier 2 PG-only augmentations. SQLite is a documented no-op."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect != "postgresql":
        # All Phase 7 augmentations are PG-only. SQLite no-op (per §1.1).
        return

    # ------------------------------------------------------------------
    # 1. CHECK constraints — strict enum-value enforcement.
    # ------------------------------------------------------------------
    op.create_check_constraint(
        "cases_status_check",
        "cases",
        f"status IN ({_quote_in_list(CASE_STATUS_VALUES)})",
    )
    op.create_check_constraint(
        "hypotheses_status_check",
        "hypotheses",
        f"status IN ({_quote_in_list(HYPOTHESIS_STATUS_VALUES)})",
    )
    op.create_check_constraint(
        "solutions_status_check",
        "solutions",
        f"status IN ({_quote_in_list(SOLUTION_STATUS_VALUES)})",
    )
    op.create_check_constraint(
        "evidence_form_check",
        "evidence",
        f"form IN ({_quote_in_list(EVIDENCE_FORM_VALUES)})",
    )
    op.create_check_constraint(
        "evidence_category_check",
        "evidence",
        f"category IN ({_quote_in_list(EVIDENCE_CATEGORY_VALUES)})",
    )
    op.create_check_constraint(
        "evidence_reliability_score_range",
        "evidence",
        "reliability_score IS NULL OR (reliability_score BETWEEN 0 AND 1)",
    )

    # ------------------------------------------------------------------
    # 2. JSONB column type conversions.
    # ------------------------------------------------------------------
    # USING <col>::jsonb relies on the existing TEXT/JSON content being
    # valid JSON. The columns in scope all default to '{}' or carry
    # JSON-serialized blobs written through the application.
    for table, column in JSONB_CONVERSIONS:
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE JSONB USING "{column}"::jsonb'
        )

    # ------------------------------------------------------------------
    # 3. evidence.tags: TEXT (CSV) → TEXT[] + GIN index.
    # ------------------------------------------------------------------
    # Existing TEXT values (if any) are split on ',' into the PG array.
    # NULL values stay NULL (string_to_array(NULL, ',') is NULL).
    op.execute(
        'ALTER TABLE "evidence" '
        'ALTER COLUMN "tags" TYPE TEXT[] USING string_to_array(tags, \',\')'
    )

    # ------------------------------------------------------------------
    # 4. GIN indexes on JSONB / array columns.
    # ------------------------------------------------------------------
    op.execute(
        'CREATE INDEX "idx_cases_progress_gin" ON "cases" USING gin(progress)'
    )
    op.execute(
        'CREATE INDEX "idx_cases_working_conclusion_gin" '
        'ON "cases" USING gin(working_conclusion)'
    )
    op.execute('CREATE INDEX "idx_evidence_tags_gin" ON "evidence" USING gin(tags)')

    # ------------------------------------------------------------------
    # 5. Partial indexes — narrow indexes for common queries.
    # ------------------------------------------------------------------
    op.execute(
        'CREATE INDEX "idx_cases_active_org_status" '
        'ON "cases" (organization_id, status) '
        "WHERE status IN ('inquiry', 'investigating')"
    )
    op.execute(
        'CREATE INDEX "idx_cases_active_last_activity" '
        'ON "cases" (last_activity_at) '
        "WHERE status IN ('inquiry', 'investigating')"
    )
    op.execute(
        'CREATE INDEX "idx_evidence_primary_only" '
        'ON "evidence" (case_id, is_primary) '
        "WHERE is_primary = TRUE"
    )

    # ------------------------------------------------------------------
    # 6. Expression indexes.
    # ------------------------------------------------------------------
    op.execute('CREATE INDEX "idx_cases_title_lower" ON "cases" (LOWER(title))')

    # ------------------------------------------------------------------
    # 7. user_audit_log.ip_address: VARCHAR(45) → INET.
    # ------------------------------------------------------------------
    # USING ip_address::inet validates each existing string. Any malformed
    # value would fail; per project directive there is no data preservation,
    # so the assumption is that pre-existing rows hold either a valid IP
    # literal or NULL.
    op.execute(
        'ALTER TABLE "user_audit_log" '
        'ALTER COLUMN "ip_address" TYPE INET USING ip_address::inet'
    )


def downgrade() -> None:
    """Reverse Phase 7 PG augmentations in inverse order. SQLite no-op."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect != "postgresql":
        return

    # 7. user_audit_log.ip_address: INET → VARCHAR(45).
    op.execute(
        'ALTER TABLE "user_audit_log" '
        'ALTER COLUMN "ip_address" TYPE VARCHAR(45) USING ip_address::text'
    )

    # 6. Expression index.
    op.execute('DROP INDEX IF EXISTS "idx_cases_title_lower"')

    # 5. Partial indexes.
    op.execute('DROP INDEX IF EXISTS "idx_evidence_primary_only"')
    op.execute('DROP INDEX IF EXISTS "idx_cases_active_last_activity"')
    op.execute('DROP INDEX IF EXISTS "idx_cases_active_org_status"')

    # 4. GIN indexes.
    op.execute('DROP INDEX IF EXISTS "idx_evidence_tags_gin"')
    op.execute('DROP INDEX IF EXISTS "idx_cases_working_conclusion_gin"')
    op.execute('DROP INDEX IF EXISTS "idx_cases_progress_gin"')

    # 3. evidence.tags: TEXT[] → TEXT (joined on ',').
    op.execute(
        'ALTER TABLE "evidence" '
        'ALTER COLUMN "tags" TYPE TEXT USING array_to_string(tags, \',\')'
    )

    # 2. JSONB → TEXT (json_typeof preserved by ::text serialization).
    for table, column in reversed(JSONB_CONVERSIONS):
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TEXT USING "{column}"::text'
        )

    # 1. CHECK constraints.
    op.drop_constraint("evidence_reliability_score_range", "evidence", type_="check")
    op.drop_constraint("evidence_category_check", "evidence", type_="check")
    op.drop_constraint("evidence_form_check", "evidence", type_="check")
    op.drop_constraint("solutions_status_check", "solutions", type_="check")
    op.drop_constraint("hypotheses_status_check", "hypotheses", type_="check")
    op.drop_constraint("cases_status_check", "cases", type_="check")
