"""The narrators read the offer facts through one typed accessor (#2811
S2b-2d, ruling (a) of 2026-11-20: "the offer facts are not outcome facts.
``case.offer_record`` stays their owner, read through one typed accessor
exempt from pin 2").

The prompt's focus selection takes the problem status and whether a resolve
decline stands from the record, and the open offer and the standing declines'
turns from ``OfferView``; the turn's narration reads the open offer and the
recorded answers from it. On every fixture of the corpus
(``offer_cases_2811``) both render byte for byte the stored oracle
(``offer_oracle_2811.json``, captured on ``main`` at 5e5b21e42; an entry a
later commit added or changed is that commit's to declare). Declared
differences, #2811 S2c-1b: ``turn:offer:needs_info_no_cause`` and
``turn:offer:needs_info_held_open``, rendered at c1ade8d8c, each differing
from ``main`` at 5a1d3b130 only in its "Root cause" line (``main``: the bare
ask). #2811 S2c-3a: ``turn:offer:turn_replayed_rca_infeasible_close``,
rendered at c31e9e4ae and differing from ``main`` at 77f3b9b8b only in a
reply that now ends with the outcome statement of its revision-confirm turn
("**Case record:** the problem is confirmed.").
"""

from __future__ import annotations

import dataclasses
import json
import re
import typing
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import pytest

from faultmaven.core.investigation import offers as offers_module
from faultmaven.core.investigation.milestone_engine import (
    turn_completion,
    turn_narration,
)
from faultmaven.core.investigation.narration.offer_view import OfferView
from faultmaven.core.investigation.narration.prompt_state import (
    ON_HOLD,
    PROGRESS_OMITTED,
    ResolveDeclined,
    focus_selection,
    resolve_declined,
)
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.problem_status import problem_on_hold
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from faultmaven.modules.case.contracts import AnswerKind, ProblemStatus
from tests.case_builders import investigating_case, with_declined_offer
from tests.unit.core.investigation import offer_cases_2811 as oc
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
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
def test_each_narrator_renders_the_oracle(name):
    assert _json(oc.render(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The accessor: the ruled field set, frozen, no text
# ---------------------------------------------------------------------------


def test_the_offer_view_holds_the_rulings_reading():
    """Ruling (a) of 2026-11-20: the offer record's facts, read through one
    typed accessor that pin 2 exempts permanently. The open offer and the
    answers are read when it is built; each decline's turn is a reader of its
    own, asked at most once."""
    assert typing.get_type_hints(OfferView) == {
        "open_proposal_id": Optional[str],
        "answers": tuple[tuple[str, AnswerKind], ...],
        "_false_alarm_turn": Callable[[], Optional[int]],
        "_resolve_turn": Callable[[], Optional[int]],
    }
    view = OfferView(None, (), lambda: None, lambda: None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.open_proposal_id = "0" * 16  # type: ignore[misc]


def test_each_decline_turn_is_read_once_on_the_first_ask():
    """Its value or its failure is kept: a reader is never asked twice."""
    asked: list = []

    def turn():
        asked.append("turn")
        return 3

    def fails():
        asked.append("fails")
        raise RuntimeError("probe")

    view = OfferView(None, (), fails, turn)
    assert asked == []
    assert view.resolve_decline_turn() == view.resolve_decline_turn() == 3
    for _ in range(2):
        with pytest.raises(RuntimeError, match="probe"):
            view.false_alarm_decline_turn()
    assert asked == ["turn", "fails"]


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
    for read in ("open_proposal_id", "answers"):
        assert any(getattr(v, read) not in (None, ()) for v in views), read
    for read in ("false_alarm_decline_turn", "resolve_decline_turn"):
        turns = [getattr(v, read)() for v in views]
        assert all(t is None or type(t) is int for t in turns), read
        assert any(t is not None for t in turns), read
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


def test_the_prompt_takes_the_decline_and_the_hold_from_the_record():
    """The record says the decline stands and the problem is not on hold; the
    live problem goes INVALIDATED. The selection follows the record, and the
    decline's turn and the open offer the offer record."""
    case = oc._prompt_case("O:declined_this_turn")
    fold = NarrationFold(case)
    with ps.frozen():
        outcome = fold.outcome("record wins")
    assert outcome.resolution.declined_stands, "premise"
    assert outcome.problem.status == ProblemStatus.VERIFIED, "premise"
    case.progress.problem_status = ProblemStatus.INVALIDATED
    assert resolve_declined(outcome, fold.fresh(), case.current_turn) == (
        ResolveDeclined(actions=True, held=False, this_turn=True)
    )


@pytest.mark.parametrize("name", [n for n in oc.PROMPT if n != "D:omitted"])
def test_the_folds_offers_are_offer_view(name):
    """The prompt's offer reader is ``offer_view`` over the case it folds:
    the record wins over a live re-derivation by construction, since nothing
    else is read."""
    case = oc._prompt_case(name)
    with ps.frozen():
        view, want = NarrationFold(case).fresh().offers(), offer_view(case)
        for read in ("false_alarm_decline_turn", "resolve_decline_turn"):
            assert getattr(view, read)() == getattr(want, read)(), read
    assert (view.open_proposal_id, view.answers) == (
        want.open_proposal_id,
        want.answers,
    )


# ---------------------------------------------------------------------------
# The reopen chip's two copies agree (``project_offers``'s and the prompt's)
# ---------------------------------------------------------------------------


def test_on_hold_is_problem_on_holds_set():
    held = {
        status
        for status in ProblemStatus
        if problem_on_hold(
            SimpleNamespace(progress=SimpleNamespace(problem_status=status))
        )
    }
    assert set(ON_HOLD) == held


@pytest.mark.parametrize("name", [n for n in oc.PROMPT if n != "D:omitted"])
def test_the_prompts_chips_are_the_projections(name):
    case = oc._prompt_case(name)
    with ps.frozen():
        fold = NarrationFold(case)
        outcome = fold.outcome("chips")
        if not outcome.resolution.declined_stands:
            return
        declined = resolve_declined(outcome, fold.fresh(), case.current_turn)
        assert declined.actions == (project_offers(case).reopen_chip is not None)


def test_the_corpus_shows_the_chip_both_ways():
    """The parity test above sees a chip and its absence."""
    shown = {
        ORACLE[f"prompt:{n}"]["unsteered"]["resolve_declined"]["actions"]
        for n in oc.PROMPT
        if n != "D:omitted" and ORACLE[f"prompt:{n}"]["unsteered"]["resolve_declined"]
    }
    assert shown == {True, False}


# ---------------------------------------------------------------------------
# A decline's turn is derived only where ``main`` derived it
# ---------------------------------------------------------------------------


def _counting(monkeypatch, *names, when=lambda: True) -> list:
    """Each ``offers`` standing-decline reader ``names`` counts and raises
    while ``when()`` holds."""
    asked: list = []
    for name in names:
        real = getattr(offers_module, name)

        def standing(case, real=real, name=name):
            if not when():
                return real(case)
            asked.append(name)
            raise RuntimeError("probe")

        monkeypatch.setattr(offers_module, name, standing)
    return asked


def test_the_false_alarm_hold_derives_no_resolve_decline(monkeypatch):
    """The review's probe: a false alarm with a resolve decline on record. The
    hold asks for the false-alarm decline alone, as ``main`` did; the resolve
    decline's derivation failing omits nothing."""
    case = oc._prompt_case("G:declined_held")
    case.progress.problem_status = ProblemStatus.INVALIDATED
    with ps.frozen():
        want = templates._focus_emphasis(
            NarrationFold(case), case.current_turn, steered=True, zoned=True
        )
        asked = _counting(monkeypatch, "standing_resolve_decline")
        fold = NarrationFold(case)
        fold.outcome("probe")  # the record, folded before the patch bites
        got = templates._focus_emphasis(
            fold, case.current_turn, steered=True, zoned=True
        )
    assert "INVESTIGATION PROGRESS: Reported problem not present" in want, "premise"
    assert got == want and PROGRESS_OMITTED not in got
    assert asked == []


@pytest.mark.parametrize(
    "before", [None, "_withdraw_the_finding", "_withdraw_the_offer"]
)
def test_the_turn_narration_derives_no_decline(monkeypatch, before):
    """B3's whole turn, the engine's offer beside the model's ``resolved``, on
    a case with a standing resolve decline: while the narration step runs,
    each standing-decline reader raises. It is never asked, and the turn
    renders as it does unpatched."""
    real_case = revision._case

    def declined():
        case = real_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        assert offers_module.standing_resolve_decline(case) is not None, "premise"
        return case

    monkeypatch.setattr(revision, "_case", declined)
    hook = None if before is None else getattr(oc.b3, before)
    fails = before != "_withdraw_the_offer"
    want = oc._b3(hook, tokens_fail=fails)
    narrating: list = []
    asked = _counting(
        monkeypatch,
        "standing_resolve_decline",
        "standing_false_alarm_decline",
        when=lambda: bool(narrating),
    )
    real = turn_completion.narrate_turn

    def narrate(case, metadata):
        narrating.append(1)
        try:
            return real(case, metadata)
        finally:
            narrating.clear()

    monkeypatch.setattr(turn_completion, "narrate_turn", narrate)
    assert oc._b3(hook, tokens_fail=fails) == want
    assert asked == []


def test_the_turn_narration_reads_the_offer_view(monkeypatch):
    """A view that disagrees with ``case.offer_record`` (nothing open, no
    answer): ``_lapsed`` and ``_engine_offer_stands`` follow the view, and
    ask for no decline."""
    stands, lapsed = "a" * 16, "b" * 16

    def never():
        raise AssertionError("a decline was asked")

    view = OfferView(stands, ((lapsed, AnswerKind.LAPSED),), never, never)
    monkeypatch.setattr(turn_narration, "offer_view", lambda _c: view, raising=False)
    case = investigating_case()
    assert case.offer_record.open is None and not case.offer_record.answers
    assert turn_narration._engine_offer_stands(case, {}, {"proposal_id": stands}) == {
        "proposal_id": stands,
        "stands": True,
    }
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
