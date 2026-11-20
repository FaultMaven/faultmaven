"""The working conclusion's contest NOTE and the causal graph block's
MECE-contested and why-open notes read the case outcome record (#2811
S2b-2a, #2615 stage S2): ``cause.contested_root_ids`` and
``cause.held_open_root_ids``, through the assembly's ``NarrationFold``.

On every fixture of the corpus (``contest_cases_2811``) both render byte for
byte what ``main`` rendered (``contest_oracle_2811.json``, captured through
``build_investigation_context`` and ``_build_causal_graph_block`` at
0c4b76c6a), but on the fixtures of the declared differences, where they
differ exactly as declared.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.cli.backfill_case_outcome import needs_record
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.causal_graph.clusters import (
    mece_contested_root_ids,
)
from faultmaven.core.investigation.causal_graph.projection import held_open_root_ids
from faultmaven.core.investigation.causal_graph.support import BLOCK_REASON_COUNT
from faultmaven.core.investigation.narration.needs import NeedsView
from faultmaven.core.investigation.outcome import (
    SHAPE_STEPS,
    compute_case_outcome,
    narration_outcome,
)
from faultmaven.core.investigation.prompts.context_builder import (
    causal_graph_block as cgb,
)
from faultmaven.core.investigation.prompts.context_builder import (
    narration_fold as fold_module,
)
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    CONTEST_OMITTED,
)
from faultmaven.modules.case.contracts import (
    CURRENT_OUTCOME_SHAPE,
    CaseOutcome,
    WritePoint,
)
from tests.unit.core.investigation.prompts.contest_cases_2811 import (
    CASES,
    DECLARED,
    build,
    graph,
    render,
)
from tests.unit.core.investigation.prompts.test_prompt_state_record_2811 import (
    _poisoned_fold,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "contest_oracle_2811.json").read_text(encoding="utf-8")
)

_NOTE = "NOTE: this is ONE of several simultaneously-validated"
_CONTEST = cgb._RECOVERY_NOTES[cgb._MECE_CONTESTED]
_WHY = cgb._RECOVERY_NOTES[cgb._WHY_OPEN]
_REFUSED = cgb._RECOVERY_NOTES[cgb._VERDICT_REFUSED]


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(set(CASES) - set(DECLARED.values())))
async def test_each_place_renders_as_main_did(name):
    case = await build(name)
    assert render(case) == ORACLE[name]
    assert graph(case) == ORACLE[name]["graph"]


async def test_a_retire_that_released_the_contest_drops_the_stale_note():
    """``C:retired``: the stored flag still says contested, the standing set
    contests nothing; ``main``'s NOTE contradicted the graph beside it."""
    case = await build(DECLARED["retired"])
    want = ORACLE[DECLARED["retired"]]
    assert case.progress.cause_identification_contested is True, "premise"
    assert mece_contested_root_ids(case) == set(), "premise"
    assert _NOTE in want["conclusion"] and _CONTEST not in want["graph"], "premise"
    note = next(ln for ln in want["conclusion"].splitlines() if ln.startswith("NOTE"))
    assert render(case) == {
        "conclusion": want["conclusion"].replace(note + "\n", ""),
        "graph": want["graph"],
    }


async def test_an_unreadable_record_omits_the_recovery_notes_as_declared():
    """``D:omitted``: the graph block's contest notes give way to the one
    declared line, under the block's header."""
    case = await build(DECLARED["omitted"])
    want = ORACLE[DECLARED["omitted"]]
    assert want["graph"].count(_CONTEST) == 2, "premise"
    lines = want["graph"].replace(_CONTEST, "").splitlines()
    lines.insert(2, cgb.RECOVERY_OMITTED)
    assert render(case) == {"conclusion": "", "graph": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Pin 2: the record wins over the live derivation
# ---------------------------------------------------------------------------


def _told(case, *, contested, held_open):
    """The assembly's fold over ``case`` (``_poisoned_fold``: nothing past
    it reads the case for the record), its record saying ``contested`` and
    ``held_open`` whatever the case derives."""
    fold, _ = _poisoned_fold(case)
    fold._needs = NeedsView.of(case)  # the assembly's other blocks read it
    cause = fold._outcome.cause.model_copy(
        update={"contested_root_ids": contested, "held_open_root_ids": held_open}
    )
    fold._outcome = fold._outcome.model_copy(update={"cause": cause})
    return fold


async def test_a_record_that_contests_nothing_renders_no_contest():
    """The case derives three contested roots, none held open; the record
    says none contested and one held open."""
    case = await build("C:contested")
    roots = sorted(mece_contested_root_ids(case))
    assert len(roots) == 3 and held_open_root_ids(case) == set(), "premise"
    assert _NOTE in ORACLE["C:contested"]["conclusion"], "premise: main's NOTE"
    got = render(case, _told(case, contested=[], held_open=roots[:1]))
    assert _NOTE not in got["conclusion"]
    assert _CONTEST not in got["graph"]
    assert got["graph"].count(_WHY) == 1
    assert next(ln for ln in got["graph"].splitlines() if roots[0] in ln).endswith(_WHY)


async def test_a_record_that_contests_renders_the_contest():
    """The reverse: the case derives one root held open and no contest; the
    record says it is contested and not held open."""
    case = await build("C:held_open")
    (root,) = held_open_root_ids(case)
    assert mece_contested_root_ids(case) == set(), "premise"
    assert _NOTE not in ORACLE["C:held_open"]["conclusion"], "premise"
    got = render(case, _told(case, contested=[root], held_open=[]))
    assert _NOTE in got["conclusion"]
    assert got["graph"].count(_CONTEST) == 1
    assert _WHY not in got["graph"]


async def test_the_refused_population_excludes_the_records_held_open_roots():
    """``refused_verdict_root_ids(case) - why`` takes ``why`` off the record:
    a refused root the record does not hold open shows the refusal's ask."""
    case = await build("C:refused_held_open")
    (root,) = held_open_root_ids(case)
    assert _REFUSED not in ORACLE["C:refused_held_open"]["graph"], "premise"
    got = render(case, _told(case, contested=[], held_open=[]))
    assert next(ln for ln in got["graph"].splitlines() if root in ln).endswith(_REFUSED)


# ---------------------------------------------------------------------------
# Fold failure: the declared lines, counted once, never raised
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


@pytest.mark.parametrize("name", ["C:all_three", "C:refused_count_held"])
async def test_a_failed_fold_renders_each_blocks_declared_line(
    monkeypatch, failures, caplog, name
):
    """The NOTE gives way to ``CONTEST_OMITTED``; the graph block renders
    ``RECOVERY_OMITTED`` once and no contested, why-open or refused note,
    while the grounding notes still render. Each block logs once; the
    failure is counted once per assembly."""
    case = await build(name)
    want = ORACLE[name]
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    with caplog.at_level(logging.ERROR, logger=fold_module.__name__):
        got = render(case, fold_module.NarrationFold(case))
    lines = [ln for ln in want["conclusion"].splitlines() if not ln.startswith(_NOTE)]
    lines.insert(-1, CONTEST_OMITTED)
    assert got["conclusion"] == "\n".join(lines)
    assert got["graph"].count(cgb.RECOVERY_OMITTED) == 1
    assert not any(n in got["graph"] for n in (_CONTEST, _WHY, _REFUSED))
    if name == "C:refused_count_held":
        assert _REFUSED in want["graph"], "premise"
        assert cgb._RECOVERY_NOTES[BLOCK_REASON_COUNT] in got["graph"]
    logged = [
        r.getMessage() for r in caplog.records if "not rendered" in r.getMessage()
    ]
    for block in (cgb._RECOVERY_BLOCK, "working conclusion's contest note"):
        assert sum(block in m for m in logged) == 1, block
    assert failures == [WritePoint.NARRATION]


async def test_the_notes_without_a_conclusion_or_a_graph_never_fold(
    monkeypatch, failures
):
    """The fold is asked only past the block's empty return, and the NOTE's
    only with a working conclusion."""
    case = await build("P:inquiry")
    assert case.working_conclusion is None and not case.causal_nodes, "premise"
    monkeypatch.setattr(fold_module, "narration_outcome", _raise)
    assert graph(case) == ""
    assert render(case, None)["conclusion"] == ""
    assert failures == []


# ---------------------------------------------------------------------------
# Shape 5: the fold writes the field; a shape-4 record upgrades
# ---------------------------------------------------------------------------


def _shape_4(record: CaseOutcome) -> CaseOutcome:
    """``record`` as a shape-4 writer stored it, without ``contested_root_ids``."""
    raw = record.model_dump(mode="json")
    raw["shape"] = 4
    raw["cause"].pop("contested_root_ids")
    return CaseOutcome.model_validate(raw)


async def test_the_fold_writes_the_contested_roots_sorted():
    case = await build("C:all_three")
    contested = mece_contested_root_ids(case)
    assert len(contested) == 3, "premise"
    record = narration_outcome(case)
    assert record.cause.contested_root_ids == sorted(contested)
    assert record.cause.held_open_root_ids == sorted(held_open_root_ids(case))


async def test_a_shape_4_record_upgrades_to_5_through_the_identity(monkeypatch):
    case = await build("C:all_three")
    old = _shape_4(compute_case_outcome(None, case, at=WritePoint.CYCLE))
    assert CURRENT_OUTCOME_SHAPE == 5
    assert old.cause.contested_root_ids == [], "premise: loads with the default"
    assert outcome_module._shape_5(old, case, None, []) is old
    case.progress.outcome = old
    assert needs_record(case), "the backfill rewrites it"
    folded = compute_case_outcome(old, case, at=WritePoint.COMMIT)
    assert folded.shape == 5
    assert folded.cause.contested_root_ids == sorted(mece_contested_root_ids(case))
    case.progress.outcome = folded
    assert not needs_record(case)

    monkeypatch.delitem(SHAPE_STEPS, 5)
    with pytest.raises(KeyError):
        compute_case_outcome(old, case, at=WritePoint.COMMIT)
