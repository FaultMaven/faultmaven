"""The offer facts' narrators: their fixture corpus (#2811 S2b-2d).

Two narrators read the offer record: the prompt's focus selection (the false
alarm's declined close, the resolve decline's turn and its chips) and the
turn's narration (``_engine_offer_stands``, ``_lapsed``).
``render_all`` renders, through entry points that exist on ``main`` and on
the branch:

* ``prompt:<name>``: ``focus_selection`` and ``_focus_emphasis``, steered and
  zoned and neither, from one ``NarrationFold``, over the prompt-state corpus
  (``prompt_state_cases_2811.CASES``), the golden corpus's cases and
  ``ADDED``;
* ``turn:<name>``: the narration step's notices and flags over the golden
  corpus's decision cycles (``CYCLE_SCENARIOS``, ``B1C_SCENARIOS``, through
  ``narrated``), its offers (``B1D_SCENARIOS``) and the engine offer that
  stood, lapsed after the drop, or closed but not lapsed (B3, ``TOKENS_FAIL``
  off: the error log path).

Its stored output is ``offer_oracle_2811.json``, captured on ``main`` at
5e5b21e42 by running ``render_all`` with this file copied into a ``git
archive`` extract of that commit, never typed by hand but for the S2c-3a
entry below. ``POOL_EMPTY``'s two
entries (#2811 S2c-1b) were rendered at c1ade8d8c; each differs from ``main``
at 5a1d3b130 only in its "Root cause" line (``main``: the bare ask).
#2811 S2c-3a re-rendered ``turn:offer:turn_replayed_rca_infeasible_close``
by hand: it equalled the stored entry on ``main`` at 77f3b9b8b, and its
reply on the S2c-3a branch at c31e9e4ae, which is stored, ends with the
outcome statement "**Case record:** the problem is confirmed.".
#3039 re-rendered ``turn:cycle:licence_downgraded_once_it_holds`` and
``turn:cycle:licence_downgraded_while_it_is_down`` by hand: each equalled the
stored entry on ``main`` at 68ec0ac01, and on the #3039 branch, which is
stored, its notice ends with the sentence that the user's report is recorded
(INV-49).
"""

from __future__ import annotations

import asyncio
import re
from enum import Enum
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.milestone_engine import turn_narration
from faultmaven.core.investigation.narration import codes as narration_codes
from faultmaven.core.investigation.narration.prompt_state import focus_selection
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import test_lapsed_offer_b3_2636 as b3
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.prompts import prompt_state_cases_2811 as ps

#: The focus emphasis's two shapes: a steered DIAGNOSIS turn, and a
#: knowledge_query turn (the resolve-decline lines alone).
MODES = {"steered": (True, True), "unsteered": (False, False)}

#: The golden corpus's cases (``golden_cases_2636.SCENARIOS``' inputs), the
#: standing resolve declines among them.
GOLDEN = {
    "G:invalidated": golden._invalidated,
    "G:held": golden._held,
    "G:held_fulfilled": lambda: golden._held(fulfilled=True),
    "G:unasked": lambda: golden._held(asked=False),
    "G:bare": golden._bare_case,
    "G:failed_fix": golden._failed_fix,
    "G:identified": lambda: golden._identified_case()[0],
    "G:confirmed": golden._confirmed_case,
    "G:declined_both_chips": lambda: golden._declined(golden._held(asked=False)),
    "G:declined_held": lambda: golden._declined(golden._held()),
    "G:declined_held_fulfilled": lambda: golden._declined(golden._held(fulfilled=True)),
}


def _declined_this_turn(*, offer_open: bool = False):
    """A resolve decline given on the current turn (``this_turn``)."""
    from tests.case_builders import with_open_offer

    case = golden._declined(golden._confirmed_case())
    if offer_open:
        with_open_offer(case, kind="close", basis="model")
    return case


#: The fixtures the plan adds that neither corpus holds. A standing decline
#: with an offer open (``P:declined_offer_open``) and a declined false-alarm
#: close while INVALIDATED (``P:false_alarm_declined``) are in the
#: prompt-state corpus.
ADDED = {
    "O:declined_revision_pending": lambda: golden._declined(
        golden._confirmed_case(), on_hold=True
    ),
    "O:declined_this_turn": _declined_this_turn,
    "O:declined_this_turn_offer_open": lambda: _declined_this_turn(offer_open=True),
}

PROMPT = [*ps.CASES, *GOLDEN, *ADDED]


def _plain(value):
    if hasattr(value, "_asdict"):
        return {k: _plain(v) for k, v in value._asdict().items()}
    return value.value if isinstance(value, Enum) else value


def _prompt_case(name: str):
    if name in ps.CASES:
        return asyncio.run(ps.build(name))
    with ps.frozen():
        return {**GOLDEN, **ADDED}[name]()


def render_prompt(case) -> dict:
    """The selection and the emphasis, by mode, from one fold."""
    fold = NarrationFold(case)
    out: dict = {}
    with ps.frozen():
        for mode, (steered, zoned) in MODES.items():
            outcome = fold.outcome("oracle")
            try:
                selection = (
                    None
                    if outcome is None
                    else _plain(
                        focus_selection(
                            outcome,
                            fold.fresh(),
                            case.current_turn,
                            steered=steered,
                            zoned=zoned,
                        )
                    )
                )
            except Exception as error:  # noqa: BLE001 — the omission is the output
                selection = f"raised {type(error).__name__}"
            out[mode] = selection
            out[f"{mode}_text"] = templates._focus_emphasis(
                fold, case.current_turn, steered=steered, zoned=zoned
            )
    return out


#: The narration step's flags (B3) and conflicts.
_FLAGS = (
    "transition_superseded_by_engine",
    "transition_dropped_for_lapsed_offer",
    turn_narration.NARRATION_CONFLICTS_KEY,
)
_PROPOSAL = re.compile(r"\b[0-9a-f]{16}\b")


def _cycle(build) -> dict:
    case, metadata = build()
    notices = golden.cycle_text(narrated(case, metadata))
    return {
        "notices": notices,
        "prose": {
            k: golden.cycle_text([metadata[k]])[0]
            for k in golden.PROSE_KEY_NAMES
            if k in metadata
        },
        "flags": {k: metadata[k] for k in _FLAGS if k in metadata},
    }


def _b3(before, *, tokens_fail: bool = True) -> dict:
    """B3's whole turn: the engine's false-alarm close beside the model's
    ``resolved``, ``before`` run just before the narration step."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(narration_codes, "TOKENS_FAIL", tokens_fail)
        with patch.object(turn_narration.logger, "error") as error:
            (entry,), extra, notices = asyncio.run(b3._turn(mp, before))
    return {
        "notices": golden.cycle_text(notices),
        "params": {k: v for k, v in entry["params"].items() if k != "proposal_id"},
        "flags": {k: extra[k] for k in _FLAGS[:2]},
        "errors": [
            _PROPOSAL.sub("<proposal>", c.args[2]) for c in error.call_args_list
        ],
    }


def _held_open():
    """A validated root whose why is open: identification held open."""
    case, root, _hyp = golden._m6_identified_case()
    root.metadata = {}
    golden._recompute_at(case, 5)
    return case


#: The needs-info request whose record holds a pool-empty reason beside the
#: "Root cause" ask (#2811 S2c-1b).
POOL_EMPTY = {
    "offer:needs_info_no_cause": lambda: golden._with_evidence(golden._bare_case()),
    "offer:needs_info_held_open": _held_open,
}

TURNS = {
    **{
        f"cycle:{n}": (lambda b=b: _cycle(b)) for n, b in golden.CYCLE_SCENARIOS.items()
    },
    **{f"cycle:{n}": (lambda b=b: _cycle(b)) for n, b in golden.B1C_SCENARIOS.items()},
    **{f"offer:{n}": b for n, b in golden.B1D_SCENARIOS.items()},
    "b3:stands": lambda: _b3(None),
    "b3:lapsed_after_the_drop": lambda: _b3(b3._withdraw_the_finding),
    "b3:closed_not_lapsed": lambda: _b3(b3._withdraw_the_offer, tokens_fail=False),
    **{
        n: (lambda b=b: golden.offer_texts(*golden._models_proposal(b(), "resolved")))
        for n, b in POOL_EMPTY.items()
    },
}

NAMES = [f"prompt:{n}" for n in PROMPT] + [f"turn:{n}" for n in TURNS]


def render(name: str):
    """The fixture ``name``'s output."""
    kind, _, fixture = name.partition(":")
    if kind == "prompt":
        return render_prompt(_prompt_case(fixture))
    return TURNS[fixture]()


def render_all() -> dict:
    """Every fixture's output, by name. Run outside an event loop: the
    golden corpus's whole turns run their own."""
    return {name: render(name) for name in NAMES}
