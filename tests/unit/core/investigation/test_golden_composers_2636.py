"""The deciders compose their outcome text through ``narration``, and the text
is ``main``'s, byte for byte (#2636 B1a).

Each scenario of ``golden_cases_2636`` builds a real case and runs a decider;
``main``'s text for it was captured by running the same scenario on ``main``
(``golden_cases_2636.capture_main``) and checked in. A decider that passes a
composer the wrong reason or params fails here.
"""

import json

import pytest

from faultmaven.core.investigation.narration.notices import (
    RESOLVE_DECLINED_ACTIONS,
    RESOLVE_DECLINED_CLOSE_ACTION,
    resolve_declined_actions,
)
from faultmaven.core.investigation.narration.offers import readiness_message
from faultmaven.core.investigation.terminal_transitions import ResolutionReadiness
from faultmaven.modules.case.contracts import ResolutionReason
from tests.unit.core.investigation import golden_cases_2636 as golden_cases

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", sorted(golden_cases.SCENARIOS))
def test_each_decider_composes_the_text_main_composed(name):
    captured = json.loads(golden_cases.GOLDEN.read_text())[name]
    assert golden_cases.SCENARIOS[name]() == captured


def test_every_scenario_was_captured_on_main():
    captured = json.loads(golden_cases.GOLDEN.read_text())
    assert sorted(captured) == sorted(golden_cases.SCENARIOS)


def test_the_declined_actions_name_both_chips_unless_a_check_holds():
    assert resolve_declined_actions(False) == RESOLVE_DECLINED_ACTIONS
    assert resolve_declined_actions(True) == RESOLVE_DECLINED_CLOSE_ACTION


def test_an_unknown_reason_has_no_message():
    with pytest.raises(ValueError, match="no readiness message"):
        readiness_message("not_a_reason")


@pytest.mark.parametrize(
    ("reason", "param"),
    [
        (ResolutionReason.PENDING_CHECK, "request_text"),
        (ResolutionReason.FIX_OFF_CAUSE, "off_cause_turn"),
    ],
)
def test_a_reason_without_the_param_its_text_names_raises(reason, param):
    with pytest.raises(ValueError, match=param):
        readiness_message(reason)


def test_a_readiness_is_composed_where_it_is_built():
    """A param no reason takes fails at construction, and the message is
    read-only."""
    with pytest.raises(TypeError):
        ResolutionReadiness(
            ResolutionReadiness.SUGGEST_CLOSE,
            [],
            reason=ResolutionReason.FALSE_ALARM,
            off_cause=3,
        )
    readiness = ResolutionReadiness(
        ResolutionReadiness.READY, [], reason=ResolutionReason.CONFIRMED
    )
    with pytest.raises(AttributeError):
        readiness.message = "x"
