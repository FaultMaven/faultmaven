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
