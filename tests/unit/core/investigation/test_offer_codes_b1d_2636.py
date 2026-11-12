"""Offer prose after the record (#2636 B1d, #2615 stage R).

Within a decision cycle the six offer-prose writers (the rca-infeasible,
deferred, backstop, false-alarm and needs-info-unmet closes and resolves, and
the needs-info request) and the summaries of the step-0 request turned READY
and of the model's proposal record an ``OfferCode`` with the facts their
decider read (decide-time, A4). The narration step renders each after the
cycle's record (``render_offer``), publishes it under its key and writes it
as the offer's summary. Every text and every stored ``Offer.summary`` is
``main``'s, captured by running ``main`` at ``21921b408``
(``golden_cases_2636.B1D_SCENARIOS``), whole turns through ``process_turn``
included. A conflict between a decision and the record is counted only on a
record the cycle stored. No live LLM: the generator is stubbed.
"""

import json

import pytest

from faultmaven.core.investigation import lifecycle_metrics
from faultmaven.core.investigation.milestone_engine import (
    turn_application,
    turn_completion,
    turn_narration,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
    narrate_turn,
)
from faultmaven.core.investigation.narration.codes import (
    CONTEXT_ONLY,
    DECIDED_FROM,
    DECIDED_PARAMS,
    DECISIONS_KEY,
    PROSE_KEYS,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.notices import (
    has_renderer,
    render_notice,
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.core.investigation.offers import answer_offer
from faultmaven.core.investigation.outcome import record_case_outcome
from faultmaven.core.investigation.terminal_transitions import (
    assess_close_summary,
    assess_closure_readiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    ClosureEligibility,
    ResolutionReason,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import narrated as narrated_module
from tests.unit.core.investigation.narrated import narrated

pytestmark = pytest.mark.unit

#: ``main``'s texts, captured by running ``main`` (``golden_cases_2636``).
ORACLE = json.loads(golden.GOLDEN.read_text())


def oracle(name: str) -> dict:
    return ORACLE[f"cycle:offer:{name}"]


@pytest.mark.parametrize("name", sorted(golden.B1D_SCENARIOS))
def test_each_offer_is_the_text_main_published_and_stored(name):
    assert golden.B1D_SCENARIOS[name]() == oracle(name)


def test_every_scenario_was_captured_on_main():
    assert {f"cycle:offer:{n}" for n in golden.B1D_SCENARIOS} <= set(ORACLE)


def test_the_stored_summary_is_the_one_decided_not_the_reply_composed_later():
    """Critique 1: housekeeping ages the working-conclusion leg after step 0
    decided the request READY, so ``main`` stored a summary naming the cause
    and composed a reply naming none. The branch stores ``main``'s summary,
    rendered from the facts step 0 read, not from the case at narration."""
    captured = oracle("turn_request_ready_on_a_decaying_leg")
    assert "- **Root cause**: The OIDC provider" in captured["summary"]
    assert "- **Root cause**: no root cause on record" in captured["reply"]
    assert golden.turn_request_ready_on_a_decaying_leg() == captured


# ---------------------------------------------------------------------------
# The composers: one per text, rendering the decider's params
# ---------------------------------------------------------------------------


def _round_trips(monkeypatch) -> list:
    """Run every B1a readiness, closure and summary scenario, checking that
    the offer each would open renders its decider's message."""
    seen: list = []

    def _readiness(case):
        r = assess_resolution_readiness(case)
        assert render_offer(OfferCode.RESOLUTION_NEEDS_INFO, r.offer_params()) == (
            r.message
        )
        seen.append(r.reason)
        return None

    def _closure(readiness):
        params = {"composer": "closure", **readiness.offer_params()}
        assert render_offer(OfferCode.MODEL_PROPOSAL, params) == readiness.message
        seen.append(readiness.verdict)
        return None

    monkeypatch.setattr(golden, "_readiness", _readiness)
    monkeypatch.setattr(golden, "_closure", _closure)
    for name, build in golden.SCENARIOS.items():
        if name.startswith(("readiness_", "closure_", "summary_")):
            build()
    return seen


def test_each_readiness_and_closure_renders_its_message(monkeypatch):
    seen = _round_trips(monkeypatch)
    assert {str(v) for v in seen} >= {
        "false_alarm",
        "pending_check",
        "nothing_on_record",
        "fix_off_cause",
        "fix_failed",
        "documentation_gaps",
        ClosureEligibility.SUGGEST_RESOLVE,
        ClosureEligibility.HAS_SUBSTANCE,
        ClosureEligibility.TRIVIAL,
    }


@pytest.mark.parametrize("declined", [False, True])
@pytest.mark.parametrize("fulfilled", [False, True])
def test_a_held_check_renders_its_readiness_message(declined, fulfilled):
    case = golden._held(fulfilled=fulfilled)
    if declined:
        golden._declined(case)
    r = assess_resolution_readiness(case)
    assert r.reason == "pending_check"
    assert render_offer(OfferCode.RESOLUTION_NEEDS_INFO, r.offer_params()) == (
        r.message
    )


def test_the_summary_composer_names_the_cause_as_main_did():
    """``assess_close_summary`` keeps ``main``'s root-cause mapping."""
    case = golden._chain_without_conclusion()
    summary = assess_close_summary(case)
    assert summary.offer_params()["root_cause"] == "the identified root cause"
    assert summary.offer_params()["identification_leg"] == "chain"


def test_a_pivot_reads_its_facts_once_when_first_read():
    """The pivot's params are built lazily, once: a verdict-only reader builds
    none, and the message and the offer read the same facts."""
    case = golden._held(asked=False)
    closure = assess_closure_readiness(case)
    assert callable(closure._params)
    assert closure.message == render_offer(
        OfferCode.MODEL_PROPOSAL, {"composer": "closure", **closure.offer_params()}
    )
    assert not callable(closure._params)


# ---------------------------------------------------------------------------
# The narration step
# ---------------------------------------------------------------------------


def _order(monkeypatch) -> list:
    """Each record write and each offer rendering of the turn, in order, with
    the open offer's summary at the record."""
    events: list = []
    real_record = turn_application.record_case_outcome
    real_render = turn_narration.render_offer

    def _record(case, *, at):
        offer = case.offer_record.open
        events.append(("record", offer.summary if offer is not None else None))
        return real_record(case, at=at)

    def _render(code, params):
        events.append(("render", str(code)))
        return real_render(code, params)

    monkeypatch.setattr(turn_application, "record_case_outcome", _record)
    monkeypatch.setattr(turn_narration, "render_offer", _render)
    return events


def test_pin_4_an_offer_decided_in_step_2_is_rendered_after_the_record(monkeypatch):
    """The model's ``resolved`` opens the resolve offer in step 2; at the
    cycle's record its summary is still empty, and the narration step
    renders it after, ``main``'s byte for byte."""
    events = _order(monkeypatch)
    texts = golden.turn_model_proposal_confirmation_on_a_decaying_leg()
    assert events == [("record", ""), ("render", OfferCode.MODEL_PROPOSAL)]
    assert texts == oracle("turn_model_proposal_confirmation_on_a_decaying_leg")


def test_a_replayed_close_is_rendered_by_the_live_turns_narration(monkeypatch):
    """A statement-revision replay opens the rca-infeasible close inside a
    bundle; a turn that replays reaches ``_finalize_turn``, whose narration
    step renders the offer's summary and prose after the live record, as
    ``main`` stored and published them (amendment 2: no step-0 exit follows
    a replay)."""
    events = _order(monkeypatch)
    texts = golden.turn_replayed_rca_infeasible_close()
    assert ("render", OfferCode.RCA_INFEASIBLE_CLOSE) in events
    rendered = events.index(("render", OfferCode.RCA_INFEASIBLE_CLOSE))
    assert [e for e in events[:rendered] if e[0] == "record"][-1] == ("record", "")
    assert texts == oracle("turn_replayed_rca_infeasible_close")
    assert texts["basis"] == "rca_infeasible"
    assert texts["summary"] in texts["reply"]


def test_the_prose_is_published_when_the_offer_closed_in_its_cycle(caplog):
    """``main`` published the prose when it opened the offer: an offer answered
    later in the cycle keeps it, and no summary is written for it."""
    case, metadata = golden._deferred(confirmed=False)
    offer = case.offer_record.open
    case.offer_record = answer_offer(
        case,
        proposal_id=offer.proposal_id,
        answer=AnswerKind.LAPSED,
        via=AnswerVia.ENGINE,
    )
    narrated(case, metadata)
    expected = oracle("deferred_close")["prose"]["deferred_solution_gate_message"]
    assert metadata["deferred_solution_gate_message"] == expected
    assert case.offer_record.open is None
    assert "no summary written" in caplog.text


def _counted(monkeypatch) -> list:
    counts: list = []
    monkeypatch.setattr(
        turn_narration, "_count", lambda code, what, *_: counts.append((code, what))
    )
    return counts


def _model_close(case):
    return golden._models_proposal(case, "closed", "close it")


def _decision(metadata: dict, code: OfferCode) -> dict:
    (entry,) = [e for e in metadata[DECISIONS_KEY] if e["code"] == code]
    return entry["params"]


def test_a_stored_record_that_differs_from_the_decision_counts_one_conflict(
    monkeypatch,
):
    counts = _counted(monkeypatch)
    case, metadata = _model_close(golden._identified_case()[0])
    params = _decision(metadata, OfferCode.MODEL_PROPOSAL)
    assert params["verdict"] == ClosureEligibility.HAS_SUBSTANCE
    params["verdict"] = ClosureEligibility.TRIVIAL
    narrated(case, metadata)
    assert counts == [(OfferCode.MODEL_PROPOSAL, "closure.eligibility")]


def test_an_in_memory_pass_counts_no_conflict(monkeypatch):
    """Counting (i): the same disagreement on a cycle whose record was not
    stored counts nothing, and the prose is still rendered."""
    counts = _counted(monkeypatch)
    case, metadata = _model_close(golden._identified_case()[0])
    _decision(metadata, OfferCode.MODEL_PROPOSAL)["verdict"] = "trivial"
    assert not metadata.get(OUTCOME_WRITTEN_KEY)
    narrate_turn(case, metadata)
    assert counts == []
    assert case.offer_record.open.summary


def test_a_chain_identified_cause_counts_no_conflict(monkeypatch):
    counts = _counted(monkeypatch)
    case, metadata = _model_close(golden._chain_without_conclusion())
    assert _decision(metadata, OfferCode.MODEL_PROPOSAL)["identification_leg"] == (
        "chain"
    )
    narrated(case, metadata)
    assert case.progress.outcome.cause.identification_leg == "chain"
    assert counts == []


def test_an_identified_cause_the_record_no_longer_states_is_counted(monkeypatch):
    counts = _counted(monkeypatch)
    case, metadata = _model_close(golden._chain_without_conclusion())
    _decision(metadata, OfferCode.MODEL_PROPOSAL)["identification_leg"] = None
    narrated(case, metadata)
    assert counts == [(OfferCode.MODEL_PROPOSAL, "cause.identification_leg")]


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("turn_request_ready_on_a_decaying_leg", OfferCode.REQUEST_READY),
        (
            "turn_model_proposal_confirmation_on_a_decaying_leg",
            OfferCode.MODEL_PROPOSAL,
        ),
        ("turn_backstop_on_a_decaying_leg", OfferCode.BACKSTOP_RESOLVE),
        ("turn_model_proposal_closure_on_a_decaying_leg", OfferCode.MODEL_PROPOSAL),
    ],
)
def test_a_cause_identified_when_decided_and_aged_out_by_the_record_is_counted(
    monkeypatch, scenario, code
):
    """Critique 1's drift, counted: the decider read the working-conclusion
    leg; housekeeping aged it under the bar before the cycle's stored record,
    which identifies no cause. Counted on that stored record, the text still
    ``main``'s."""
    counts = _counted(monkeypatch)
    stored: list = []
    real = turn_narration._conflicts

    def _on_stored(log, outcome):
        stored.append(outcome.cause.identification_leg)
        return real(log, outcome)

    monkeypatch.setattr(turn_narration, "_conflicts", _on_stored)
    assert golden.B1D_SCENARIOS[scenario]() == oracle(scenario)
    assert stored == [None]
    assert (code, "cause.identification_leg") in counts


def test_a_held_check_at_step_2_is_counted_once(monkeypatch):
    """Step 2's pending check is one decision logged under two codes
    (``PENDING_CHECK`` and its needs-info request, both of reason
    ``pending_check``): a stored record stating another reason counts it
    once."""
    counts = _counted(monkeypatch)
    case, metadata = golden._models_proposal(golden._held(), "resolved")
    codes = [e["code"] for e in metadata[DECISIONS_KEY]]
    assert {NoticeCode.PENDING_CHECK, OfferCode.RESOLUTION_NEEDS_INFO} <= set(codes)
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case, at="cycle")
    case.progress.outcome.resolution.reason = ResolutionReason.CONFIRMED
    narrate_turn(case, metadata)
    assert counts == [(NoticeCode.PENDING_CHECK, "resolution.reason")]


def test_every_counted_code_is_a_declared_label():
    labels = set(lifecycle_metrics._OUTCOME_RECORD_FIELDS)
    assert {f"narration.{code}" for code, _composer in DECIDED_FROM} <= labels


def test_every_offer_code_names_what_it_was_decided_from():
    """Each offer code (each composer of the model's proposal) and the pending
    check compare at least one field, each one ``DECIDED_PARAMS`` names."""
    keys = {code for code, _composer in DECIDED_FROM}
    assert set(OfferCode) | {NoticeCode.PENDING_CHECK} == keys
    for fields in DECIDED_FROM.values():
        assert fields and set(fields) <= set(DECIDED_PARAMS)


def test_the_engine_offer_standing_renders_the_same_with_any_outcome(monkeypatch):
    """Its renderer reads only its params, so the narration step may compute
    no outcome for it (B1c's check, for B1d's notice code)."""
    from tests.unit.core.investigation.test_notice_codes_b1c_2636 import (
        _Untouchable,
    )

    logs: list = []
    real = turn_narration.narrate_turn

    def _seen(case, metadata):
        logs.append(list(metadata.get(DECISIONS_KEY) or []))
        return real(case, metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _seen)
    golden.turn_model_proposal_beside_the_deferred_close()
    ((log,),) = [
        [e for e in log if e["code"] == NoticeCode.ENGINE_OFFER_STANDS] for log in logs
    ]
    params = log["params"]
    assert render_notice(
        NoticeCode.ENGINE_OFFER_STANDS, params, _Untouchable(), [log]
    ) == render_notice(NoticeCode.ENGINE_OFFER_STANDS, params, None, [log])


# ---------------------------------------------------------------------------
# Pin 6, its code-exhaustiveness part
# ---------------------------------------------------------------------------


def _logged_codes(monkeypatch) -> set:
    """Every code a B1b, B1c or B1d scenario's cycle logged, as the narration
    step saw it."""
    codes: set = set()
    real = turn_narration.narrate_turn

    def _seen(case, metadata):
        codes.update(e["code"] for e in metadata.get(DECISIONS_KEY) or [])
        for entry in metadata.get(DECISIONS_KEY) or []:
            if entry["code"] in set(OfferCode):
                assert render_offer(entry["code"], entry["params"])
            elif entry["code"] not in CONTEXT_ONLY:
                assert render_notice(entry["code"], entry["params"], None, [])
        return real(case, metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _seen)
    monkeypatch.setattr(narrated_module, "narrate_turn", _seen)
    for build in (
        *golden.CYCLE_SCENARIOS.values(),
        *golden.B1C_SCENARIOS.values(),
        *golden.B1D_SCENARIOS.values(),
    ):
        result = build()
        if isinstance(result, tuple):
            narrated(*result)
    return codes


def test_pin_6_every_code_has_a_renderer_or_a_composer_and_a_fixture(monkeypatch):
    """Every ``NoticeCode`` but a context-only one has a renderer, every
    ``OfferCode`` a composer (``render_offer``), and each is logged by a
    scenario whose text is ``main``'s. #2615's pin 6 as written (a model
    renderer for every reason) stays S2's."""
    for code in set(NoticeCode) - CONTEXT_ONLY:
        assert has_renderer(code), code
    logged = _logged_codes(monkeypatch)
    # A19's context code is logged by the step after the scenarios' cycles
    # (``_record_hypotheses_closed_later``), its fixture below.
    retired = {NoticeCode.HYPOTHESIS_RETIRED}
    assert (set(NoticeCode) | set(OfferCode)) - retired <= logged
    case, metadata = golden.CYCLE_SCENARIOS["refutation_refused_open"]()
    (h_id,) = case.hypotheses
    case.hypotheses[h_id].state = "retired"
    turn_application._record_hypotheses_closed_later(case, metadata)
    assert NoticeCode.HYPOTHESIS_RETIRED in {e["code"] for e in metadata[DECISIONS_KEY]}
    assert "It stays open" not in " ".join(narrated(case, metadata))


def test_every_prose_code_publishes_under_a_key_the_reply_reads():
    import inspect

    source = inspect.getsource(turn_completion._compose_turn_reply)
    for code in PROSE_KEYS:
        assert f'"{PROSE_KEYS[code]}"' in source, code
