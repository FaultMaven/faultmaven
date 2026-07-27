"""Tests for the single-owner terminal-hypothesis-state predicate (#840).

``{REFUTED, RETIRED}`` — "this hypothesis is out of the differential for
good" — was previously re-spelled independently at six call sites. The pair
is now spelled exactly once, in ``TERMINAL_HYPOTHESIS_STATES`` on the domain
model, with ``HypothesisState.is_terminal`` deriving from it; every consumer
routes through one of the two.

These tests sweep the FULL enum (the guarantee is a property of every state,
not of two instances) so a third terminal state — or a decision that one of
the two stops being terminal — fails here first, forcing the change to be
made at the single owner.
"""

import pytest

from faultmaven.modules.case.contracts import (
    TERMINAL_HYPOTHESIS_STATES,
    HypothesisState,
)

pytestmark = pytest.mark.unit

_EXPECTED_TERMINAL = {HypothesisState.REFUTED, HypothesisState.RETIRED}


def test_terminal_set_is_exactly_refuted_and_retired():
    assert TERMINAL_HYPOTHESIS_STATES == frozenset(_EXPECTED_TERMINAL)


@pytest.mark.parametrize("state", list(HypothesisState))
def test_is_terminal_agrees_with_the_set_for_every_state(state):
    """``is_terminal`` and set membership are the same predicate — sweep the
    whole input space so they can never silently diverge."""
    assert state.is_terminal == (state in _EXPECTED_TERMINAL)
    assert state.is_terminal == (state in TERMINAL_HYPOTHESIS_STATES)


def test_terminal_set_is_immutable():
    """frozenset by contract — a consumer cannot widen or narrow the terminal
    set in place."""
    assert isinstance(TERMINAL_HYPOTHESIS_STATES, frozenset)
