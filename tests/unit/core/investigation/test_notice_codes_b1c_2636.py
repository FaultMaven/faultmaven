"""The remaining outcome notices after the record (#2636 B1c, #2615 stage R).

Within a decision cycle the deciders B1b left composing text record codes
instead: the failed-window absence, the unbound re-checks, the failed fix, the
licence refusals, the ignored fix report, the untargeted fix, the signals on
an invalidated problem, the false-alarm and revision refusals, the
root-terminal refusals, the pending check and the declined re-asks. The
narration step renders each from the facts its decider read (decide-time,
A4), so every text is ``main``'s, captured by running ``main``
(``golden_cases_2636.B1C_SCENARIOS``, ``golden_main_2636.json``), except two
cases declared under the ruled A9 (one line for two equal decisions in one
cycle): the absence decided twice in one cycle, and the declined re-ask
decided at step 0 and at step 2 of one turn. No live LLM: the generator is
stubbed.
"""

import json

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine import (
    turn_completion,
    turn_narration,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    merge_decisions,
    merge_system_notices,
    record_decision,
)
from faultmaven.core.investigation.narration.codes import (
    CONTEXT_ONLY,
    DECISIONS_KEY,
    NoticeCode,
    OfferCode,
    is_token,
)
from faultmaven.core.investigation.narration.notices import (
    READS_THE_RECORD,
    render_notice,
    unbound_lines,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation.narrated import narrated

pytestmark = pytest.mark.unit

#: ``main``'s texts, captured by running ``main`` (``golden_cases_2636``).
ORACLE = json.loads(golden.GOLDEN.read_text())

#: B1c's codes, appended after B1b's seven.
B1C_CODES = list(NoticeCode)[7:]


def oracle(name: str) -> list[str]:
    return ORACLE[f"cycle:{name}"]


def rendered(name: str) -> list[str]:
    """What this tree tells the model for the scenario: the record, then the
    narration step."""
    case, metadata = golden.B1C_SCENARIOS[name]()
    return golden.cycle_text(narrated(case, metadata))


#: The declared differences (A9, ruled 2026-11-11), asserted below.
DECLARED = {
    "absence_decided_twice_in_one_cycle",
    "declined_resolution_reask_at_step_0_and_step_2",
}


@pytest.mark.parametrize("name", sorted(set(golden.B1C_SCENARIOS) - DECLARED))
def test_each_notice_is_the_text_main_composed(name):
    assert rendered(name) == oracle(name)


def test_every_scenario_was_captured_on_main():
    assert {f"cycle:{n}" for n in golden.B1C_SCENARIOS} <= set(ORACLE)


@pytest.mark.parametrize("name", sorted(golden.B1C_SCENARIOS))
def test_a_decided_notice_is_rendered_where_it_stands(name):
    """Critique 1 and 2, through the narration step (``_replace_token``
    included): every B1c decision renders at least one line, and each of its
    lines is in the turn's notices."""
    case, metadata = golden.B1C_SCENARIOS[name]()
    log = list(metadata.get(DECISIONS_KEY) or [])
    notices = narrated(case, metadata)
    # #2555: the unbound re-checks are told by ``narrate_turn`` alone, from
    # the record's rows, read before the narration marked them (the
    # scenarios store no record before it).
    record = outcome_module.compute_case_outcome(None, case)
    for entry in log:
        if entry["code"] == NoticeCode.UNBOUND_RECHECKS:
            texts = [line for line, _ids in unbound_lines(entry["params"], record)]
        elif entry["code"] in B1C_CODES:
            texts = render_notice(entry["code"], entry["params"], None, log)
        else:
            continue
        assert texts and all(text in notices for text in texts)


# ---------------------------------------------------------------------------
# The two cycles whose record differs from what the decider read
# ---------------------------------------------------------------------------


def _decided(metadata: dict, code: NoticeCode) -> dict:
    (entry,) = [e for e in metadata[DECISIONS_KEY] if e["code"] == code]
    return entry["params"]


def test_the_absence_notice_states_the_window_its_decider_read():
    """The window failed on a relapse at 12 and is carried whole; at 14 the
    decider reads the cause re-checked gone beside the problem, a
    disconfirmation the record's window does not hold. The text is the
    decider's, as on ``main``."""
    name = "absence_after_a_relapse_later_disconfirmed"
    case, metadata = golden.B1C_SCENARIOS[name]()
    assert _decided(metadata, NoticeCode.ABSENCE_IN_FAILED_WINDOW)["disconfirmed"]
    text = golden.cycle_text(narrated(case, metadata))
    (window,) = [
        a.window
        for a in case.progress.outcome.attempts
        if a.window is not None and a.window.fix_turn == 6
    ]
    assert window.disconfirmation is None, "premise: the record differs"
    assert text == oracle(name)
    assert "the cause was shown removed" in text[0]


def test_the_unbound_notice_states_the_pool_its_decider_read():
    """Housekeeping retires a competitor after the decider, so the record's
    confirmation roots differ from the pool the decider read. The text is the
    decider's, as on ``main``."""
    name = "unbound_before_housekeeping_retires_a_competitor"
    case, metadata = golden.B1C_SCENARIOS[name]()
    assert _decided(metadata, NoticeCode.UNBOUND_RECHECKS)["pool_roots"] is False
    text = golden.cycle_text(narrated(case, metadata))
    assert case.progress.outcome.cause.confirmation_root_ids, "premise: it differs"
    assert text == oracle(name)


# ---------------------------------------------------------------------------
# One line for two equal decisions (A9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DECLARED))
def test_a9_two_equal_decisions_in_one_cycle_render_one_line(name):
    """``main`` wrote the line twice; each line is rendered once, in place."""
    lines = oracle(name)
    assert len(set(lines)) < len(lines), "premise: main repeated a line"
    assert rendered(name) == list(dict.fromkeys(lines))


def test_a_bundle_and_the_live_apply_deciding_one_notice_render_one_line():
    """As ``main``'s merge keeps it: the replay's merge folds the two
    decisions into one."""
    (line,) = oracle("absence_decided_by_a_bundle_and_the_live_apply")
    assert rendered("absence_decided_by_a_bundle_and_the_live_apply") == [line]


def _disconfirmed_at(metadata: dict, failure_turn: int) -> None:
    record_decision(
        metadata,
        NoticeCode.ABSENCE_IN_FAILED_WINDOW,
        prepend=True,
        disconfirmed=True,
        relief_turn=None,
        failure_turn=failure_turn,
        fix_turn=8,
    )


@pytest.mark.parametrize("merged", [True, False], ids=["bundle-and-live", "one-apply"])
def test_two_decisions_with_other_params_and_one_text_render_one_line(merged):
    """Two keys, one text: a disconfirmed window's line does not name its
    failure turn, so decisions that differ only there render one line (A9),
    a replayed bundle's beside the live apply's or two in one apply."""
    case, _metadata = golden.absence_after_a_persistence()
    first: dict = {}
    _disconfirmed_at(first, failure_turn=9)
    second = {} if merged else first
    _disconfirmed_at(second, failure_turn=10)
    if merged:
        merge_system_notices(first, second)
        merge_decisions(first, second)
    assert len(first[DECISIONS_KEY]) == 2, "premise: two keys"
    assert golden.cycle_text(narrated(case, first)) == oracle(
        "absence_after_a_persistence"
    )


# ---------------------------------------------------------------------------
# What the renderers read
# ---------------------------------------------------------------------------


class _Untouchable:
    """An outcome no renderer outside ``READS_THE_RECORD`` may read."""

    def __getattr__(self, name):
        raise AssertionError(f"read outcome.{name}")


def _logged():
    """Every logged decision of the B1b and B1c scenarios, with its log."""
    for build in (*golden.CYCLE_SCENARIOS.values(), *golden.B1C_SCENARIOS.values()):
        _case, metadata = build()
        log = list(metadata.get(DECISIONS_KEY) or [])
        for entry in log:
            yield entry, log


def test_only_the_record_reading_codes_read_the_outcome():
    """A renderer outside ``READS_THE_RECORD`` renders the same with any
    outcome as with none, so the narration step may compute none for it."""
    covered = set()
    for entry, log in _logged():
        code = entry["code"]
        # B1d's offer codes are rendered by ``render_offer``, which takes no
        # outcome (``test_offer_codes_b1d_2636.py``).
        if code in READS_THE_RECORD or code in CONTEXT_ONLY or code in set(OfferCode):
            continue
        if code == NoticeCode.UNBOUND_RECHECKS:
            # #2555: no renderer; ``narrate_turn`` tells it from a stored
            # record alone (``unbound_lines``), and ``render_notice`` refuses.
            with pytest.raises(ValueError):
                render_notice(code, entry["params"], None, log)
            continue
        covered.add(code)
        params = entry["params"]
        assert render_notice(code, params, _Untouchable(), log) == render_notice(
            code, params, None, log
        )
    # B1d's ENGINE_OFFER_STANDS is logged by a whole turn only: checked in
    # ``test_offer_codes_b1d_2636.py``
    # (``test_the_engine_offer_standing_renders_the_same_with_any_outcome``).
    assert covered == set(NoticeCode) - READS_THE_RECORD - CONTEXT_ONLY - {
        NoticeCode.ENGINE_OFFER_STANDS,
        NoticeCode.UNBOUND_RECHECKS,
    }


def _failing_write(monkeypatch) -> list:
    """A derivation the write and the in-memory pass share raises; returns
    the narration step's in-memory computations."""

    def _boom(*_a, **_k):
        raise RuntimeError("no outcome")

    monkeypatch.setattr(outcome_module, "shown_milestones", _boom)
    computed: list = []
    real = turn_narration.compute_case_outcome

    def _counted(*args, **kwargs):
        computed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(turn_narration, "compute_case_outcome", _counted)
    return computed


def test_a_failed_write_computes_no_outcome_for_codes_that_read_none(
    monkeypatch, caplog
):
    """Item 9: with only codes rendered from their params, a failed write
    leads to no in-memory outcome and no "no outcome" log line; the text is
    ``main``'s."""
    case, metadata = golden.absence_after_a_persistence()
    computed = _failing_write(monkeypatch)
    assert golden.cycle_text(narrated(case, metadata)) == oracle(
        "absence_after_a_persistence"
    )
    assert computed == [] and case.progress.outcome is None
    assert "no outcome to narrate from" not in caplog.text


def test_a_failed_write_computes_the_outcome_for_a_code_that_reads_it(monkeypatch):
    """The control: a code whose renderer reads the record still gets one."""
    case, metadata = golden.mitigation_no_bound_relief_seen_present()
    computed = _failing_write(monkeypatch)
    narrated(case, metadata)
    assert computed == [1]


# ---------------------------------------------------------------------------
# Through process_turn: one turn per decider family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (
            "turn_stage_gates_fix_report_without_an_acceptance",
            NoticeCode.FIX_REPORT_IGNORED,
        ),
        (
            "turn_response_application_false_alarm_refused",
            NoticeCode.FALSE_ALARM_REFUSED,
        ),
        ("turn_chain_emission_root_terminal_refused", NoticeCode.ROOT_TERMINAL_REFUSED),
        ("turn_transitions_declined_close_reask", NoticeCode.DECLINED_CLOSE_REASK),
        ("turn_terminal_proposals_pending_check", NoticeCode.PENDING_CHECK),
    ],
)
def test_each_decider_family_through_a_turn(monkeypatch, name, code):
    """The narration step finds the code holding its place with a token; the
    turn record holds ``main``'s whole notice list, in order, and no token."""
    seen: dict = {}
    real = turn_completion.narrate_turn

    def _spy(case, metadata):
        seen["notices"] = list(metadata.get("system_notices") or [])
        seen["codes"] = [e["code"] for e in metadata.get(DECISIONS_KEY) or []]
        real(case, metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _spy)
    _case, metadata = golden.B1C_SCENARIOS[name]()
    notices = metadata["system_notices"]
    assert code in seen["codes"]
    assert any(is_token(n) for n in seen["notices"]), "a token before narration"
    assert not any(is_token(n) for n in notices), "none in the turn record"
    assert golden.cycle_text(notices) == oracle(name)
