"""The cycle's offers and the met request's confirmation render the cycle's
record (#3053 S3d-3, #2615 stage S3).

The spec: the ruling on Q1-Q11 of 2026-11-25 (Q1, Q4, Q5, Q6), the ruling of
2026-11-26 (the four sent-back renders keep their decision's text, counted;
the MECE contest's one derivation is the record's) and the owner's in-session
answers of 2026-11-26 (ruled case 3 by each offer's own fields; the three
fixed-text templates left as they are, listed).

- Q5: every offer renders the record and the content read at render
  (``offers.narrated_params``), per decision; a disagreement is counted; the
  four ruled cases (``offers.RULED_CASES``) render the decision, and each is a
  disagreement, so each is counted.
- Q1: the met request's confirmation in the reply reads the cycle's stored
  record, else the statement's fold (counted once), else its decision; no
  decider folds, so a turn whose folds all raise counts at most the prompt's
  fold and the statement's.
- MECE: the record's IDENTIFICATION_CONTESTED verdict and its contested roots
  are one computation; the deciders' leg keeps the recompute's flag.
- Q6: the summary gate's offer and enforcement halves are one predicate, and
  the record's shape does not move.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.causal_graph.clusters import (
    mece_contested_root_ids,
)
from faultmaven.core.investigation.cause_assurance import cause_pool_empty_reason
from faultmaven.core.investigation.milestone_engine import (
    turn_application,
    turn_completion,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _closed_suggestions,
    resolution_content,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _ready_confirmation,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
    _disagreements,
)
from faultmaven.core.investigation.narration.codes import (
    DECIDED_FROM,
    DECISIONS_KEY,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.fix_verification import content_by_id
from faultmaven.core.investigation.narration.offers import (
    RULED_CASES,
    confirmation_message,
    narrated_params,
    record_params,
    render_offer,
)
from faultmaven.core.investigation.narration.replies import NO_ROOT_ON_RECORD
from faultmaven.core.investigation.narration.statement import offer_stated
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    narration_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.terminal_transitions import (
    terminal_summary_skip_reason,
)
from faultmaven.modules.case.contracts import (
    CURRENT_OUTCOME_SHAPE,
    CaseState,
    ClosureEligibility,
    PendingCheckReason,
    PoolEmptyReason,
    ProblemStatus,
    ResolutionReason,
    WritePoint,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import reply_cases_2811 as replies
from tests.unit.core.investigation.golden_cases_2636 import _DSU
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _bare_case,
    _rootless,
)
from tests.unit.core.investigation.test_record_prose_2811_s2b2c import SWITCHED

pytestmark = pytest.mark.unit

REPLY_ORACLE = json.loads(
    (Path(__file__).parent / "reply_oracle_2811.json").read_text()
)


def _text(case):
    return lambda item_id: content_by_id(case, item_id)


def _edited(record, section: str, **fields):
    setattr(record, section, getattr(record, section).model_copy(update=fields))


# ---------------------------------------------------------------------------
# Q5: every (code, composer) and every value of each field it was decided
# from
# ---------------------------------------------------------------------------

#: Each offer key's scenario (the closure proposal's on both verdict kinds,
#: the unmet request's close with and without ``suggest_close``).
SCENARIOS = {
    (OfferCode.RCA_INFEASIBLE_CLOSE, None): ["rca_infeasible_close"],
    (OfferCode.DEFERRED_RESOLVE, None): ["deferred_resolve"],
    (OfferCode.DEFERRED_CLOSE, None): ["deferred_close"],
    (OfferCode.BACKSTOP_RESOLVE, None): ["backstop_resolve"],
    (OfferCode.FALSE_ALARM_CLOSE, None): ["false_alarm_close"],
    (OfferCode.NEEDS_INFO_UNMET_CLOSE, None): [
        "needs_info_unmet_close",
        "unmet_close_no_suggestion",
    ],
    (OfferCode.RESOLUTION_NEEDS_INFO, None): ["resolution_needs_info"],
    (OfferCode.REQUEST_READY, None): ["request_ready"],
    (OfferCode.MODEL_PROPOSAL, "readiness"): ["model_proposal_readiness"],
    (OfferCode.MODEL_PROPOSAL, "confirmation"): ["model_proposal_confirmation"],
    (OfferCode.MODEL_PROPOSAL, "closure"): [
        "model_proposal_closure",
        "model_proposal_closure_pivot",
    ],
}

BUILDS = {
    **SWITCHED,
    "unmet_close_no_suggestion": lambda: golden._requested(
        golden._identified_case()[0]
    ),
    "model_proposal_closure_pivot": lambda: golden._models_proposal(
        golden._confirmed_case(), "closed", "close it"
    ),
}

#: The templates that take no record fact from their params (owner,
#: 2026-11-26: left as they are, listed): their text is one constant, or the
#: decider's rationale, whatever the record holds.
FIXED_TEXT = {
    "unmet_close_no_suggestion": "the unmet request's close without a close "
    "suggestion prints a constant",
    "false_alarm_close": "the false-alarm close names the decider's rationale "
    "and never reads problem_status",
    "deferred_close": "the deferred close prints a constant",
}


def _readiness_facts(r):
    return {
        "reason": r.resolution.reason,
        "missing": list(r.resolution.missing),
        "gaps": list(r.resolution.confirmation_gaps),
        "off_cause_turn": r.resolution.off_cause_turn,
        "cause": r.cause,
    }


def _closure_facts(r, params):
    leg = r.cause.identification_leg
    facts = {"identification_leg": leg, "root_named": leg is not None}
    if params["verdict"] == ClosureEligibility.SUGGEST_RESOLVE:
        facts["record_lines"] = r.resolution.statement
    else:
        facts["milestones_completed"] = len(r.milestones_shown)
    return facts


#: The record facts each template takes from its params, as the record holds
#: them.
RECORD_FACTS = {
    (OfferCode.RCA_INFEASIBLE_CLOSE, None): lambda r, p: {
        "closure_line": r.closure.statement
    },
    (OfferCode.DEFERRED_RESOLVE, None): lambda r, p: {
        "record_lines": r.resolution.statement,
        "identification_leg": r.cause.identification_leg,
    },
    (OfferCode.BACKSTOP_RESOLVE, None): lambda r, p: {
        "record_lines": r.resolution.statement,
        "identification_leg": r.cause.identification_leg,
    },
    (OfferCode.NEEDS_INFO_UNMET_CLOSE, None): lambda r, p: _readiness_facts(r),
    (OfferCode.RESOLUTION_NEEDS_INFO, None): lambda r, p: _readiness_facts(r),
    (OfferCode.MODEL_PROPOSAL, "readiness"): lambda r, p: _readiness_facts(r),
    (OfferCode.REQUEST_READY, None): lambda r, p: {
        "record_lines": r.resolution.statement,
        "identification_leg": r.cause.identification_leg,
        "root_named": r.cause.identification_leg is not None,
    },
    (OfferCode.MODEL_PROPOSAL, "confirmation"): lambda r, p: {
        "record_lines": r.resolution.statement,
        "identification_leg": r.cause.identification_leg,
        "root_named": r.cause.identification_leg is not None,
    },
    (OfferCode.MODEL_PROPOSAL, "closure"): _closure_facts,
}


def _stated(params: dict) -> dict:
    """``params``' facts as ``RECORD_FACTS`` names them."""
    out = dict(params)
    if "root" in params:
        out["root_named"] = params["root"] != NO_ROOT_ON_RECORD
    if "root_cause" in params:
        out["root_named"] = params["root_cause"] is not None
    return out


_LEGS = (None, "chain", "rcc", "working_conclusion")

#: Each field's every value, with what a coherent record holds beside it.
_VALUES = {
    "resolution.reason": [
        (reason, extra)
        for reason, extra in [
            (ResolutionReason.FALSE_ALARM, {}),
            (ResolutionReason.NOTHING_ON_RECORD, {}),
            (ResolutionReason.FIX_FAILED, {}),
            (ResolutionReason.FIX_OFF_CAUSE, {"off_cause_turn": 4}),
            (ResolutionReason.DOCUMENTATION_GAPS, {"missing": ["root cause"]}),
            (
                ResolutionReason.PENDING_CHECK,
                {
                    "pending_check_need_id": "eneed_0000000000ff",
                    "pending_check_reason": PendingCheckReason.UNANSWERED,
                },
            ),
            (ResolutionReason.CONFIRMED, {"statement": "- **The cause** — x"}),
        ]
    ],
    "closure.eligibility": [(v, {}) for v in ClosureEligibility],
    "cause.identification_leg": [(v, {}) for v in _LEGS],
    "problem.status": [(v, {}) for v in ProblemStatus],
}

_POINTS = [
    (key, name, field, value, extra)
    for key, names in SCENARIOS.items()
    for name in names
    for field in DECIDED_FROM[key]
    for value, extra in _VALUES[field]
]


@pytest.fixture(scope="module")
def stored():
    """Each scenario's cycle, its record written, built once."""
    out = {}
    for name in {n for names in SCENARIOS.values() for n in names}:
        case, metadata = BUILDS[name]()
        record_case_outcome(case, at=WritePoint.CYCLE)
        (entry,) = [e for e in metadata[DECISIONS_KEY] if e["code"] in set(OfferCode)]
        out[name] = (case, entry, resolution_content(case))
    return out


def _render(code, params):
    return render_offer(code, params)


@pytest.mark.parametrize(
    ("key", "name", "field", "value", "extra"),
    _POINTS,
    ids=[f"{n}-{f}-{getattr(v, 'value', v)}" for _k, n, f, v, _e in _POINTS],
)
def test_every_decided_value_renders_the_record_or_a_ruled_decision(
    stored, key, name, field, value, extra
):
    """Ruling Q5: the offer renders over every value of every field it was
    decided from. Outside the ruled cases it states the record's fact for
    every fact its template takes from params; in one, the decision's own
    params, and the decision disagrees with the record, so it is counted."""
    case, entry, content = stored[name]
    code, params = entry["code"], entry["params"]
    record = case.progress.outcome.model_copy(deep=True)
    section, attr = field.split(".")
    _edited(record, section, **{attr: value})
    if extra:
        _edited(record, section, **extra)
    request_text = None
    narrated = narrated_params(code, params, record, request_text, content, _text(case))
    text = _render(code, narrated)
    offer_stated(code, narrated, record.attempts)
    if any(ruled(code, params, record, request_text) for ruled in RULED_CASES):
        assert narrated == narrated_params(
            code, params, None, None, content, _text(case)
        )
        assert _disagreements(code, params, record)
        return
    assert text or key == (OfferCode.NEEDS_INFO_UNMET_CLOSE, None)
    if name in FIXED_TEXT:
        assert text == _render(
            code, narrated_params(code, params, None, None, content, _text(case))
        )
        return
    facts = RECORD_FACTS[key](record, params)
    assert {k: _stated(narrated)[k] for k in facts} == facts


def test_the_matrix_reaches_each_ruled_case_and_a_record_render(stored):
    """A positive control: the matrix covers every offer key, renders the
    record somewhere, and meets each of the four ruled cases."""
    reached = set()
    for _key, name, field, value, extra in _POINTS:
        case, entry, _content = stored[name]
        record = case.progress.outcome.model_copy(deep=True)
        section, attr = field.split(".")
        _edited(record, section, **{attr: value, **extra})
        held = [
            ruled.__name__
            for ruled in RULED_CASES
            if ruled(entry["code"], entry["params"], record, None)
        ]
        reached.add(held[0] if held else "record")
    assert reached == {"record", *(ruled.__name__ for ruled in RULED_CASES)}
    assert {key for key, *_ in _POINTS} == set(DECIDED_FROM) - {
        (NoticeCode.PENDING_CHECK, None)
    }


# ---------------------------------------------------------------------------
# Each ruled case is a disagreement, so each is counted (critique point 3)
# ---------------------------------------------------------------------------

#: What each decider can log for the fields it decides on.
_NOT_READY = [r for r in ResolutionReason if r != ResolutionReason.CONFIRMED]
DECIDED = {
    "readiness": [
        {
            "reason": r,
            "request_text": (
                "the check" if r == ResolutionReason.PENDING_CHECK else None
            ),
        }
        for r in _NOT_READY
    ],
    "resolve": [
        {"reason": ResolutionReason.CONFIRMED, "identification_leg": leg}
        for leg in _LEGS
    ],
    "deferred": [
        {"verdict": ClosureEligibility.SUGGEST_RESOLVE, "identification_leg": leg}
        for leg in _LEGS
    ],
    "closure": [
        {"verdict": v, "identification_leg": leg}
        for v in ClosureEligibility
        for leg in _LEGS
    ],
}
_KINDS = {
    (OfferCode.NEEDS_INFO_UNMET_CLOSE, None): ("readiness", {"suggest_close": True}),
    (OfferCode.RESOLUTION_NEEDS_INFO, None): ("readiness", {}),
    (OfferCode.MODEL_PROPOSAL, "readiness"): ("readiness", {}),
    (OfferCode.BACKSTOP_RESOLVE, None): ("resolve", {}),
    (OfferCode.REQUEST_READY, None): ("resolve", {}),
    (OfferCode.MODEL_PROPOSAL, "confirmation"): ("resolve", {}),
    (OfferCode.DEFERRED_RESOLVE, None): ("deferred", {}),
    (OfferCode.RCA_INFEASIBLE_CLOSE, None): ("closure", {}),
    (OfferCode.DEFERRED_CLOSE, None): ("closure", {}),
    (OfferCode.MODEL_PROPOSAL, "closure"): ("closure", {}),
}


def test_every_offer_key_has_a_decider_domain():
    assert set(_KINDS) | {(OfferCode.FALSE_ALARM_CLOSE, None)} == set(DECIDED_FROM) - {
        (NoticeCode.PENDING_CHECK, None)
    }


@pytest.mark.parametrize("key", sorted(_KINDS, key=str))
def test_a_ruled_case_always_disagrees_with_the_record(key):
    """For every ``(code, composer)``, every value its decider can log and
    every record: a ruled case that holds implies a disagreement on a field
    the decision was decided from, so every ruled case is counted."""
    code, composer = key
    kind, fixed = _KINDS[key]
    base = narration_outcome(golden._confirmed_case())
    held = 0
    for decided in DECIDED[kind]:
        params = {"composer": composer, **fixed, **decided}
        for reason in ResolutionReason:
            for eligibility in ClosureEligibility:
                for leg in (None, "chain"):
                    for request_text in (None, "the record's check"):
                        record = base.model_copy(deep=True)
                        _edited(record, "resolution", reason=reason)
                        _edited(record, "closure", eligibility=eligibility)
                        _edited(record, "cause", identification_leg=leg)
                        if any(
                            ruled(code, params, record, request_text)
                            for ruled in RULED_CASES
                        ):
                            held += 1
                            assert _disagreements(code, params, record), (
                                params,
                                reason,
                                eligibility,
                                leg,
                            )
    assert held, "premise: a ruled case holds somewhere in the domain"


# ---------------------------------------------------------------------------
# The reply: the met request's confirmation in Q1's order
# ---------------------------------------------------------------------------


def _request_ready():
    """Step 0 turned READY on a remembered request, its record stored."""
    case, metadata = golden._requested(golden._confirmed_case())
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case, at=WritePoint.CYCLE)
    assert metadata["resolution_ready_for_confirmation"], "premise"
    (entry,) = [
        e for e in metadata[DECISIONS_KEY] if e["code"] == OfferCode.REQUEST_READY
    ]
    return case, metadata, entry["params"]


def test_the_reply_states_the_cycles_stored_record():
    """The cycle's record, read as written, and the content read at reply
    time: the reopen chip's rendering of the same record."""
    case, metadata, _params = _request_ready()
    record = case.progress.outcome
    _edited(record, "resolution", statement="- the stored record's line")
    with patch.object(outcome_module, "compute_case_outcome") as fold:
        params = _ready_confirmation(case, metadata, None)
    fold.assert_not_called()
    assert params["record_lines"] == "- the stored record's line"
    assert "- the stored record's line" in confirmation_message(params)


def test_a_failed_write_reads_the_statements_fold_and_else_the_decision():
    """Q1's order: the statement's fold when the cycle's write failed, and
    the decision's leg and record lines when that fold failed too."""
    case, metadata, params = _request_ready()
    metadata[OUTCOME_WRITTEN_KEY] = False
    folded = case.progress.outcome.model_copy(deep=True)
    _edited(folded, "resolution", statement="- the fold's line")
    assert _ready_confirmation(case, metadata, folded)["record_lines"] == (
        "- the fold's line"
    )
    _edited(case.progress.outcome, "resolution", statement="- a stale line")
    decided = _ready_confirmation(case, metadata, None)
    assert decided["record_lines"] == params["record_lines"]
    assert decided["identification_leg"] == params["identification_leg"]
    assert decided["solution"] == resolution_content(case).solution


def test_a_record_not_confirmed_leaves_the_reply_the_decisions():
    """Ruled case 3: the stored record reads another reason (its statement
    empty); the reply's confirmation is the decision's, as the offer's
    summary is."""
    case, metadata, params = _request_ready()
    _edited(
        case.progress.outcome,
        "resolution",
        reason=ResolutionReason.DOCUMENTATION_GAPS,
        statement="",
    )
    reply = _ready_confirmation(case, metadata, None)
    assert reply["record_lines"] == params["record_lines"] != ""
    assert reply == narrated_params(
        OfferCode.REQUEST_READY,
        params,
        None,
        None,
        resolution_content(case),
        _text(case),
    )


@pytest.mark.parametrize(
    "name", sorted(n for n in replies.CASES if not n.startswith("T:"))
)
async def test_the_record_rendered_confirmation_is_the_oracles(name):
    """Parity (critique 9 (b)): for each non-terminal fixture, the
    confirmation the narration renders from the record and the content read
    at render is ``main``'s (``reply_oracle_2811.json``)."""
    case = await replies.build(name)
    with replies.frozen():
        params = record_params(
            OfferCode.REQUEST_READY,
            {"reason": ResolutionReason.CONFIRMED},
            narration_outcome(case),
            None,
            resolution_content(case),
            _text(case),
        )
        assert confirmation_message(params) == REPLY_ORACLE[name]["confirmation"]


# ---------------------------------------------------------------------------
# Q1: a turn whose folds all raise
# ---------------------------------------------------------------------------

_COUNTING = (
    "faultmaven.core.investigation.milestone_engine.terminal_replies",
    "faultmaven.core.investigation.milestone_engine.turn_completion",
    "faultmaven.core.investigation.prompts.context_builder.narration_fold",
    "faultmaven.core.investigation.outcome",
)


@pytest.fixture
def narration_counts(monkeypatch) -> list:
    """``case_outcome_write_failures_total``'s increments, by ``at``, in every
    module that counts it."""
    seen: list = []
    metric = SimpleNamespace(
        labels=lambda at: SimpleNamespace(inc=lambda: seen.append(at))
    )
    for name in _COUNTING:
        monkeypatch.setattr(
            importlib.import_module(name), "case_outcome_write_failures_total", metric
        )
    return seen


def _raise(*_args, **_kwargs):
    raise RuntimeError("probe")


def _ready_turn(monkeypatch, *, baseline: bool, prompt_fold: bool) -> dict:
    """The met request's whole turn, every fold raising; the reply's
    confirmation params captured. ``prompt_fold`` False: the prompt folds
    nothing, so only the reply path counts."""
    case = golden._wc_leg_case(confirmed=True)
    case.offer_record = remember_resolve_request(case, ["the check"])
    if baseline:
        record_case_outcome(case, at=WritePoint.CYCLE)
    else:
        case.progress.outcome = None
    captured: list = []
    real = turn_completion.narrated_params

    def _capture(code, params, outcome, *args):
        captured.append(outcome)
        captured.append(real(code, params, outcome, *args))
        return captured[-1]

    monkeypatch.setattr(turn_completion, "narrated_params", _capture)
    monkeypatch.setattr(outcome_module, "compute_case_outcome", _raise)
    if not prompt_fold:
        monkeypatch.setattr(NarrationFold, "outcome", lambda self, _reader: None)
    texts = golden._whole_turn(case, _DSU())
    return {**texts, "outcome": captured[0], "params": captured[1]}


@pytest.mark.parametrize(
    ("baseline", "prompt_fold", "counted"),
    [(True, True, 2), (True, False, 1), (False, False, 0)],
    ids=["through_process_turn", "reply_path_with_baseline", "no_baseline"],
)
def test_a_turn_whose_folds_raise_counts_only_the_prompt_and_the_statement(
    monkeypatch, narration_counts, baseline, prompt_fold, counted
):
    """Ruling Q1 (critique point 8): the narration's own fold counts nothing,
    the statement's (``_statement_record``) once when there is a baseline
    record, the prompt's (``NarrationFold``) once through ``process_turn``.
    Step 0 and the reply no longer fold for the confirmation, so the met
    request adds nothing (``main`` counted two more). Each reader yields its
    declared line: the offer's summary and the reply state the decision."""
    texts = _ready_turn(monkeypatch, baseline=baseline, prompt_fold=prompt_fold)
    assert narration_counts.count(WritePoint.NARRATION) == counted
    assert texts["outcome"] is None
    confirmation = confirmation_message(texts["params"])
    assert texts["params"]["identification_leg"] == "working_conclusion"
    assert confirmation in texts["reply"]
    assert texts["summary"] == confirmation


# ---------------------------------------------------------------------------
# MECE: one derivation inside the record (ruling of 2026-11-26 (3))
# ---------------------------------------------------------------------------


def _stale_contest_flag():
    """What a pre-prompt hypothesis intent (retire or refute) leaves: no
    contest stands, and the flag the last recompute wrote still says one
    does (no recompute runs before the prompt)."""
    case = _bare_case()
    _rootless(case)
    case.progress.cause_identification_contested = True
    assert not mece_contested_root_ids(case), "premise: no contest stands"
    return case


def test_a_stale_contest_flag_is_not_the_records_verdict():
    """The record's reason agrees with its contested roots: none, so the
    next reason holds (``main`` read IDENTIFICATION_CONTESTED off the flag
    beside an empty ``contested_root_ids``)."""
    cause = compute_case_outcome(None, _stale_contest_flag(), at=WritePoint.CYCLE).cause
    assert cause.contested_root_ids == []
    assert cause.pool_empty_reason == PoolEmptyReason.ROOTLESS_VALIDATED_ONLY


def test_a_contest_the_flag_missed_is_the_records_verdict(monkeypatch):
    """The other direction: the flag reads no contest and the fold's one
    computation finds one; the reason and the roots both say so."""
    case = _bare_case()
    _rootless(case)
    case.progress.cause_identification_contested = False
    monkeypatch.setattr(
        outcome_module, "mece_contested_root_ids", lambda _c: {"cn_00000000000a"}
    )
    cause = compute_case_outcome(None, case, at=WritePoint.CYCLE).cause
    assert cause.contested_root_ids == ["cn_00000000000a"]
    assert cause.pool_empty_reason == PoolEmptyReason.IDENTIFICATION_CONTESTED


def test_the_deciders_leg_keeps_the_flag():
    """``cause_identification_leg`` still reads the recompute's flag: the
    stale flag holds the leg off (``main``'s behaviour), so no decider moves."""
    from faultmaven.core.investigation.cause_identification import (
        cause_identification_leg,
    )

    case = replies._rcc(replies._backstop(replies._held()))
    assert cause_identification_leg(case) == "rcc", "premise: a backstop leg"
    case.progress.cause_identification_contested = True
    assert cause_identification_leg(case) is None


def _verdicts_agree(case) -> bool:
    """The fresh MECE verdict and the flag-read one give the same reason."""
    fresh = mece_contested_root_ids(case)
    flagged = {"flag"} if case.progress.cause_identification_contested else set()
    return cause_pool_empty_reason(case, contested=fresh) == (
        cause_pool_empty_reason(case, contested=flagged)
    )


def test_every_cycle_written_record_reads_the_verdict_the_flag_did(monkeypatch):
    """Over the golden corpus's turns: at every cycle's record (after the
    final recompute, which writes the flag from the same function) the fresh
    verdict is the flag-read one, so no cycle-written record changes."""
    seen: list = []
    real = turn_application.record_case_outcome

    def _check(case, *, at):
        if at == WritePoint.CYCLE:
            seen.append(_verdicts_agree(case))
        return real(case, at=at)

    monkeypatch.setattr(turn_application, "record_case_outcome", _check)
    for build in (
        *golden.CYCLE_SCENARIOS.values(),
        *golden.B1C_SCENARIOS.values(),
        *golden.B1D_SCENARIOS.values(),
    ):
        build()
    assert len(seen) >= 5, "premise: the corpus runs whole turns"
    assert all(seen)


@pytest.mark.parametrize("name", sorted(replies.CASES))
async def test_every_parity_fixture_reads_the_verdict_the_flag_did(name):
    """Over the parity and reply corpora, once the recompute ran as a cycle
    runs it before its record."""
    case = await replies.build(name)
    if case.state == CaseState.INVESTIGATING:
        _recompute_cause_state_from_chain(case)
    assert _verdicts_agree(case)


# ---------------------------------------------------------------------------
# Q6: one predicate for the summary gate's two halves; the shape stays
# ---------------------------------------------------------------------------


def test_the_record_shape_does_not_move():
    assert CURRENT_OUTCOME_SHAPE == 5


def _trivial_closed():
    case = investigating_case()
    case.evidence = []
    case.hypotheses = {}
    return case.model_copy(update={"state": CaseState.CLOSED})


CLOSED = {
    **{n: (lambda n=n: asyncio.run(replies.build(n))) for n in replies.TERMINALS},
    "trivial": _trivial_closed,
}


@pytest.mark.parametrize("name", sorted(CLOSED))
def test_the_report_is_offered_exactly_when_it_is_not_refused(name):
    """``_closed_suggestions`` offers the closure summary exactly when the
    terminal transition and a regeneration would generate it (#698, ruling
    Q6): one predicate."""
    case = CLOSED[name]()
    if case.state != CaseState.CLOSED:
        pytest.skip("a resolve: the gate is CLOSED's")
    handler = TerminalTurnHandler(
        deps=SimpleNamespace(report_service=None), generator=None, runbooks=None
    )
    offered = bool(_closed_suggestions(case, 1))
    payload, _failed = asyncio.run(
        handler.auto_generate_report(case, plan=TurnCommitPlan())
    )
    regen = asyncio.run(
        handler._handle_report_regeneration(case, {}, plan=TurnCommitPlan())
    )
    skip = terminal_summary_skip_reason(case)
    assert offered == (payload is None) == (regen["agent_response"] != skip)


def test_the_closed_corpus_shows_both_sides_of_the_gate():
    offered = {bool(_closed_suggestions(build(), 1)) for build in CLOSED.values()}
    assert offered == {True, False}
