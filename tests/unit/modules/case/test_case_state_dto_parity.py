"""The three case-state enums must agree.

`CaseStateDTO` is the cross-module contract surface, `domain.models.CaseState`
is the authority, and the persistence enum is the storable set. A value present
in one but not the others is unusable: it either advertises a lifecycle the
product does not implement, or stops a real state crossing a boundary.
"""

from faultmaven.infrastructure.persistence.models import CaseState as PersistedCaseState
from faultmaven.modules.case.contracts import CaseStateDTO
from faultmaven.modules.case.domain.models import CaseState as DomainCaseState


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


class TestCaseStateEnumParity:
    def test_dto_matches_domain_exactly(self):
        """Equality, not subset — drift in either direction is a defect."""
        assert _values(CaseStateDTO) == _values(DomainCaseState)

    def test_dto_matches_persistence_exactly(self):
        """A DTO state that cannot be stored is unusable."""
        assert _values(CaseStateDTO) == _values(PersistedCaseState)

    def test_lifecycle_is_the_four_documented_states(self):
        """Pins WHAT the enums agree on, not just that they agree.

        Parity alone passes if a state is added to all three. Pinning the set
        makes adding one a deliberate act that updates this test, both enums,
        and a migration together.
        """
        assert _values(DomainCaseState) == {
            "inquiry",
            "investigating",
            "resolved",
            "closed",
        }

    def test_every_domain_state_round_trips_through_the_dto(self):
        for member in DomainCaseState:
            assert CaseStateDTO(member.value).value == member.value
