"""The prompt's milestone status, exclusion rule and focus emphasis selected
from the case outcome record (#2811 S2b-1c-i, #2615 stage S2).

The milestone status (``context_builder.assembly``), the exclusion rule's
action half (``causal_graph_block.exclusions_reminder_parts``) and the four
focus-emphasis lines (``templates.assembly``) are selected by
``narration.prompt_state`` from the record and the inputs read fresh beside
it (``PromptStateFresh``); the emphasis functions take only the selection.
On every fixture of the corpus (``prompt_state_cases_2811``) the three
places render byte for byte what ``main`` rendered
(``prompt_state_oracle_2811.json``, captured through ``get_prompt_for_case``
at 19ab0e8b8), but on the fixtures of the declared differences, where they
differ exactly as declared.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.narration.fix_verification import CaseIdentity
from faultmaven.core.investigation.narration.prompt_state import (
    MILESTONES_OMITTED,
    PROGRESS_OMITTED,
    FocusZone,
    PromptStateFresh,
    cause_work_rule,
    focus_zone,
    milestone_status,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.prompts.context_builder import (
    assembly as context_assembly,
)
from faultmaven.core.investigation.prompts.context_builder import (
    narration_fold as fold_module,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    exclusions_reminder,
)
from faultmaven.core.investigation.prompts.context_builder.evidence import (
    _symptom_currency_note,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from faultmaven.modules.case.contracts import (
    CaseState,
    InvestigationStage,
    ProblemStatus,
    WritePoint,
)
from tests.unit.core.investigation.prompts.prompt_state_cases_2811 import (
    CASES,
    build,
    frozen,
    render,
)
from tests.unit.core.investigation.prompts.test_fix_verification_record_2811 import (
    _IDENTITY,
    _Poisoned,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "prompt_state_oracle_2811.json").read_text()
)

_VERIFYING_LINE = (
    "\nNo row is on record as what showed the problem: cite the "
    "symptom_evidence rows already on record that showed it in "
    "symptom_verification_evidence_ids. Until then no re-check can confirm "
    "the problem gone.\n"
)
_ACTION_HALF = (
    " Any other alternative is not excluded: name the check that would "
    "settle it, or record the exclusion this turn (link the row that "
    "excludes it with stance REFUTES and set state=REFUTED)."
)

#: One entry per declared difference, by the fixtures that show it.
DECLARED = {
    # A fold that raises: the milestone status and the focus emphasis are
    # their declared lines, and the exclusion rule leaves out its action half.
    "omitted": ("D:omitted",),
    # A problem VERIFIED with no verification record: the record cannot tell
    # "no verification" from "no verifying id", and says no row is on record
    # (``main`` printed nothing). In production a VERIFIED problem always has
    # its record (``problem_status.py``'s one writer).
    "verifying": (
        "E:diagnosis",
        "E:exhausted_only",
        "E:exhausted_overflow",
        "E:surface_rotation",
        "E:render_cap",
        "E:fallback",
        "E:row_text",
        "E:checklist_overflow",
    ),
}
_DECLARED_FIXTURES = {name for names in DECLARED.values() for name in names}


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_each_place_renders_as_main_did(name):
    assert render(await build(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The declared differences, each shown by its fixtures, and only those
# ---------------------------------------------------------------------------


async def _shows_omitted(case, got, want):
    """Every line the record selects is its declared line; the exclusion
    record stays, without the action half (``main``'s)."""
    assert _ACTION_HALF in want["exclusions"], "premise"
    omitted = f"\n{PROGRESS_OMITTED}\n"
    return got == {
        "milestones": MILESTONES_OMITTED,
        "exclusions": want["exclusions"].replace(_ACTION_HALF, ""),
        "focus": omitted,
        "focus_knowledge_query": omitted,
    }


async def _shows_verifying(case, got, want):
    """The verifying-rows line is appended to ``main``'s steered emphasis."""
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert case.problem_verification is None
    return got == {**want, "focus": want["focus"] + _VERIFYING_LINE}


_SHOWS = {"omitted": _shows_omitted, "verifying": _shows_verifying}


async def test_the_declared_differences_are_the_plans():
    """The fixtures that render other than the oracle are exactly the
    declared ones, and each difference has its check."""
    differing = {name for name in CASES if render(await build(name)) != ORACLE[name]}
    assert differing == _DECLARED_FIXTURES
    assert set(DECLARED) == set(_SHOWS)


@pytest.mark.parametrize(
    "difference, name",
    [(d, name) for d, names in sorted(DECLARED.items()) for name in names],
)
async def test_each_declared_difference_is_shown_as_declared(difference, name):
    case = await build(name)
    assert await _SHOWS[difference](case, render(case), ORACLE[name])


async def test_a_moved_closure_reason_moves_none_of_these_places():
    """``D:closure`` (a closed case whose stored reason moved) is a terminal
    case: no milestone status, exclusion rule or focus emphasis renders."""
    assert ORACLE["D:closure"] == {
        "milestones": "",
        "exclusions": "",
        "focus": None,
        "focus_knowledge_query": None,
    }
    assert render(await build("D:closure")) == ORACLE["D:closure"]


# ---------------------------------------------------------------------------
# Pin 2: the selectors and the emphasis functions read the record, the
# identity and the fresh inputs alone
# ---------------------------------------------------------------------------


def test_the_fresh_inputs_are_the_plans():
    assert {f.name for f in dataclasses.fields(PromptStateFresh)} == {
        "mitigation_verified",
        "symptom_stale",
        "false_alarm_declined_at",
        "resolve_decline_turn",
        "reopen_chip",
    }


@pytest.mark.parametrize(
    "function",
    [
        templates._problem_hold_emphasis,
        templates._get_diagnosis_focus_emphasis,
        templates._resolve_declined_emphasis,
        templates._verifying_rows_missing_emphasis,
    ],
)
def test_each_emphasis_function_takes_only_a_selection(function):
    (parameter,) = inspect.signature(function).parameters.values()
    assert parameter.name != "case"
    assert "Case" not in str(parameter.annotation)


def _expected(name: str) -> dict:
    """What the branch renders: the oracle, with the verifying difference."""
    want = dict(ORACLE[name])
    if name in DECLARED["verifying"]:
        want["focus"] += _VERIFYING_LINE
    return want


@pytest.mark.parametrize(
    "name", sorted(n for n in set(CASES) - set(DECLARED["omitted"]))
)
async def test_the_selections_read_no_case_field(name):
    """The milestone status and both focus emphases, composed from the record
    read off a poisoned case, its identity and the fresh inputs (read from
    the real case, which pin 2 allows); and the exclusion rule's half."""
    case = await build(name)
    want = _expected(name)
    if case.state != CaseState.INVESTIGATING:
        assert want["milestones"] == "" and want["focus"] is None
        return
    with frozen():
        outcome = narration_outcome(case)
        fresh = NarrationFold(case).fresh()
        poisoned = _Poisoned(case, _IDENTITY, outcome)
        identity = CaseIdentity.of(poisoned)
        record = poisoned.progress.outcome
        status = milestone_status(record, fresh)
        note = _symptom_currency_note(case, "symptom_verified")
        assert context_assembly._milestone_lines(status, note) == want["milestones"]
        stage = identity.current_stage or InvestigationStage.DIAGNOSIS
        for key, steered in (("focus", True), ("focus_knowledge_query", False)):
            focus = templates._focus_lines(
                record,
                fresh,
                identity.current_turn,
                steered=steered,
                zoned=steered and stage == InvestigationStage.DIAGNOSIS,
            )
            assert focus == want[key], key
    if want["exclusions"] and "never that it is absent" not in want["exclusions"]:
        assert cause_work_rule(record) == (_ACTION_HALF in want["exclusions"])


# ---------------------------------------------------------------------------
# Laziness: the record and each fresh input are read only where ``main``
# read the case for them, and at most once per assembly
# ---------------------------------------------------------------------------


@pytest.fixture
def failures(monkeypatch) -> list:
    counted: list = []
    monkeypatch.setattr(
        fold_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(
            labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
        ),
    )
    return counted


def _raise(*_args, **_kwargs):
    raise RuntimeError("probe")


@pytest.mark.parametrize("name", ["P:inquiry", "terminal_qa", "D:closure"])
async def test_no_fold_outside_an_investigation(monkeypatch, failures, name):
    """INQUIRY and terminal prompts print none of these lines and never fold
    for them."""
    case = await build(name)
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert render(case) == ORACLE[name]
    assert failures == []


@pytest.mark.parametrize(
    "name",
    [
        # A confirmed fix: the rule's action half is not decided.
        "offer_answered_at_0b",
        # No hypothesis: nothing to speak of.
        "2609_relapsed_mitigation",
    ],
)
async def test_the_exclusion_rule_folds_only_where_it_is_decided(
    monkeypatch, failures, name
):
    case = await build(name)
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    with frozen():
        assert exclusions_reminder(case, NarrationFold(case)) == (
            ORACLE[name]["exclusions"]
        )
    assert failures == []


#: Each fresh input, and fixtures where ``main`` did not read the case for
#: it: no Zone 2 on DIAGNOSIS; no false alarm; no standing resolve decline.
_NOT_ASKED = {
    "_symptom_stale": ["gate_one", "R:other_cause", "P:false_alarm_declined"],
    "_false_alarm_declined_at": ["gate_one", "P:revision_hypotheses", "P:stale"],
    "_resolve_decline_turn": ["gate_one", "R:other_cause", "N:held_pending"],
    "_reopen_chip": ["gate_one", "R:other_cause", "N:held_pending"],
}


@pytest.mark.parametrize(
    "reader, name",
    [(reader, name) for reader, names in _NOT_ASKED.items() for name in names],
)
async def test_a_fresh_input_is_read_only_where_its_line_is_decided(
    monkeypatch, failures, reader, name
):
    case = await build(name)
    monkeypatch.setattr(fold_module, reader, _raise)
    assert render(case) == _expected(name)
    assert failures == []


async def test_each_input_is_read_once_per_assembly(monkeypatch):
    """Three builds of one assembly read the record and each fresh input
    once."""
    case = await build("offer_answered_at_0b")
    calls: dict = {}

    def counted(reader):
        real = getattr(fold_module, reader)

        def read(c):
            calls[reader] = calls.get(reader, 0) + 1
            return real(c)

        monkeypatch.setattr(fold_module, reader, read)

    for reader in (
        "narration_outcome",
        "_stored_mitigation_verified",
        "_symptom_stale",
        "_resolve_decline_turn",
        "_reopen_chip",
    ):
        counted(reader)
    fold = NarrationFold(case)
    with frozen():
        for _ in range(3):
            context_assembly._milestone_status_section(case, fold)
            templates._focus_emphasis(fold, case.current_turn, steered=True, zoned=True)
            exclusions_reminder(case, fold)
    assert calls == dict.fromkeys(calls, 1) and len(calls) == 5


# ---------------------------------------------------------------------------
# Failure: each place prints its declared line, counted once, never raising
# ---------------------------------------------------------------------------


async def test_a_fold_failure_prints_the_declared_lines_once_counted(
    monkeypatch, failures
):
    case = await build("R:other_cause")
    want = ORACLE["R:other_cause"]
    assert _ACTION_HALF in want["exclusions"], "premise"
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    got = render(case)
    assert got["milestones"] == MILESTONES_OMITTED
    assert got["focus"] == got["focus_knowledge_query"] == f"\n{PROGRESS_OMITTED}\n"
    assert got["exclusions"] == want["exclusions"].replace(_ACTION_HALF, "")
    # Once per assembly: ``render`` builds two prompts.
    assert failures == [WritePoint.NARRATION] * 2


#: Each fresh input, a fixture whose prompt reads it, the places its failure
#: omits, and how many of the fixture's two prompts (no processing mode, a
#: knowledge_query turn) read it: the milestone status reads the stored
#: verdict on both; a knowledge_query turn's emphasis carries only the
#: resolve-decline lines.
_FRESH_FAILURES = [
    ("_stored_mitigation_verified", "2609_relapsed_mitigation", {"milestones"}, 2),
    ("_symptom_stale", "P:stale", {"focus"}, 1),
    ("_false_alarm_declined_at", "P:false_alarm_declined", {"focus"}, 1),
    (
        "_resolve_decline_turn",
        "P:declined_earlier",
        {"focus", "focus_knowledge_query"},
        2,
    ),
    ("_reopen_chip", "offer_answered_at_0b", {"focus", "focus_knowledge_query"}, 2),
]


@pytest.mark.parametrize("reader, name, places, prompts", _FRESH_FAILURES)
async def test_a_fresh_read_failure_omits_only_its_own_lines(
    monkeypatch, failures, reader, name, places, prompts
):
    case = await build(name)
    monkeypatch.setattr(fold_module, reader, _raise)
    got = render(case)
    for place, text in ORACLE[name].items():
        if place not in places:
            assert got[place] == text, place
        elif place == "milestones":
            assert got[place] == MILESTONES_OMITTED
        else:
            assert got[place] == f"\n{PROGRESS_OMITTED}\n", place
    assert failures == [WritePoint.NARRATION] * prompts


async def test_a_failed_counter_still_prints_the_declared_lines(monkeypatch):
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    monkeypatch.setattr(
        fold_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(labels=lambda at: SimpleNamespace(inc=_raise)),
    )
    got = render(await build("R:other_cause"))
    assert got["milestones"] == MILESTONES_OMITTED


# ---------------------------------------------------------------------------
# The zone, selected from the record (the selection half of the focus
# emphasis's tests that passed a bare ``InvestigationProgress``)
# ---------------------------------------------------------------------------


def _fresh(*, stale: bool = False) -> PromptStateFresh:
    def never():
        raise AssertionError("not asked")

    return PromptStateFresh(
        mitigation_verified=never,
        symptom_stale=lambda: stale,
        false_alarm_declined_at=never,
        resolve_decline_turn=never,
        reopen_chip=never,
    )


@pytest.mark.parametrize(
    "shown, stale, zone",
    [
        ([], True, FocusZone.VERIFY),
        (["symptom_verified"], False, FocusZone.CAUSE),
        (["symptom_verified"], True, FocusZone.CAUSE_STALE),
        (["symptom_verified", "root_cause_identified"], True, FocusZone.SOLUTION),
        (
            ["symptom_verified", "root_cause_identified", "solution_proposed"],
            True,
            FocusZone.PENDING,
        ),
    ],
)
async def test_the_zone_is_the_records_milestones(shown, stale, zone):
    outcome = narration_outcome(await build("R:other_cause"))
    outcome = outcome.model_copy(update={"milestones_shown": shown})
    assert focus_zone(outcome, _fresh(stale=stale)) == zone
