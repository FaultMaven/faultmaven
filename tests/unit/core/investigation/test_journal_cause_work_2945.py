"""A journal entry that names a hypothesis is cause work (#2945).

The journal outlives the turn, so an entry must not record cause work that
never stood. While a revised statement awaits re-confirmation, an entry naming
a hypothesis is staged with the turn's other cause work: replayed when the user
confirms, dropped with it when they decline. An entry naming a hypothesis the
case does not hold after the apply (one the gate refused) is dropped. A
same-turn ref (``new_index_N``, or a bare ``N``) is resolved through the apply's
emit order and stored as the id it names. Entries naming no hypothesis apply as
before. No live LLM.
"""

import pytest

from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.problem_status import decline_revision
from faultmaven.core.investigation.schemas import HypothesisToAdd, JournalEntryOutput
from faultmaven.modules.case.contracts import (
    AnswerVia,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    ProblemStatus,
)
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)

pytestmark = pytest.mark.unit

BLOCKER = "no shell on the database host"
FINDING = "the pool is capped at 5"


async def _staged():
    """A revision turn (4) whose journal names a hypothesis on the case, and
    carries a blocker naming none."""
    engine, case = revision._engine(), revision._case()
    standing = Hypothesis(
        statement="the pool is too small",
        category=HypothesisCategory.CONFIG,
        generated_at_turn=3,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="pool errors",
    )
    case.hypotheses[standing.hypothesis_id] = standing
    dsu = revision._revision_turn_with_cause_work()
    dsu.journal_entries = [
        JournalEntryOutput(
            entry_type="finding", content=FINDING, hypothesis_id=standing.hypothesis_id
        ),
        JournalEntryOutput(entry_type="blocker", content=BLOCKER),
    ]
    await revision._apply(engine, case, dsu)
    return engine, case


def _journal(case):
    return [(e.content, e.turn) for e in case.investigation_journal]


async def test_an_entry_naming_a_hypothesis_is_staged_and_replayed_on_confirm():
    engine, case = await _staged()
    assert _journal(case) == [(BLOCKER, 4)], "held with the staged cause work"

    case.current_turn = 5
    await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)

    # Dated at the turn it was written, like the cause work replayed with it.
    assert _journal(case) == [(BLOCKER, 4), (FINDING, 4)]


async def test_an_entry_naming_a_hypothesis_is_dropped_on_decline():
    _engine, case = await _staged()

    case.current_turn = 5
    decline_revision(case, via=AnswerVia.CLICK)

    assert _journal(case) == [(BLOCKER, 4)]


def _turn(hypothesis_id: str):
    """One turn that adds a hypothesis and journals a finding naming
    ``hypothesis_id``, beside a blocker naming none."""
    return revision._DSU(
        hypotheses_to_add=[
            HypothesisToAdd(
                statement="the pool is too small",
                category=HypothesisCategory.CONFIG,
                likelihood=0.6,
                rationale="pool errors",
            )
        ],
        journal_entries=[
            JournalEntryOutput(
                entry_type="finding", content=FINDING, hypothesis_id=hypothesis_id
            ),
            JournalEntryOutput(entry_type="blocker", content=BLOCKER),
        ],
    )


@pytest.mark.parametrize("ref", ["new_index_0", "0", " [new_index_0] "])
async def test_a_same_turn_ref_is_kept_as_the_minted_id(ref):
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    await revision._apply(engine, case, _turn(ref))

    (minted,) = case.hypotheses
    finding = next(e for e in case.investigation_journal if e.content == FINDING)
    assert finding.hypothesis_id == minted


@pytest.mark.parametrize("ref", ["hyp_0000000000", "new_index_0"])
async def test_an_entry_naming_a_hypothesis_the_gate_refused_is_dropped(ref):
    """On an unverified problem the hypothesis is refused: its slot resolves to
    nothing, and an id the case does not hold names nothing either. The
    blocker stands."""
    engine, case = revision._engine(), revision._case()
    await revision._apply(engine, case, _turn(ref))

    assert case.hypotheses == {}, "premise: the gate refused the hypothesis"
    assert _journal(case) == [(BLOCKER, 4)]


async def test_a_staged_ref_to_the_bundles_own_hypothesis_is_kept_on_confirm():
    """The staged bundle creates the hypothesis its entry names by
    ``new_index_0``: the replay resolves it through its own emit order."""
    engine, case = revision._engine(), revision._case()
    dsu = revision._revision_turn_with_cause_work()
    dsu.journal_entries = [
        JournalEntryOutput(
            entry_type="finding", content=FINDING, hypothesis_id="new_index_0"
        )
    ]
    await revision._apply(engine, case, dsu)
    assert case.investigation_journal == [], "premise: held with the cause work"

    case.current_turn = 5
    await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)

    (replayed,) = case.hypotheses
    assert [
        (e.content, e.hypothesis_id, e.turn) for e in case.investigation_journal
    ] == [(FINDING, replayed, 4)]
