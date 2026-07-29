"""CaseStateDTO must mirror the domain and persistence case-state enums.

The DTO is the cross-module contract surface. When it advertises states the
domain does not have, callers write code against values that cannot exist:
``CaseState.SOLVED`` / ``CaseState.DOCUMENTING`` were referenced in the
case-closure route and raised AttributeError -> HTTP 500 on every call before
being fixed. The caller was corrected but the phantom DTO values stayed, so the
same trap remained for the next reader. These tests pin the invariant instead of
relying on the comment.
"""

import pytest

from faultmaven.infrastructure.persistence.models import CaseState as PersistedCaseState
from faultmaven.modules.case.contracts import CaseStateDTO
from faultmaven.modules.case.domain.models import CaseState as DomainCaseState


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


class TestCaseStateEnumParity:
    def test_dto_matches_domain_exactly(self):
        """Equality, not subset — drift in EITHER direction is a defect.

        Extra DTO values advertise a lifecycle the product does not implement;
        missing ones mean a real state cannot cross a module boundary.
        """
        assert _values(CaseStateDTO) == _values(DomainCaseState)

    def test_dto_matches_persistence_exactly(self):
        """A DTO state that cannot be stored is unusable."""
        assert _values(CaseStateDTO) == _values(PersistedCaseState)

    def test_lifecycle_is_the_four_documented_states(self):
        """Anchors the invariant to the documented lifecycle.

        CLAUDE.md and the CaseState docstring both specify
        INQUIRY -> INVESTIGATING -> RESOLVED/CLOSED. Pinning the literal set
        means adding a state is a deliberate act that updates this test, the
        domain enum, the persistence enum and a migration together.
        """
        assert _values(CaseStateDTO) == {
            "inquiry",
            "investigating",
            "resolved",
            "closed",
        }

    @pytest.mark.parametrize(
        "phantom",
        ["documenting", "resolved_with_workaround", "resolved_by_user", "abandoned"],
    )
    def test_previously_advertised_phantom_states_are_gone(self, phantom):
        """Named individually so a regression says WHICH value came back."""
        assert phantom not in _values(CaseStateDTO)
        with pytest.raises(ValueError):
            CaseStateDTO(phantom)

    def test_every_domain_state_round_trips_through_the_dto(self):
        """The DTO must be constructible from any real domain state."""
        for member in DomainCaseState:
            assert CaseStateDTO(member.value).value == member.value
