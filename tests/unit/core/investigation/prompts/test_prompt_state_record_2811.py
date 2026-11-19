"""The prompt's milestone status, exclusion rule, focus emphasis, state
summary and fallback prompt selected from the case outcome record (#2811
S2b-1c-i, #2615 stage S2).

The milestone status (``context_builder.assembly``), the exclusion rule's
action half (``causal_graph_block.exclusions_reminder_parts``), the four
focus-emphasis lines (``templates.assembly``), the state summary's
``Verified:`` line (``context_builder.history``) and the fallback prompt's
milestones and resolution (``templates.fallback``) are selected by
``narration.prompt_state`` from the record and the inputs read fresh beside
it; the emphasis functions take only the selection. On every fixture of the
corpus (``prompt_state_cases_2811``) these places render byte for byte what
``main`` rendered (``prompt_state_oracle_2811.json``, captured through
``get_prompt_for_case`` and ``get_fallback_prompt_for_case`` at 19ab0e8b8),
but on the fixtures of the declared differences, where they differ exactly
as declared.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.narration import prompt_state
from faultmaven.core.investigation.narration.fix_verification import CaseIdentity
from faultmaven.core.investigation.narration.prompt_state import (
    CLOSURE_MODEL_TEXT,
    MILESTONES_OMITTED,
    PROGRESS_OMITTED,
    RESOLUTION_OMITTED,
    FocusZone,
    PromptStateFresh,
    focus_zone,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.prompts.context_builder import (
    assembly as context_assembly,
)
from faultmaven.core.investigation.prompts.context_builder import (
    history as history_module,
)
from faultmaven.core.investigation.prompts.context_builder import (
    narration_fold as fold_module,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    exclusions_reminder,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from faultmaven.core.investigation.prompts.templates import (
    fallback as fallback_module,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    ClosureReason,
    InvestigationStage,
    ProblemStatus,
    WritePoint,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
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
_VERIFIED = re.compile(r"^Verified: .*$", re.M)
_FB_MILESTONES = re.compile(r"^MILESTONES COMPLETED: .*$", re.M)
_FB_RESOLUTION = re.compile(r"^RESOLUTION: .*$", re.M)

#: One entry per declared difference, by the fixtures that show it.
DECLARED = {
    # A fold that raises: the milestone status, the focus emphasis, the state
    # summary's verified line and the fallback's milestones are their
    # declared lines, and the exclusion rule leaves out its action half.
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
    # A closed case whose stored reason moved since its terminal write: the
    # fallback's resolution states the record's (critique finding 4).
    "closure": ("D:closure",),
}
_DECLARED_FIXTURES = {name for names in DECLARED.values() for name in names}


def _omitted(want: dict) -> dict:
    progress = f"\n{PROGRESS_OMITTED}\n"
    return {
        "milestones": MILESTONES_OMITTED,
        "exclusions": want["exclusions"].replace(_ACTION_HALF, ""),
        "focus": progress,
        "focus_knowledge_query": progress,
        "state_summary": _VERIFIED.sub(
            "Verified: unknown", want["state_summary"], count=1
        ),
        "fallback": _FB_MILESTONES.sub(
            f"MILESTONES COMPLETED: {MILESTONES_OMITTED}", want["fallback"]
        ).replace(_ACTION_HALF, ""),
    }


def _expected(name: str, case=None) -> dict:
    """What the branch renders: the oracle, with the declared differences."""
    want = dict(ORACLE[name])
    if name in DECLARED["omitted"]:
        return _omitted(want)
    if name in DECLARED["verifying"]:
        want["focus"] += _VERIFYING_LINE
    if name in DECLARED["closure"]:
        reason = narration_outcome(case).closure.reason
        want["fallback"] = _FB_RESOLUTION.sub(
            f"RESOLUTION: {reason.value}", want["fallback"]
        )
    return want


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_each_place_renders_as_main_did(name):
    assert render(await build(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The declared differences, each shown by its fixtures, and only those
# ---------------------------------------------------------------------------


async def test_the_declared_differences_are_the_plans():
    """The fixtures that render other than the oracle are exactly the
    declared ones."""
    differing = {name for name in CASES if render(await build(name)) != ORACLE[name]}
    assert differing == _DECLARED_FIXTURES


@pytest.mark.parametrize(
    "difference, name",
    [(d, name) for d, names in sorted(DECLARED.items()) for name in names],
)
async def test_each_declared_difference_is_shown_as_declared(difference, name):
    case = await build(name)
    want = ORACLE[name]
    if difference == "omitted":
        assert _ACTION_HALF in want["exclusions"], "premise"
        assert "Verified: symptom" in want["state_summary"], "premise"
    if difference == "verifying":
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.problem_verification is None
    if difference == "closure":
        assert case.closure_reason != narration_outcome(case).closure.reason
    got = render(case)
    assert got != want
    assert got == _expected(name, case)


# ---------------------------------------------------------------------------
# Pin 2: the production entry points read the record, the identity and the
# fresh inputs alone
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


def _poisoned_fold(case, allowed=_IDENTITY):
    """The assembly's fold over ``case``, its record and every fresh input
    read from the real case (pin 2 allows them), then its case poisoned: an
    entry point that reads the case past them fails. Returns the fold and the
    poisoned case."""
    fold = NarrationFold(case)
    outcome = fold.outcome("pin 2")
    fresh = fold.fresh()
    for f in dataclasses.fields(fresh):
        getattr(fresh, f.name)()
    fold.currency_note()
    fold.fix_confirmed()
    poisoned = _Poisoned(case, allowed, outcome)
    fold._case = poisoned
    return fold, poisoned


@pytest.mark.parametrize("name", sorted(set(CASES) - set(DECLARED["omitted"])))
async def test_the_entry_points_read_no_case_field(name):
    """``_milestone_status_section``, ``_focus_emphasis`` (both modes), the
    state summary's and the fallback's record lines, and
    ``exclusions_reminder_parts`` (which reads the hypotheses, its record of
    exclusions, as case content), driven with the poisoned case."""
    case = await build(name)
    want = _expected(name, case)
    with frozen():
        fold, poisoned = _poisoned_fold(case)
        identity = CaseIdentity.of(poisoned)
        investigating = identity.state == CaseState.INVESTIGATING
        if investigating:
            assert context_assembly._milestone_status_section(fold) == (
                want["milestones"]
            )
            stage = identity.current_stage or InvestigationStage.DIAGNOSIS
            for key, steered in (("focus", True), ("focus_knowledge_query", False)):
                focus = templates._focus_emphasis(
                    fold,
                    identity.current_turn,
                    steered=steered,
                    zoned=steered and stage == InvestigationStage.DIAGNOSIS,
                )
                assert focus == want[key], key
            assert fallback_module._fallback_milestones(fold) in want["fallback"]
        else:
            assert want["milestones"] == "" and want["focus"] is None
        if identity.state in (CaseState.RESOLVED, CaseState.CLOSED):
            resolution = fallback_module._fallback_resolution(fold)
            assert f"RESOLUTION: {resolution}" in want["fallback"]
        verified = history_module._verified(poisoned, fold)
        assert f"Verified: {verified}\n" in want["state_summary"]
        hypotheses = _Poisoned(
            case, _IDENTITY | {"hypotheses"}, poisoned.progress.outcome
        )
        assert exclusions_reminder(hypotheses, fold) == want["exclusions"]


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


async def test_no_fold_in_inquiry(monkeypatch, failures):
    """An INQUIRY prompt prints none of these lines from the record and
    never folds for them."""
    case = await build("P:inquiry")
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert render(case) == ORACLE["P:inquiry"]
    assert failures == []


@pytest.mark.parametrize("name", ["terminal_qa", "P:resolved_verified"])
async def test_a_terminal_prompt_folds_for_two_lines_only(monkeypatch, failures, name):
    """A terminal case prints the state summary's verified line and the
    fallback's resolution from the record; a fold failure omits those two
    alone, counted once per assembly (two prompts and a fallback)."""
    case = await build(name)
    want = ORACLE[name]
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert render(case) == {
        **want,
        "state_summary": _VERIFIED.sub(
            "Verified: unknown", want["state_summary"], count=1
        ),
        "fallback": f"RESOLUTION: {RESOLUTION_OMITTED}",
    }
    assert failures == [WritePoint.NARRATION] * 3


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
#: it: no Zone 2 on DIAGNOSIS (Zone 1, Zone 3, Zone 3 pending, a hold, no
#: zone); no false alarm; no standing resolve decline.
_NOT_ASKED = {
    "_symptom_stale": [
        "gate_one",
        "hypothesis_action",
        "R:fix_failed",
        "R:other_cause",
        "P:false_alarm_declined",
    ],
    "_false_alarm_declined_at": ["gate_one", "P:revision_hypotheses", "P:stale"],
    "_resolve_decline_turn": ["gate_one", "R:other_cause", "N:held_pending"],
    "_reopen_chip": ["gate_one", "R:other_cause", "N:held_pending"],
}


async def test_the_zone_3_fixtures_are_zone_3():
    assert ORACLE["hypothesis_action"]["focus"].startswith(
        "\n**INVESTIGATION PROGRESS: Solution needed**"
    )
    assert ORACLE["R:fix_failed"]["focus"].startswith(
        "\n**INVESTIGATION PROGRESS: Solution proposal issued"
    )


@pytest.mark.parametrize(
    "reader, name",
    [(reader, name) for reader, names in _NOT_ASKED.items() for name in names],
)
async def test_a_fresh_input_is_read_only_where_its_line_is_decided(
    monkeypatch, failures, reader, name
):
    case = await build(name)
    monkeypatch.setattr(fold_module, reader, _raise)
    assert render(case) == _expected(name, case)
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

    readers = (
        "narration_outcome",
        "_stored_mitigation_verified",
        "_symptom_stale",
        "_resolve_decline_turn",
        "_reopen_chip",
        "_currency_note",
        "_fix_confirmed",
    )
    for reader in readers:
        counted(reader)
    fold = NarrationFold(case)
    with frozen():
        for _ in range(3):
            context_assembly._milestone_status_section(fold)
            templates._focus_emphasis(fold, case.current_turn, steered=True, zoned=True)
            exclusions_reminder(case, fold)
            history_module._build_state_summary(case, fold)
    assert calls == dict.fromkeys(readers, 1)


async def test_the_fallback_reads_the_assemblys_fold(monkeypatch, failures):
    """A prompt the backstop replaces with the fallback (a target no section
    fits) folds once for the assembly and every candidate render, and a fold
    failure is counted once."""
    case = await build("R:other_cause")
    real = outcome_module.compute_case_outcome
    calls = []

    def fold(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    with frozen(), patch.object(outcome_module, "compute_case_outcome", fold):
        prompt = templates.get_prompt_for_case(
            case, "status", given_tools=DEFAULT_GIVEN, target_tokens=200
        )
    assert "MILESTONES COMPLETED:" in prompt, "premise: the fallback is returned"
    assert len(calls) == 1
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    with frozen():
        prompt = templates.get_prompt_for_case(
            case, "status", given_tools=DEFAULT_GIVEN, target_tokens=200
        )
    assert f"MILESTONES COMPLETED: {MILESTONES_OMITTED}" in prompt
    assert failures == [WritePoint.NARRATION]


async def test_a_fallback_called_alone_folds_once_per_call(monkeypatch, failures):
    """The #662 retry has no assembly: one fold per call, never one per
    candidate render, and its failure counted once."""
    case = await build("R:other_cause")
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    with frozen():
        prompt = fallback_module.get_fallback_prompt_for_case(
            case, "x" * 40000, max_tokens=300
        )
    assert f"MILESTONES COMPLETED: {MILESTONES_OMITTED}" in prompt
    assert failures == [WritePoint.NARRATION]


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
    assert render(case) == _omitted(want)
    # Once per assembly: ``render`` builds two prompts and a fallback.
    assert failures == [WritePoint.NARRATION] * 3


#: Each fresh input a fold failure policy covers, a fixture whose prompt
#: reads it, the places its failure omits, and how many of the fixture's
#: three assemblies (no processing mode, a knowledge_query turn, the
#: fallback) read it: the milestone status reads the stored verdict on both
#: prompts; a knowledge_query turn's emphasis carries only the
#: resolve-decline lines. The symptom's currency is not among them: the
#: milestone status's currency note reads it first, unguarded, so its failure
#: fails the build, as on ``main``.
_FRESH_FAILURES = [
    ("_stored_mitigation_verified", "2609_relapsed_mitigation", {"milestones"}, 2),
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


async def test_a_currency_failure_fails_the_build_as_on_main(monkeypatch):
    """The currency note is read where ``- symptom_verified`` prints, outside
    the milestone status's guard, as ``main`` read it."""
    monkeypatch.setattr(fold_module, "_currency_note", _raise)
    with pytest.raises(RuntimeError, match="probe"):
        render(await build("P:stale"))


async def test_a_fault_in_the_emphasis_text_raises(monkeypatch, failures):
    """Only the selection is guarded: a fault in the text composition is a
    bug, raised as on ``main``, never reported as a record failure."""
    monkeypatch.setattr(templates, "_get_diagnosis_focus_emphasis", _raise)
    with pytest.raises(RuntimeError, match="probe"):
        render(await build("P:stale"))
    assert failures == []


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
# Pin 6, its model half (ruling Q2 (a)): every closure reason the fallback
# prints renders as it did
# ---------------------------------------------------------------------------


def test_every_closure_reason_has_its_model_rendering():
    assert CLOSURE_MODEL_TEXT == {reason: reason.value for reason in ClosureReason}


@pytest.mark.parametrize("reason", list(ClosureReason))
def test_each_closure_reason_renders_as_main_did(reason):
    assert ORACLE[f"P:closed_{reason.value}"]["fallback"] == (
        f"RESOLUTION: {reason.value}"
    )


async def test_a_reason_without_a_rendering_renders_its_value_counted(
    monkeypatch, failures, caplog
):
    reason = ClosureReason.CLOSED_FALSE_ALARM
    monkeypatch.setattr(
        prompt_state,
        "CLOSURE_MODEL_TEXT",
        {r: t for r, t in CLOSURE_MODEL_TEXT.items() if r != reason},
    )
    case = await build(f"P:closed_{reason.value}")
    with frozen(), caplog.at_level("ERROR", logger=fold_module.__name__):
        prompt = fallback_module.get_fallback_prompt_for_case(case, "status")
    assert f"RESOLUTION: {reason.value}" in prompt
    assert failures == [WritePoint.NARRATION]
    assert any("fallback resolution" in r.getMessage() for r in caplog.records)


async def test_a_reason_outside_the_enum_renders_its_value_counted(
    monkeypatch, failures
):
    """A record loaded without validation can hold a closure reason that is
    no ``ClosureReason`` (the docstring of ``_fallback_body`` names it): it
    renders as its value, counted once, and never raises."""
    case = await build("terminal_qa")
    with frozen():
        record = narration_outcome(case)
    bogus = record.model_copy(
        update={"closure": record.closure.model_copy(update={"reason": "bogus_reason"})}
    )
    monkeypatch.setattr(fold_module, "narration_outcome", lambda _case: bogus)
    with frozen():
        prompt = fallback_module.get_fallback_prompt_for_case(case, "status")
    assert "RESOLUTION: bogus_reason" in prompt
    assert failures == [WritePoint.NARRATION]


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
