"""The remaining outcome notices after the record (#2636 B1c, #2615 stage R).

Within a decision cycle the deciders B1b left composing text record codes
instead: the failed-window absence, the unbound re-checks, the failed fix, the
licence refusals, the ignored fix report, the untargeted fix, the signals on
an invalidated problem, the false-alarm and revision refusals, the
root-terminal refusals, the pending check and the declined re-asks. The
narration step renders each from the facts its decider read (decide-time,
A4), so every text is ``main``'s, captured by running ``main``
(``golden_cases_2636.B1C_SCENARIOS``, ``golden_main_2636.json``), except one
declared under the ruled A9: two equal decisions in one cycle render one
line. No live LLM: the generator is stubbed.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.narration.codes import (
    DECISIONS_KEY,
    NoticeCode,
    is_token,
)
from faultmaven.core.investigation.narration.notices import render_notice
from faultmaven.core.investigation.schemas import (
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    ProblemVerificationUpdate,
)
from faultmaven.modules.case.contracts import ProblemStatus
from tests.case_builders import with_declined_offer
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _DSU
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

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


#: The declared difference (A9, ruled 2026-11-11), asserted below.
DECLARED = {"absence_decided_twice_in_one_cycle"}


@pytest.mark.parametrize("name", sorted(set(golden.B1C_SCENARIOS) - DECLARED))
def test_each_notice_is_the_text_main_composed(name):
    assert rendered(name) == oracle(name)


def test_every_scenario_was_captured_on_main():
    assert {f"cycle:{n}" for n in golden.B1C_SCENARIOS} <= set(ORACLE)


@pytest.mark.parametrize("name", sorted(golden.B1C_SCENARIOS))
def test_a_decided_notice_never_renders_nothing(name):
    """Critique 1 and 2: whatever the record says, a decision renders its
    text, with no outcome as with one."""
    _case, metadata = golden.B1C_SCENARIOS[name]()
    log = metadata.get(DECISIONS_KEY) or []
    for entry in log:
        if entry["code"] in B1C_CODES:
            assert render_notice(entry["code"], entry["params"], None, log)


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


def test_a9_two_equal_decisions_in_one_cycle_render_one_line():
    first, second = oracle("absence_decided_twice_in_one_cycle")
    assert first == second, "premise: main wrote the same line twice"
    assert rendered("absence_decided_twice_in_one_cycle") == [first]


def test_a_bundle_and_the_live_apply_deciding_one_notice_render_one_line():
    """As ``main``'s merge keeps it: the replay's merge folds the two
    decisions into one."""
    (line,) = oracle("absence_decided_by_a_bundle_and_the_live_apply")
    assert rendered("absence_decided_by_a_bundle_and_the_live_apply") == [line]


# ---------------------------------------------------------------------------
# The failed write
# ---------------------------------------------------------------------------


def _no_outcome(monkeypatch) -> None:
    """A derivation the write and the in-memory pass share raises."""

    def _boom(*_a, **_k):
        raise RuntimeError("no outcome")

    monkeypatch.setattr(outcome_module, "shown_milestones", _boom)


@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n in golden.B1C_SCENARIOS
        if n.startswith(("absence_", "unbound_"))
        and n not in DECLARED | {"absence_after_a_relapse_later_disconfirmed"}  # stored
    ),
)
def test_with_no_outcome_the_notices_are_the_stored_record_texts(monkeypatch, name):
    case, metadata = golden.B1C_SCENARIOS[name]()
    _no_outcome(monkeypatch)
    assert golden.cycle_text(narrated(case, metadata)) == oracle(name)
    assert case.progress.outcome is None


# ---------------------------------------------------------------------------
# Through process_turn: one turn per decider family
# ---------------------------------------------------------------------------


async def _turn(monkeypatch, case, state_updates, message="ok") -> tuple:
    """One turn through ``process_turn``: the notices and codes the narration
    step found, then the turn record's notices."""
    seen: dict = {}
    real = turn_completion.narrate_turn

    def _spy(case, metadata):
        seen["notices"] = list(metadata.get("system_notices") or [])
        seen["codes"] = [e["code"] for e in metadata.get(DECISIONS_KEY) or []]
        real(case, metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _spy)
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=state_updates
        )
    )
    case.current_turn += 1
    await engine.process_turn(case=case, user_message=message, redaction_ctx=None)
    return seen, case.turn_history[-1].system_notices


def _assert_rendered_after(seen, notices, code, lines) -> None:
    assert code in seen["codes"]
    assert any(is_token(n) for n in seen["notices"]), "a token before narration"
    assert not any(is_token(n) for n in notices), "none in the turn record"
    for line in lines:
        assert line in golden.cycle_text(notices)


async def test_stage_gates_through_a_turn(monkeypatch):
    _eng, case, _hyp = golden.fix_report._identified()
    seen, notices = await _turn(
        monkeypatch,
        case,
        _DSU(milestones=MilestoneUpdates(accepted_fix_reported_in="previous_message")),
    )
    _assert_rendered_after(
        seen,
        notices,
        NoticeCode.FIX_REPORT_IGNORED,
        oracle("fix_report_without_an_acceptance"),
    )


async def test_response_application_through_a_turn(monkeypatch):
    case = golden.revision._case(ProblemStatus.VERIFIED)
    seen, notices = await _turn(
        monkeypatch,
        case,
        _DSU(
            verification_updates=ProblemVerificationUpdate(
                problem_invalidated=True,
                invalidation_basis="the user says it was never broken",
            )
        ),
    )
    _assert_rendered_after(
        seen, notices, NoticeCode.FALSE_ALARM_REFUSED, oracle("false_alarm_refused")
    )


async def test_chain_emission_through_a_turn(monkeypatch):
    case, _root, hyp = golden._d6_case()
    case.current_turn -= 1
    seen, notices = await _turn(
        monkeypatch,
        case,
        _DSU(
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id,
                    root_is_terminal=True,
                    terminal_basis_evidence_ids=[],
                )
            ]
        ),
    )
    _assert_rendered_after(
        seen,
        notices,
        NoticeCode.ROOT_TERMINAL_REFUSED,
        oracle("root_terminal_refused_with_none_cited"),
    )


async def test_transitions_through_a_turn(monkeypatch):
    case = golden._bare_case()
    with_declined_offer(case, kind="close", basis="deferred")
    seen, notices = await _turn(
        monkeypatch,
        case,
        _DSU(proposed_transition={"to_state": "closed"}),
        message="close it",
    )
    _assert_rendered_after(
        seen,
        notices,
        NoticeCode.DECLINED_CLOSE_REASK,
        oracle("declined_close_reask_on_the_deferred_close"),
    )


async def test_terminal_proposals_through_a_turn(monkeypatch):
    seen, notices = await _turn(monkeypatch, golden._held(), _DSU())
    _assert_rendered_after(
        seen,
        notices,
        NoticeCode.PENDING_CHECK,
        ORACLE["pending_check_backstop"]["notices"],
    )
