"""``closure_reason`` and ``valid_next_states`` are published as enums (#2389).

Both were bare strings in the contract, so a value the server added reached the
clients' label tables with no type error, and the vocabulary was a ``set[str]``
copied by hand into each server table keyed on it (#1798). ``ClosureReason`` is
now the one definition: the contract publishes it with each value's meaning,
and every server table keyed on it is checked complete when it is built.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI

from faultmaven.api.routes.admin_cases import router as admin_cases_router
from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from faultmaven.modules.case.domain.models.lifecycle import (
    _CLOSURE_REASON_MEANINGS,
    CaseState,
    ClosureReason,
    closure_reason_table,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    USER_SELECTABLE_ACTIONS,
    SelectableNextState,
    _require_declared_selectable_states,
)
from tests.case_builders import make_case

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def schemas() -> dict:
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    app.include_router(admin_cases_router, prefix="/api/v1")
    return app.openapi()["components"]["schemas"]


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


class TestTheClosureReasonComponent:
    def test_it_lists_the_values_with_one_meaning_each_in_order(self, schemas):
        component = schemas["ClosureReason"]
        assert component["enum"] == [reason.value for reason in ClosureReason]
        meanings = component["x-enum-descriptions"]
        assert meanings == [_CLOSURE_REASON_MEANINGS[r] for r in ClosureReason]
        assert all(isinstance(m, str) and m.strip() for m in meanings)

    @pytest.mark.parametrize(
        "model", ["CaseSummary", "CaseDetail", "AdminCaseMetadata"]
    )
    def test_each_published_closure_reason_references_it(self, schemas, model):
        field = schemas[model]["properties"]["closure_reason"]
        assert field["anyOf"] == [_ref("ClosureReason"), {"type": "null"}]


class TestTheSelectableNextStateComponent:
    @pytest.mark.parametrize(
        "model",
        [
            "CaseSummary",
            "CaseDetail",
            "CaseUIResponse_Inquiry",
            "CaseUIResponse_Investigating",
            "CaseUIResponse_Resolved",
        ],
    )
    def test_each_valid_next_states_lists_it(self, schemas, model):
        field = schemas[model]["properties"]["valid_next_states"]
        assert field["items"] == _ref("SelectableNextState")

    def test_its_values_are_the_states_the_menu_lists(self, schemas):
        component = schemas["SelectableNextState"]
        listed = {t.value for ts in USER_SELECTABLE_ACTIONS.values() for t in ts}
        assert set(component["enum"]) == listed
        assert len(component["x-enum-descriptions"]) == len(component["enum"])

    @pytest.mark.parametrize(
        "table, named",
        [
            pytest.param(
                {CaseState.INVESTIGATING: (CaseState.CLOSED, CaseState.RESOLVED)},
                "'resolved'",
                id="listed-not-declared",
            ),
            pytest.param({CaseState.INQUIRY: ()}, "'closed'", id="declared-not-listed"),
        ],
    )
    def test_the_import_time_check_names_a_difference(self, table, named):
        with pytest.raises(ValueError, match=named):
            _require_declared_selectable_states(table)
        assert {m.value for m in SelectableNextState} == {"closed"}, "premise"


class TestAClosureReasonTableIsComplete:
    def test_a_missing_member_is_named(self):
        partial = {
            r: r.value for r in ClosureReason if r is not ClosureReason.INQUIRY_ONLY
        }
        with pytest.raises(ValueError, match=r"missing \['inquiry_only'\]"):
            closure_reason_table(partial)

    def test_a_key_that_is_not_a_member_is_named(self):
        table = {**{r: r.value for r in ClosureReason}, "abandoned": "?"}
        with pytest.raises(ValueError, match=r"not a member \[\"'abandoned'\"\]"):
            closure_reason_table(table)

    def test_a_complete_table_is_read_only_and_keyed_by_the_members(self):
        table = closure_reason_table({r.value: r.name for r in ClosureReason})
        assert list(table) == list(ClosureReason)
        with pytest.raises(TypeError):
            table[ClosureReason.INQUIRY_ONLY] = "x"


def test_the_fallback_terminal_prompt_names_the_value_not_the_member():
    """The terminal fallback prompt interpolates the reason, so the model reads
    the value. (The persisted ``justifying_signature``, an f-string, is pinned
    as ``"closed_false_alarm|<turn>"`` in the false-alarm tests: a ``(str,
    Enum)`` member would render there as ``ClosureReason.CLOSED_FALSE_ALARM``.)"""
    now = datetime.now(timezone.utc)
    case = make_case(
        state=CaseState.CLOSED,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        closed_at=now,
        closure_reason=ClosureReason.INQUIRY_ONLY,
    )
    prompt = get_fallback_prompt_for_case(case, "what happened?")
    assert "inquiry_only" in prompt
    assert "ClosureReason." not in prompt
