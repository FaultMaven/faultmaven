"""The Case.closure_reason description is built from VALID_CLOSURE_REASONS (#741)."""

import pytest

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import VALID_CLOSURE_REASONS

_MARKER = "One of: "


@pytest.mark.unit
def test_description_names_exactly_the_enforced_vocabulary():
    description = Case.model_fields["closure_reason"].description
    assert _MARKER in description
    listed = description.split(_MARKER, 1)[1].split(" | ")
    assert set(listed) == VALID_CLOSURE_REASONS
    assert len(listed) == len(VALID_CLOSURE_REASONS)


@pytest.mark.unit
def test_description_says_resolved_carries_none():
    assert "RESOLVED" in Case.model_fields["closure_reason"].description
