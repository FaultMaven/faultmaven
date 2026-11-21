"""The narrators read the offer facts through one typed accessor (#2811
S2b-2d, ruling (a) of 2026-11-20: "the offer facts are not outcome facts.
``case.offer_record`` stays their owner, read through one typed accessor
exempt from pin 2").

The prompt's focus selection takes the problem status and whether a resolve
decline stands from the record, and the open offer and the standing declines'
turns from ``OfferView``; the turn's narration reads the open offer and the
recorded answers from it. On every fixture of the corpus
(``offer_cases_2811``) both render byte for byte what ``main`` rendered
(``offer_oracle_2811.json``, captured at 5e5b21e42). Declared differences:
none.
"""

from __future__ import annotations

import dataclasses
import json
import re
import typing
from enum import Enum
from pathlib import Path
from typing import Optional

import pytest

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.milestone_engine import turn_narration
from faultmaven.core.investigation.narration.offer_view import OfferView
from faultmaven.core.investigation.narration.prompt_state import (
    ResolveDeclined,
    focus_selection,
    resolve_declined,
)
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.modules.case.contracts import AnswerKind, ProblemStatus
from tests.case_builders import investigating_case
from tests.unit.core.investigation import offer_cases_2811 as oc
from tests.unit.core.investigation.prompts import prompt_state_cases_2811 as ps
from tests.unit.core.investigation.prompts.test_prompt_state_record_2811 import (
    _poisoned_fold,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(Path(__file__).with_name("offer_oracle_2811.json").read_text())


def _json(value):
    return json.loads(json.dumps(value))


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(oc.NAMES)


@pytest.mark.parametrize("name", oc.NAMES)
def test_each_narrator_renders_as_main_did(name):
    assert _json(oc.render(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The accessor: the ruled field set, frozen, no text
# ---------------------------------------------------------------------------


def test_the_offer_view_holds_the_rulings_reading():
    """Ruling (a) of 2026-11-20: the offer record's facts, read through one
    typed accessor that pin 2 exempts permanently."""
    assert typing.get_type_hints(OfferView) == {
        "open_proposal_id": Optional[str],
        "answers": tuple[tuple[str, AnswerKind], ...],
        "false_alarm_decline_turn": Optional[int],
        "resolve_decline_turn": Optional[int],
    }
    view = OfferView(None, (), None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.open_proposal_id = "0" * 16  # type: ignore[misc]


_PROPOSAL_ID = re.compile(r"[0-9a-f]{16}")


def _no_text(value) -> None:
    """Every string the view holds is a proposal id; an enum member is closed
    vocabulary."""
    if dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            _no_text(getattr(value, f.name))
    elif isinstance(value, tuple):
        for nested in value:
            _no_text(nested)
    elif isinstance(value, str) and not isinstance(value, Enum):
        assert _PROPOSAL_ID.fullmatch(value), value


def test_a_populated_view_holds_no_text():
    views = [
        offer_view(oc._prompt_case("O:declined_this_turn_offer_open")),
        offer_view(oc._prompt_case("P:false_alarm_declined")),
    ]
    for f in dataclasses.fields(OfferView):
        assert any(getattr(v, f.name) not in (None, ()) for v in views), f.name
    for view in views:
        _no_text(view)


# ---------------------------------------------------------------------------
# Pin 2: with the offer facts and the record read, the selection reads no
# case field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [n for n in oc.PROMPT if n != "D:omitted"])
def test_the_selection_reads_no_case_field(name):
    case = oc._prompt_case(name)
    want = ORACLE[f"prompt:{name}"]
    with ps.frozen():
        fold, poisoned = _poisoned_fold(case)
        for mode, (steered, zoned) in oc.MODES.items():
            got = focus_selection(
                fold.outcome("pin 2"),
                fold.fresh(),
                poisoned.current_turn,
                steered=steered,
                zoned=zoned,
            )
            assert _json(oc._plain(got)) == want[mode], mode


# ---------------------------------------------------------------------------
# The record and the view win
# ---------------------------------------------------------------------------


def _live_disagrees(monkeypatch, case, how: str) -> None:
    if how == "problem_on_hold":
        case.progress.problem_status = ProblemStatus.INVALIDATED
    else:  # the closure verdict no longer SUGGEST_RESOLVE
        monkeypatch.setattr(terminal_transitions, "closure_verdict", lambda c: None)


@pytest.mark.parametrize("how", ["problem_on_hold", "closure_verdict"])
def test_the_prompt_takes_the_decline_and_the_hold_from_the_record(monkeypatch, how):
    """The record says the decline stands and the problem is not on hold; the
    live case says otherwise. The selection follows the record, and the
    decline's turn and the open offer the offer record."""
    case = oc._prompt_case("O:declined_this_turn")
    fold = NarrationFold(case)
    with ps.frozen():
        outcome = fold.outcome("record wins")
    assert outcome.resolution.declined_stands, "premise"
    assert outcome.problem.status == ProblemStatus.VERIFIED, "premise"
    _live_disagrees(monkeypatch, case, how)
    assert resolve_declined(outcome, fold.fresh(), case.current_turn) == (
        ResolveDeclined(actions=True, held=False, this_turn=True)
    )


def test_the_turn_narration_reads_the_offer_view(monkeypatch):
    """A view that disagrees with ``case.offer_record`` (nothing open, no
    answer): ``_offer_stands``, ``_lapsed`` and ``_engine_offer_stands``
    follow the view."""
    stands, lapsed = "a" * 16, "b" * 16
    view = OfferView(stands, ((lapsed, AnswerKind.LAPSED),), None, None)
    monkeypatch.setattr(turn_narration, "offer_view", lambda _c: view, raising=False)
    case = investigating_case()
    assert case.offer_record.open is None and not case.offer_record.answers
    assert turn_narration._offer_stands(case, stands)
    assert turn_narration._lapsed(case, lapsed)
    metadata: dict = {}
    params = {"proposal_id": lapsed}
    assert turn_narration._engine_offer_stands(case, metadata, params) == {
        **params,
        "stands": False,
    }
    assert metadata == {
        "transition_superseded_by_engine": False,
        "transition_dropped_for_lapsed_offer": True,
    }
