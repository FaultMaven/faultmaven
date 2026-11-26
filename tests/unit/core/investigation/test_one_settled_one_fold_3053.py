"""One ``settled`` rule, one hold set, and the reopen chip from one fold
(#3053 S3d-2, ruling Q1, Q2, Q3 and Q11 of 2026-11-25).

Q2: when a mitigation is settled is stated once (``mitigation_settled_of``,
``MitigationRecord.settled``); the stage, the operator list,
``cause_assurance.mitigation_settled`` and the monitor read it, and each says
what it said on ``main`` (``settled_oracle_3053.json``). Q3: the hold set is
``PROBLEM_ON_HOLD``. Q11: the reopen chip takes its verdict and every record
fact from one fold of the record (the content the ask names is read beside
it), its replies byte for byte ``main``'s (``reopen_chip_oracle_3053.json``),
and fails closed when the fold raises. Q1:
a verifying turn whose same-cycle mint fails still accepts cause work.

The Q2 and Q3 property tests fail on ``main`` only by ``ImportError`` (the
rule and the set do not exist there); their bite is the census rows
(``test_outcome_derivation_census_2615``) and the copies this stage deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.cause_assurance import mitigation_settled
from faultmaven.core.investigation.milestone_engine import (
    response_application,
    terminal_replies,
)
from faultmaven.core.investigation.milestone_engine import transition_turns as tt
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _register_disposition,
)
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.problem_status import problem_on_hold
from faultmaven.core.investigation.schemas import MilestoneUpdates
from faultmaven.core.investigation.terminal_transitions import (
    assess_resolution_readiness,
    cause_recheck_names,
)
from faultmaven.modules.case.contracts import (
    PROBLEM_ON_HOLD,
    ConfirmationGap,
    InvestigationStage,
    ProblemStatus,
    ResolutionReason,
    ResolutionVerdict,
    WritePoint,
)
from faultmaven.modules.case.domain.models.progress import (
    MitigationRecord,
    investigation_stage,
    mitigation_settled_of,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import reopen_chip_cases_3053 as chips
from tests.unit.core.investigation import settled_cases_3053 as settled
from tests.unit.core.investigation.test_outcome_statement_2811_s2c3a import (
    _process,
    _response,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _case,
    _revision_turn_with_cause_work,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _revision_engine,
)

pytestmark = pytest.mark.unit

_HERE = Path(__file__).parent
SETTLED_ORACLE = json.loads((_HERE / "settled_oracle_3053.json").read_text())
CHIP_ORACLE = json.loads((_HERE / "reopen_chip_oracle_3053.json").read_text())


# ---------------------------------------------------------------------------
# Q2: one settled rule
# ---------------------------------------------------------------------------


def test_the_settled_oracle_covers_the_corpus():
    assert set(SETTLED_ORACLE) == set(settled.CASES)


@pytest.mark.parametrize("name", sorted(settled.CASES))
def test_each_reader_of_settled_says_what_it_said_on_main(name):
    assert settled.render(settled.CASES[name]()) == SETTLED_ORACLE[name]


@pytest.mark.parametrize("name", sorted(settled.CASES))
def test_the_stage_is_the_rule_fed_the_folds_settled(name):
    """``current_stage`` equals the stage computed from a same-moment fold's
    ``mitigation.settled``; every reader of settled reads the one rule."""
    case = settled.CASES[name]()
    record = case.progress.mitigation
    fold = narration_outcome(case).mitigation
    progress = case.progress

    assert progress.current_stage is investigation_stage(
        mitigation_accepted=bool(record and record.accepted),
        mitigation_settled=bool(fold and fold.settled),
        solution_accepted=progress.solution_accepted,
        solution_verified=progress.solution_verified,
    )
    if record is not None:
        assert record.settled is mitigation_settled_of(
            verified=record.verified, disposed=record.disposition is not None
        )
        assert mitigation_settled(case) is record.settled
    if fold is not None:
        assert fold.settled is record.settled


@pytest.mark.parametrize("name", sorted(n for n in settled.CASES if "relapse" in n))
def test_a_bound_relapse_leaves_settled_and_the_stage_out_of_mitigation(name):
    """Q7: ``settled`` is forward-only; the relapse-adjusted reading is
    ``mitigation.verified``."""
    case = settled.CASES[name]()
    assert case.progress.mitigation.verified, "premise: the verify registered"
    fold = narration_outcome(case).mitigation
    assert fold.present_turn == 8, "premise: seen present after the relief"

    assert (fold.settled, fold.verified) == (True, False)
    assert case.progress.mitigation.settled is True
    assert case.progress.current_stage is not InvestigationStage.MITIGATION


def _disposing(verified: bool):
    case = settled.CASES[
        "verified:no_solution" if verified else "accepted:no_solution"
    ]()
    return case, {"milestones_completed": []}


@pytest.mark.parametrize("value", ["declared_stable", "proceed_unmitigated"])
def test_a_disposition_on_an_unsettled_mitigation_settles_it(value):
    case, metadata = _disposing(verified=False)
    assert _register_disposition(case, value, metadata) is True
    assert case.progress.mitigation.disposition == value
    assert metadata["milestones_completed"] == ["mitigation_disposed"]
    assert case.progress.current_stage is not InvestigationStage.MITIGATION


def test_a_declaration_on_a_verified_mitigation_settles_nothing():
    case, metadata = _disposing(verified=True)
    assert _register_disposition(case, "declared_stable", metadata) is False
    assert case.progress.mitigation.disposition == "declared_stable"
    assert metadata["milestones_completed"] == []


def test_the_disposition_writer_reads_the_one_rule(monkeypatch):
    """Whether a disposition settles is ``MitigationRecord.settled`` read
    before the write, not a copy of the rule."""
    case, metadata = _disposing(verified=False)
    monkeypatch.setattr(MitigationRecord, "settled", property(lambda _self: True))
    assert _register_disposition(case, "declared_stable", metadata) is False
    assert metadata["milestones_completed"] == []


# ---------------------------------------------------------------------------
# Q3: one hold set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(ProblemStatus))
def test_problem_on_hold_is_the_folds_status_in_the_hold_set(status):
    case = _case(status)
    assert problem_on_hold(case) is (
        narration_outcome(case).problem.status in PROBLEM_ON_HOLD
    )
    assert problem_on_hold(case) is (
        status in (ProblemStatus.REVISION_PENDING, ProblemStatus.INVALIDATED)
    )


# ---------------------------------------------------------------------------
# Q11: the reopen chip from one fold
# ---------------------------------------------------------------------------


def test_the_chip_oracle_covers_the_corpus():
    assert set(CHIP_ORACLE) == set(chips.CASES)


@pytest.mark.parametrize("name", sorted(chips.CASES))
def test_each_chip_reply_is_mains_byte_for_byte(name):
    """The confirmation, each refusal and the hold's reply, and their chips:
    no difference from ``main`` at babd5b944 is declared."""
    assert chips.render(name) == CHIP_ORACLE[name]


def test_the_corpus_reaches_every_reply_the_chip_decides():
    """Every reason the fold can refuse on is shown, READY opens, and a
    false alarm is reached only on hold."""
    reasons = set()
    for name in chips.CASES:
        case = chips.build(name)
        if problem_on_hold(case):
            assert name.startswith("hold:")
            continue
        resolution = narration_outcome(case).resolution
        assert (resolution.verdict == ResolutionVerdict.READY) is name.startswith(
            "ready:"
        )
        assert CHIP_ORACLE[name]["opened"] is name.startswith("ready:")
        reasons.add(resolution.reason)
    unreachable = {ResolutionReason.FALSE_ALARM}
    assert reasons == set(ResolutionReason) - unreachable
    assert golden._invalidated().progress.problem_status in PROBLEM_ON_HOLD


@pytest.fixture
def folds(monkeypatch) -> list:
    """Every fold of the record, at any write point."""
    calls: list = []
    real = outcome_module.compute_case_outcome

    def _compute(prior, case, *, at, **kw):
        calls.append(at)
        return real(prior, case, at=at, **kw)

    monkeypatch.setattr(outcome_module, "compute_case_outcome", _compute)
    return calls


@pytest.mark.parametrize("name", sorted(chips.CASES))
def test_the_chip_folds_once_per_reply(folds, name):
    """One fold gives the verdict and every record fact; none on hold."""
    case = chips.build(name)
    folds.clear()
    chips.chip(case)
    expected = [] if name.startswith("hold:") else [WritePoint.NARRATION]
    assert folds == expected


@pytest.fixture
def counted(monkeypatch) -> list:
    seen: list = []
    metric = SimpleNamespace(
        labels=lambda at: SimpleNamespace(inc=lambda: seen.append(at))
    )
    monkeypatch.setattr(terminal_replies, "case_outcome_write_failures_total", metric)
    return seen


@pytest.fixture
def readiness_calls(monkeypatch) -> list:
    calls: list = []
    for module in (terminal_transitions, outcome_module):
        real = module.assess_resolution_readiness
        monkeypatch.setattr(
            module,
            "assess_resolution_readiness",
            lambda c, real=real: calls.append(c) or real(c),
        )
    return calls


def _kept(case) -> list:
    projection = project_offers(case)
    if projection.reopen_chip is None:
        return []
    return [projection.reopen_chip, projection.close_chip]


@pytest.mark.parametrize(
    "name", sorted(n for n in chips.CASES if not n.startswith("hold:"))
)
def test_a_fold_that_raises_fails_closed(monkeypatch, counted, readiness_calls, name):
    """The ruled reply, the case's chips kept, nothing opened or recorded,
    counted once, and no fresh readiness read beside the fold."""
    case = chips.build(name)
    before = case.offer_record.model_dump()
    kept = _kept(case)

    def _raise(_case):
        raise RuntimeError("probe")

    monkeypatch.setattr(terminal_replies, "narration_outcome", _raise)
    reply = chips.chip(case)

    assert reply["agent_response"] == (
        "This case can't be marked resolved right now: the case record could "
        "not be read this turn."
    )
    assert reply["suggested_follow_ups"] == kept
    assert case.offer_record.model_dump() == before
    assert counted == [WritePoint.NARRATION]
    assert readiness_calls == []


def test_the_failed_reply_keeps_the_declined_cases_chips(monkeypatch, counted):
    """Positive control for the kept chips: a standing decline's pair."""
    case = chips.build("ready:declined")
    monkeypatch.setattr(terminal_replies, "narration_outcome", _raise)
    reply = chips.chip(case)
    assert len(reply["suggested_follow_ups"]) == 2
    assert reply["suggested_follow_ups"] == _kept(case)


@pytest.mark.parametrize("failure", ["fold", "held_check_text"])
def test_a_failed_reply_whose_chips_cannot_be_built_carries_none(
    monkeypatch, counted, failure
):
    """The guard is the fail-closed path's alone: the ruled reply stands
    without its chips."""
    if failure == "fold":
        case = chips.build("ready:declined")
        monkeypatch.setattr(terminal_replies, "narration_outcome", _raise)
    else:
        case = chips.build("refused:pending_check_declined")
        monkeypatch.setattr(tt, "content_by_id", lambda _case, _id: None)
    monkeypatch.setattr(tt, "project_offers", _raise)
    reply = chips.chip(case)
    assert reply["agent_response"] == tt.RECORD_UNREADABLE_LINE
    assert reply["suggested_follow_ups"] == []
    assert counted == [WritePoint.NARRATION]


def test_a_pending_check_refusal_lets_a_projection_failure_through(monkeypatch):
    """The normal refusal builds its chips as ``main`` did: a projection
    that raises propagates."""
    case = chips.build("refused:pending_check_declined")
    monkeypatch.setattr(tt, "project_offers", _raise)
    with pytest.raises(RuntimeError, match="probe"):
        chips.chip(case)


def test_a_held_check_with_no_text_fails_closed_counted_once(monkeypatch, counted):
    case = chips.build("refused:pending_check_declined")
    assert narration_outcome(case).resolution.reason == ResolutionReason.PENDING_CHECK
    kept = _kept(case)
    monkeypatch.setattr(tt, "content_by_id", lambda _case, _id: None)
    reply = chips.chip(case)
    assert reply["agent_response"] == tt.RECORD_UNREADABLE_LINE
    assert reply["suggested_follow_ups"] == kept
    assert case.offer_record.open is None
    assert counted == [WritePoint.NARRATION]


@pytest.mark.parametrize(
    "build",
    [
        golden._relief_only,
        golden._relief_unnamed,
        lambda: golden._relief_clipped_empty(".", "..."),
        lambda: golden._relief_clipped_empty(".", "raise max_connections to 100"),
        lambda: golden._relief_clipped_empty("max_connections is set to 20", "..."),
    ],
)
def test_the_cause_recheck_names_are_readinesss(build):
    """The content accessor names what readiness's ask names."""
    case = build()
    readiness = assess_resolution_readiness(case)
    assert ConfirmationGap.RELIEF_WITHOUT_CAUSE_RECHECK in readiness.confirmation_gaps
    assert cause_recheck_names(case) == readiness.params


def _raise(*_args, **_kwargs):
    raise RuntimeError("probe")


# ---------------------------------------------------------------------------
# Q1: a verifying turn whose same-cycle mint fails still accepts cause work
# ---------------------------------------------------------------------------


async def test_a_failed_pre_chain_mint_leaves_the_cause_work_accepted(monkeypatch):
    """The cause-work gates read the live problem status, an input, never
    the record: the mint before the chain (``WritePoint.PRE_CHAIN``) fails,
    and the turn that verifies the problem still accepts its hypothesis."""
    seen: list = []
    real = outcome_module.compute_case_outcome

    def _compute(prior, case, *, at, **kw):
        seen.append(at)
        if at == WritePoint.PRE_CHAIN:
            raise RuntimeError("probe")
        return real(prior, case, at=at, **kw)

    monkeypatch.setattr(outcome_module, "compute_case_outcome", _compute)
    work = _revision_turn_with_cause_work()
    response = _response(
        why={"symptom_verified": "timeouts in the gateway log"},
        milestones=MilestoneUpdates(symptom_verified=True),
        evidence_to_add=work.evidence_to_add,
        hypotheses_to_add=work.hypotheses_to_add,
        causal_nodes_to_add=work.causal_nodes_to_add,
        node_evidence_links=work.node_evidence_links,
    )
    case = _case(ProblemStatus.UNVERIFIED)

    with patch.object(
        response_application,
        "_mint_before_the_chain",
        wraps=response_application._mint_before_the_chain,
    ) as mint:
        await _process(_revision_engine(), case, "the log", response)

    assert mint.called and WritePoint.PRE_CHAIN in seen, "premise: the mint ran"
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert [h.statement for h in case.hypotheses.values()] == [
        work.hypotheses_to_add[0].statement
    ]
