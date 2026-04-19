"""phase_5_enum_reconciliation

Storage redesign 2026-04 — Phase 5: ORM-layer enum reconciliation.

Per deployment-schema-strategy.md §3.3 + §12 decision #20: there is one
authoritative enum per concept and the domain enum wins. The following
ORM enum classes were deleted from
``faultmaven/infrastructure/persistence/models.py`` because they
duplicated or collided with their domain counterparts:

* ``EvidenceCategoryEnum`` — was misleadingly named; its values
  (``LOGS_AND_ERRORS``, ``STRUCTURED_CONFIG``, …) described data form,
  not investigation category. The ``evidence.category`` column already
  stores domain ``EvidenceCategory`` string values
  (``symptom_evidence | causal_evidence | mitigation_evidence |
  solution_evidence | resolution_evidence | contextual_evidence |
  rejected``). A separate ``evidence.form`` column bound to a renamed
  ``EvidenceFormEnum`` is planned for Phase 6 and is **not** in scope
  here.

* ``HypothesisStatusEnum`` — had drifted values
  (``proposed/testing/validated/invalidated/deferred``); domain
  ``HypothesisStatus`` is now authoritative
  (``captured | active | validated | refuted | inconclusive |
  retired``).

* ``SolutionStatusEnum`` — identical value set to domain
  ``SolutionStatus`` (``proposed | in_progress | implemented |
  verified | rejected``); was redundant.

Schema impact
-------------
Schema-level changes in this migration are intentionally minimal: the
baseline (``001_clean_baseline``) created ``hypotheses.status``,
``solutions.status`` and ``evidence.category`` as plain ``String``
columns **without** any embedded ``CHECK`` constraint, so there is no
old enum value list to drop or rewrite. The columns themselves keep
their existing types (``String(20)`` / ``String(20)`` / ``String(50)``)
and now bind to the domain enums in application code. No native PG
enum types are used (Tier 2 decision: enum binding via ``CHECK`` +
``String`` is dialect-agnostic).

Per the user directive there is no data migration. Existing rows that
might still hold legacy values from the now-deleted ORM vocabularies
(e.g. an old ``hypotheses.status = 'proposed'`` row) are not rewritten;
any such cleanup is handled separately.

This migration therefore exists primarily as a tombstone in the alembic
history that pins Phase 5 to a real revision so subsequent phases can
reference it. It is a no-op at the schema level.

Revision ID: 7372cc0135c3
Revises: 7b5962bf5e5d
Create Date: 2026-04-19 22:09:00.000000

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "7372cc0135c3"
down_revision: Union[str, Sequence[str], None] = "7b5962bf5e5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: enum reconciliation is an application-layer change.

    The ``hypotheses.status``, ``solutions.status`` and
    ``evidence.category`` columns were created without ``CHECK``
    constraints in the baseline, so there is nothing to drop or
    recreate at the schema level. The deleted ORM enum classes were
    only consumed by the ORM model definitions themselves.
    """
    pass


def downgrade() -> None:
    """No-op: matches upgrade()."""
    pass
