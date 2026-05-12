"""Schema constraint parity tests.

Asserts that Python enum definitions exactly match the CHECK constraints
declared on ORM models. A mismatch means the application code can produce
a value that the database will reject (500 on write) or silently skip
(unmatched filter on read).

Run with: pytest tests/unit/infrastructure/test_schema_constraints.py
"""

import re

import pytest


def _extract_check_values(constraint_expression: str) -> set[str]:
    """Parse values from a 'col IN (...)' CHECK expression."""
    match = re.search(r"IN\s*\(([^)]+)\)", constraint_expression, re.IGNORECASE)
    assert match, f"Could not parse IN list from: {constraint_expression!r}"
    raw = match.group(1)
    return {v.strip().strip("'\"") for v in raw.split(",")}


@pytest.mark.unit
class TestConversionDraftStatusConstraint:
    """DraftStatus enum must exactly match conversion_drafts_status_check."""

    def test_draft_status_enum_matches_orm_check_constraint(self):
        from faultmaven.infrastructure.persistence.models import (
            ConversionDraftModel,
        )
        from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus

        # Locate the status CHECK constraint in the ORM model's table args
        constraint = next(
            (
                c
                for c in ConversionDraftModel.__table_args__
                if hasattr(c, "sqltext")
                and "status" in str(c.sqltext).lower()
                and "IN" in str(c.sqltext).upper()
            ),
            None,
        )
        assert (
            constraint is not None
        ), "conversion_drafts_status_check not found in ConversionDraftModel.__table_args__"

        orm_values = _extract_check_values(str(constraint.sqltext))
        enum_values = {member.value for member in DraftStatus}

        assert enum_values == orm_values, (
            f"DraftStatus enum values {enum_values} do not match "
            f"ORM CHECK constraint values {orm_values}. "
            "Update the enum, the ORM model constraint, AND the migration together."
        )

    def test_draft_status_check_constraint_name_is_stable(self):
        """Constraint name must stay stable so migrations can reference it by name."""
        from faultmaven.infrastructure.persistence.models import ConversionDraftModel

        constraint = next(
            (
                c
                for c in ConversionDraftModel.__table_args__
                if hasattr(c, "name") and c.name == "conversion_drafts_status_check"
            ),
            None,
        )
        assert constraint is not None, (
            "Expected a constraint named 'conversion_drafts_status_check' "
            "in ConversionDraftModel.__table_args__"
        )
