"""``disposition_eligibility`` is published as two enums (#2379, contract 14.10.0).

It was a free-form ``{str: str}`` in the contract, so a value the server added
reached the clients' menu logic with no type error. Each key now has its own
value set — ``resolved`` a ``ResolveEligibility``, ``closed`` a
``CloseEligibility`` — published with each value's meaning, inside a
``DispositionEligibility`` object that takes those two keys and no other.
"""

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError

from faultmaven.modules.case.api.routes.router import router as case_router
from faultmaven.modules.case.contracts import (
    CloseEligibility,
    DispositionEligibility,
    ResolveEligibility,
)
from faultmaven.modules.case.domain.models.lifecycle import (
    _CLOSE_ELIGIBILITY_MEANINGS,
    _RESOLVE_ELIGIBILITY_MEANINGS,
    member_table,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def schemas() -> dict:
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    return app.openapi()["components"]["schemas"]


@pytest.mark.parametrize(
    "enum, meanings",
    [
        (ResolveEligibility, _RESOLVE_ELIGIBILITY_MEANINGS),
        (CloseEligibility, _CLOSE_ELIGIBILITY_MEANINGS),
    ],
)
def test_each_enum_lists_its_values_with_one_meaning_each(schemas, enum, meanings):
    component = schemas[enum.__name__]
    assert component["enum"] == [member.value for member in enum]
    assert component["x-enum-descriptions"] == [meanings[m] for m in enum]
    assert all(m.strip() for m in component["x-enum-descriptions"])


def test_the_object_takes_both_keys_and_no_other(schemas):
    component = schemas["DispositionEligibility"]
    assert component["required"] == ["resolved", "closed"]
    assert component["additionalProperties"] is False
    assert component["properties"] == {
        "resolved": {"$ref": "#/components/schemas/ResolveEligibility"},
        "closed": {"$ref": "#/components/schemas/CloseEligibility"},
    }


@pytest.mark.parametrize(
    "model",
    [
        "CaseUIResponse_Inquiry",
        "CaseUIResponse_Investigating",
        "CaseUIResponse_Resolved",
    ],
)
def test_each_case_read_references_it(schemas, model):
    field = schemas[model]["properties"]["disposition_eligibility"]
    assert {"$ref": "#/components/schemas/DispositionEligibility"} in field["anyOf"]


class TestTheShape:
    adapter = TypeAdapter(DispositionEligibility)

    def test_a_third_key_is_refused(self):
        with pytest.raises(ValidationError, match="extra"):
            self.adapter.validate_python(
                {"resolved": "ready", "closed": "ready", "reopen": "ready"}
            )

    def test_a_value_from_the_other_key_is_refused(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python(
                {"resolved": "suggests_alternative", "closed": "ready"}
            )

    def test_a_missing_key_is_refused(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python({"resolved": "ready"})


class TestTheMeaningsTable:
    def test_a_missing_member_is_named(self):
        with pytest.raises(ValueError, match="missing \\['needs_info'"):
            member_table(
                ResolveEligibility,
                {
                    ResolveEligibility.READY: "r",
                    ResolveEligibility.NOT_ELIGIBLE: "n",
                },
                noun="a resolve-eligibility",
            )

    def test_a_key_that_is_not_a_member_is_named(self):
        with pytest.raises(ValueError, match="not a member .*needs_info"):
            member_table(
                CloseEligibility,
                {
                    **{member: "m" for member in CloseEligibility},
                    "needs_info": "n",
                },
                noun="a close-eligibility",
            )
