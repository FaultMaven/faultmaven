"""The ``<evidence_needs>`` block rendered from the needs pool's view and the
case outcome record (#2811 S2b-1b-ii, #2615 stage S2).

The need sections render from ``NeedsView`` and ``content(id)`` alone; the
re-verification checklist from the record (``bindable``, the dated attempts),
``content(id, row=True)`` and, where no row verified the problem, the symptom rows read
fresh (``EvidenceNeedsFresh``). On every fixture of the corpus
(``evidence_needs_cases_2811``) the block is byte for byte what ``main``
rendered (``evidence_needs_oracle_2811.json``, captured through
``_build_evidence_needs_block`` at 9ed524b07), but on the fixtures of the
declared differences, where it differs exactly as declared.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.cause_assurance import fix_application_turn
from faultmaven.core.investigation.narration.evidence_needs import (
    CHECKLIST_OMITTED,
    EvidenceNeedsFresh,
    recheck_checklist,
    render_evidence_needs,
)
from faultmaven.core.investigation.narration.fix_verification import (
    CaseIdentity,
    Content,
    _dated,
    content_by_id,
)
from faultmaven.core.investigation.narration.needs import NeedsView
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.prompts.context_builder import (
    evidence_needs as call_site,
)
from faultmaven.core.investigation.prompts.context_builder import (
    narration_fold as fold_module,
)
from faultmaven.core.investigation.prompts.context_builder.evidence_needs import (
    _build_evidence_needs_block,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from faultmaven.modules.case.contracts import (
    CaseState,
    EvidenceCategory,
    InvestigationStage,
    WritePoint,
)
from tests.unit.core.investigation.bound_rows import bound_row
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.prompts.evidence_needs_cases_2811 import (
    CASES,
    build,
)
from tests.unit.core.investigation.prompts.test_fix_verification_record_2811 import (
    _IDENTITY,
    _no_text,
    _Poisoned,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "evidence_needs_oracle_2811.json").read_text()
)
_PLAIN = "confirm the fix held; emit *_absence_evidence when a signature is gone):"
_NAMING = (
    "confirm the fix held; emit *_absence_evidence when a signature is gone,\n"
    "naming the row it re-checks in rechecks):"
)

#: One entry per declared difference, by the fixtures that show it.
DECLARED = {
    # A fold that raises: the checklist is its one declared line.
    "omitted": ("D:omitted",),
    # The heading reads the record's dated attempts: an attempt only the
    # record still dates (carried, or its action gone) names the row.
    "heading": ("E:only_carried", "E:only_gone"),
}
_DECLARED_FIXTURES = {name for names in DECLARED.values() for name in names}


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_the_block_renders_as_main_did(name):
    assert _build_evidence_needs_block(await build(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The declared differences, each shown by its fixtures, and only those
# ---------------------------------------------------------------------------


def _checklist_section(block: str) -> str:
    start = block.index("Re-verification checklist")
    return block[start : block.index("\n</evidence_needs>")]


async def _shows_omitted(case, block, want):
    """The checklist (4 rows on ``main``) is the one declared line; the rest
    is ``main``'s."""
    assert _checklist_section(want).count("\n  - [") == 4, "premise"
    return block == want.replace(_checklist_section(want), CHECKLIST_OMITTED)


async def _shows_heading(case, block, want):
    """Only the record dates an attempt; the heading names the row."""
    assert fix_application_turn(case) is None and _dated(narration_outcome(case))
    return _PLAIN in want and block == want.replace(_PLAIN, _NAMING)


_SHOWS = {"omitted": _shows_omitted, "heading": _shows_heading}


async def test_the_declared_differences_are_the_plans():
    """The fixtures that render other than the oracle are exactly the
    declared ones, and each difference has its check."""
    differing = {
        name
        for name in CASES
        if _build_evidence_needs_block(await build(name)) != ORACLE[name]
    }
    assert differing == _DECLARED_FIXTURES
    assert set(DECLARED) == set(_SHOWS) == {"omitted", "heading"}


@pytest.mark.parametrize(
    "difference, name",
    [(d, name) for d, names in sorted(DECLARED.items()) for name in names],
)
async def test_each_declared_difference_is_shown_by_its_fixture(difference, name):
    case = await build(name)
    block = _build_evidence_needs_block(case)
    assert await _SHOWS[difference](case, block, ORACLE[name])


# ---------------------------------------------------------------------------
# Pin 2: the renderer reads the record, the identity, content, the needs view
# and the fresh rows alone
# ---------------------------------------------------------------------------


def test_the_inputs_hold_their_rulings_reading():
    assert Content._fields == ("text", "established_turn")
    assert [f.name for f in dataclasses.fields(EvidenceNeedsFresh)] == [
        "uncited_symptom_row_ids"
    ]


def _post_diagnosis(case) -> bool:
    return case.state == CaseState.INVESTIGATING and case.current_stage in (
        InvestigationStage.MITIGATION,
        InvestigationStage.TREATMENT,
    )


def _render(case, fresh=None):
    """The block on the poisoned case: the need sections' ``content`` reads
    actions and needs, the checklist's rows only; the needs view and the
    fresh rows are passed in, checked text-free."""
    needs = NeedsView.of(case)
    _no_text(needs)
    texts = _Poisoned(case, {"proposed_actions", "evidence_needs"})
    rows = _Poisoned(case, {"evidence"})
    checklist = ()
    if _post_diagnosis(case):
        outcome = narration_outcome(case)
        if fresh is None and not outcome.problem.verifying_row_ids:
            fresh = call_site._uncited_symptom_rows(case)
        _no_text(fresh)
        poisoned = _Poisoned(case, _IDENTITY, outcome)
        checklist = recheck_checklist(
            poisoned.progress.outcome,
            lambda item_id: content_by_id(rows, item_id, row=True),
            fresh,
        )
    poisoned = _Poisoned(case, _IDENTITY)
    return render_evidence_needs(
        CaseIdentity.of(poisoned),
        lambda item_id: content_by_id(texts, item_id),
        lambda: needs,
        checklist,
    )


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_the_renderer_reads_no_case_field(name):
    assert _render(await build(name)) == ORACLE[name]


async def test_the_fallback_is_the_fresh_rows():
    """No row verified the problem: the checklist's problem half is the
    fresh input, every symptom row (an engine-authored one included), and
    only that; verifying ids that name no row show none of it."""
    case = await build("E:fallback")
    assert narration_outcome(case).problem.verifying_row_ids == [], "premise"
    assert _render(case) == ORACLE["E:fallback"]
    assert _render(case, EvidenceNeedsFresh()) != ORACLE["E:fallback"]
    assert "(SYMPTOM," not in _render(case, EvidenceNeedsFresh())

    outcome = narration_outcome(case)
    with pytest.raises(ValueError, match="fresh symptom rows"):
        recheck_checklist(outcome, lambda item_id: None, None)

    named = await build("E:verifying_names_no_row")
    rows = call_site._uncited_symptom_rows(named)
    assert len(rows.uncited_symptom_row_ids) == 4, "premise: rows to fall back to"
    assert _render(named, rows) == ORACLE["E:verifying_names_no_row"]


# ---------------------------------------------------------------------------
# Failure: the checklist alone is lost; the needs view's failure propagates
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


def _rows_without_text(monkeypatch):
    real = call_site.content_by_id
    monkeypatch.setattr(
        call_site,
        "content_by_id",
        lambda case, item_id, row=False: (
            None if row else real(case, item_id, row=row)
        ),
    )


@pytest.mark.parametrize(
    "name, where",
    [
        ("D:omitted", "fold"),
        ("N:held_pending", "row_text"),
        ("E:fallback", "fresh"),
    ],
)
async def test_a_failure_omits_only_the_checklist(
    monkeypatch, failures, caplog, name, where
):
    """The record that does not fold, a row whose text does not read, or the
    fresh rows that raise: the checklist is its declared line, the need
    sections are ``main``'s, the failure is counted and logged."""
    case = await build(name)
    want = ORACLE[name]
    if where == "row_text":
        _rows_without_text(monkeypatch)
    if where == "fresh":
        monkeypatch.setattr(call_site, "_uncited_symptom_rows", _raise)
    with caplog.at_level("ERROR", logger=fold_module.__name__):
        block = _build_evidence_needs_block(case)
    assert block == want.replace(_checklist_section(want), CHECKLIST_OMITTED)
    assert failures == [WritePoint.NARRATION]
    logged = [
        r.getMessage() for r in caplog.records if "not rendered" in r.getMessage()
    ]
    assert len(logged) == 1
    assert "<evidence_needs> re-verification checklist was not rendered" in logged[0]


async def test_a_failed_fold_is_logged_by_each_block_it_omits(caplog, failures):
    """``D:omitted``: the fold fails once, and each block that renders an
    omission logs it once, naming itself, over the assembly's builds (the
    prompt-state lines included, #2811 S2b-1c-i); the failure is counted
    once."""
    case = await build("D:omitted")
    with caplog.at_level("ERROR", logger=fold_module.__name__):
        prompt = templates.get_prompt_for_case(
            case, "status", given_tools=DEFAULT_GIVEN
        )
    assert CHECKLIST_OMITTED in prompt
    logged = sorted(
        r.getMessage() for r in caplog.records if "not rendered" in r.getMessage()
    )
    assert logged == [
        f"Case {case.case_id}: the <evidence_needs> re-verification checklist "
        "was not rendered (#2811)",
        f"Case {case.case_id}: the <fix_verification> block was not rendered "
        "(#2811)",
        f"Case {case.case_id}: the exclusion rule was not rendered (#2811)",
        f"Case {case.case_id}: the focus emphasis was not rendered (#2811)",
        f"Case {case.case_id}: the milestone status was not rendered (#2811)",
    ]
    assert failures == [WritePoint.NARRATION]


async def test_the_omitted_line_prints_where_no_row_would_show(monkeypatch, failures):
    """The rows cannot be known, so the line prints even on a case with none."""
    case = await build("E:fallback")
    case.evidence = []
    assert _build_evidence_needs_block(case) == ""
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert _build_evidence_needs_block(case).splitlines()[-2:] == [
        CHECKLIST_OMITTED,
        "</evidence_needs>",
    ]


@pytest.mark.parametrize("name", ["E:diagnosis", "R:other_cause"])
async def test_a_needs_view_failure_propagates_from_the_prompt(monkeypatch, name):
    """As ``main``'s block read the pool unguarded: the turn fails, it does
    not render the need sections empty."""
    case = await build(name)
    monkeypatch.setattr(NeedsView, "of", _raise)
    with pytest.raises(RuntimeError, match="probe"):
        templates.get_prompt_for_case(case, "status", given_tools=DEFAULT_GIVEN)


def _masked(prompt: str) -> str:
    fence = re.search(r'fence="([0-9a-f]+)"', prompt)
    if fence:
        prompt = prompt.replace(fence.group(1), "FENCE")
    return re.sub(r"CURRENT_TIME: .*", "CURRENT_TIME:", prompt)


@pytest.mark.parametrize("name", ["inquiry", "terminal_qa"])
async def test_no_needs_view_outside_an_investigation(monkeypatch, name):
    """A guard: INQUIRY and terminal prompts never read the pool, so a needs
    view that would raise leaves them as they were."""
    from tests.unit.core.investigation.test_evidence_needs_context_block import (
        _make_case,
    )

    case = (
        _make_case(state=CaseState.INQUIRY) if name == "inquiry" else await build(name)
    )
    whole = templates.get_prompt_for_case(case, "status", given_tools=DEFAULT_GIVEN)
    monkeypatch.setattr(NeedsView, "of", _raise)
    prompt = templates.get_prompt_for_case(case, "status", given_tools=DEFAULT_GIVEN)
    assert _masked(prompt) == _masked(whole)


@pytest.mark.parametrize("name", ["E:diagnosis", "gate_one", "terminal_qa"])
async def test_no_fold_outside_post_diagnosis(monkeypatch, failures, name):
    """A guard (it holds on ``main``, which never folded here): in DIAGNOSIS
    and outside INVESTIGATING the record is never asked."""
    case = await build(name)
    assert not _post_diagnosis(case), "premise"
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert _build_evidence_needs_block(case) == ORACLE[name]
    assert failures == []


async def test_an_assembly_reads_the_needs_view_once():
    """Under a target the first cut overshoots, ``get_prompt_for_case``
    builds the context again; both blocks share one needs view."""
    case = await build("R:other_cause")
    for i in range(60):
        bound_row(
            case,
            EvidenceCategory.CAUSAL_EVIDENCE,
            turn=2,
            summary=f"pool log line {i}",
            extract="connection refused by pgbouncer " * 60,
        )
    real_of = NeedsView.of
    real_ctx = templates.build_investigation_context
    real_fold = outcome_module.compute_case_outcome
    rebuilt = False
    for target in range(20000, 40001, 500):
        counts = {"needs": 0, "ctx": 0, "fold": 0}

        def of(c):
            counts["needs"] += 1
            return real_of(c)

        def ctx(*args, **kwargs):
            counts["ctx"] += 1
            return real_ctx(*args, **kwargs)

        def fold(*args, **kwargs):
            counts["fold"] += 1
            return real_fold(*args, **kwargs)

        with (
            patch.object(NeedsView, "of", of),
            patch.object(templates, "build_investigation_context", ctx),
            patch.object(outcome_module, "compute_case_outcome", fold),
        ):
            prompt = templates.get_prompt_for_case(
                case, "status", given_tools=DEFAULT_GIVEN, target_tokens=target
            )
        if counts["ctx"] > 1 and "<evidence_needs>" in prompt:
            rebuilt = True
            assert (counts["needs"], counts["fold"]) == (1, 1), counts
    assert rebuilt, "premise: some target rebuilds the context"
