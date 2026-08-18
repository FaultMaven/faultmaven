"""Mention decay, enforced by the engine (fm#1079).

What was already there
======================

fm#1081 gave every EVIDENCE ask a durable identity: ``surfaced_turns`` records
which turns a need was asked on, ``<evidence_needs>`` renders it as ``asked 3×
(last turn 9)``, the prompt states "First mention: full request. Second: brief
reminder. Third+: stop surfacing", and a model-declared ``UNOBTAINABLE`` makes a
need yield its surface slot.

Why it was inert
================

Nothing branched on the count. It was rendered and persisted, and every decision
that could follow from it was left to the model. On ``case_897ce7909658``
(``sha-ed1b575``, which includes fm#1081) the agent re-asked for the STS call
path on turns 8, 11, 12, 13 and 15 after the user answered it and twice stated
no further data existed, and every evidence-need row on that case still read
``obtainability = unknown``. A fourth restatement of the rule was not going to
help.

What these pin
==============

The engine now stops making the ask instead of asking the model to stop:
``is_ask_exhausted`` is a deterministic function of the ask history, an
exhausted need's EVIDENCE suggestion is dropped at the seam, the need yields its
rotating surface slot, and ``<evidence_needs>`` reports it as suppressed rather
than listing it among asks the user can still receive.

What it deliberately does NOT do is declare the wall. ``UNOBTAINABLE`` is read
by ``verification_status._candidate_unresolvable`` and moves a case toward
INSUFFICIENT_EVIDENCE, so an engine-fabricated one would trade a redundant ask
for an unsound conclusion. Repetition shows an ask is not working; it says
nothing about whether the data exists.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.evidence_need_linking import (
    link_evidence_suggestions_to_needs,
)
from faultmaven.core.investigation.evidence_need_surfacing import (
    _SURFACED_CAUSAL_CAP,
    is_ask_exhausted,
    select_surfaced_causal_needs,
)
from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_needs_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceNeed,
    InquiryData,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)
from tests.utils import generate_case_id

pytestmark = pytest.mark.unit


# ============================================================
# Fixtures
# ============================================================


def _case(turn: int = 10) -> Case:
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Assume-role calls fail"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True
    case = Case(
        case_id=generate_case_id(),
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=inquiry,
    )
    case.current_turn = turn
    case.progress.symptom_verified = True
    return case


def _need(
    case: Case | None = None,
    *,
    asked_on: list[int] | None = None,
    state: NeedState = NeedState.PENDING,
    purpose: NeedPurpose = NeedPurpose.CAUSAL_VERIFICATION,
    priority: NeedPriority = NeedPriority.MEDIUM,
    request_text: str = "target-account OIDC provider record",
    motivating_hypothesis_ids: list[str] | None = None,
) -> EvidenceNeed:
    need = EvidenceNeed(
        case_id=case.case_id if case else generate_case_id(),
        purpose=purpose,
        request_text=request_text,
        rationale="discriminates the audience mismatch",
        priority=priority,
        state=state,
        superseded_reason="stale" if state == NeedState.SUPERSEDED else None,
        fulfilling_evidence_ids=(
            ["ev_abcdef123456"] if state == NeedState.FULFILLED else []
        ),
        motivating_hypothesis_ids=motivating_hypothesis_ids or [],
        surfaced_turns=list(asked_on or []),
        created_at_turn=1,
    )
    if case is not None:
        case.evidence_needs.append(need)
    return need


class _FollowUp:
    """Stand-in for ``SuggestedFollowUp`` — the seam touches these attributes
    only, and the real model's validators reject the mid-flight mutation the
    engine relies on."""

    def __init__(self, action_type="EVIDENCE", body="", label="", need_id=None):
        self.action_type = action_type
        self.body = body
        self.label = label
        self.evidence_need_id = need_id


def _resolve(ref, created, _prefix):
    return ref


# ============================================================
# The predicate
# ============================================================


class TestAskExhaustionPredicate:
    """Absolute turn numbers throughout. A test phrased in terms of
    ``_ASK_REPEAT_FLOOR`` / ``_ASK_DECAY_AGE_TURNS`` would pass under any value
    of them, including values that never fire — which is the shape of failure
    this whole change exists to correct."""

    def test_one_ask_is_never_exhausted_however_old(self):
        """A single ask is not a nag. Age alone must not suppress it, or an ask
        made once and never followed up would stop being offered while the user
        was still gathering it."""
        assert is_ask_exhausted(_need(asked_on=[1]), 99) is False

    def test_a_repeat_is_not_exhausted_the_moment_it_is_made(self):
        """ "Second: brief reminder" is still a delivery. Suppressing at the
        second ask would be stricter than the policy the prompt states."""
        assert is_ask_exhausted(_need(asked_on=[5, 6]), 6) is False

    def test_the_third_ask_after_two_recorded_ones_is_exhausted(self):
        """The stated policy is "third+: stop surfacing". Asked on 1 and 2, the
        ask due on turn 3 is that third one."""
        assert is_ask_exhausted(_need(asked_on=[1, 2]), 3) is True

    def test_age_is_measured_from_the_first_ask_not_the_last(self):
        """The design decision that makes the trigger robust to an undercount.

        ``surfaced_turns`` only records asks that went out as EVIDENCE
        suggestions, so a re-ask made in ``agent_response`` prose is invisible to
        it — on the fm#1079 run the nagging need showed 2 recorded surfacings
        against 5 prose asks. Keying on the FIRST ask means uncounted repeats
        cannot push the trigger later: two recorded asks eight turns apart is a
        long-running loop whatever happened in between.
        """
        assert is_ask_exhausted(_need(asked_on=[1, 9]), 10) is True
        # And the converse: two asks that both just happened are not yet a loop.
        assert is_ask_exhausted(_need(asked_on=[9, 10]), 10) is False

    def test_exhaustion_does_not_lapse(self):
        """Monotone while outstanding. Re-arming on the passage of time would
        restore the loop: the need would go quiet, become askable again, and be
        re-asked — which is the observed failure with extra steps."""
        need = _need(asked_on=[1, 2])
        assert [is_ask_exhausted(need, t) for t in range(3, 25)] == [True] * 22

    @pytest.mark.parametrize("state", [NeedState.FULFILLED, NeedState.SUPERSEDED])
    def test_a_terminal_need_is_never_exhausted(self, state):
        """Terminal needs are not asked for at all, so "should we keep asking?"
        has no answer to give. Reporting True would put a fulfilled need into
        the suppressed section of the prompt, telling the model to dispose of
        something already disposed of."""
        assert is_ask_exhausted(_need(asked_on=[1, 2], state=state), 9) is False

    def test_a_need_that_was_never_asked_is_not_exhausted(self):
        """A need can be authored and matched against uploads without ever being
        surfaced — it has no ask history to be exhausted by."""
        assert is_ask_exhausted(_need(asked_on=[]), 40) is False


# ============================================================
# The seam: the ask is not shipped
# ============================================================


class TestTheAskIsWithheld:
    def test_the_repeat_is_dropped_from_the_turn(self):
        case = _case(turn=10)
        _need(case, asked_on=[5, 6])
        follow_ups = [_FollowUp(body="target-account OIDC provider record")]

        link_evidence_suggestions_to_needs(case, follow_ups, {}, 10, _resolve)

        assert follow_ups == []

    def test_the_withheld_ask_is_not_recorded_as_asked(self):
        """The rendered ``asked N×`` is stated to the model as fact. Counting an
        ask the engine itself discarded would make that fact false, and would let
        a suppressed need inflate its own count forever."""
        case = _case(turn=10)
        need = _need(case, asked_on=[5, 6])

        link_evidence_suggestions_to_needs(
            case,
            [_FollowUp(body="target-account OIDC provider record")],
            {},
            10,
            _resolve,
        )

        assert need.surfaced_turns == [5, 6]

    def test_the_need_itself_is_untouched(self):
        """Suppression must not look like a conclusion. Nothing that
        ``verification_status`` reads may move, or the engine would be walling
        the case on "the model repeated itself" rather than on "the data cannot
        be had"."""
        case = _case(turn=10)
        need = _need(case, asked_on=[5, 6])

        link_evidence_suggestions_to_needs(
            case,
            [_FollowUp(body="target-account OIDC provider record")],
            {},
            10,
            _resolve,
        )

        assert need.state == NeedState.PENDING
        assert need.obtainability == NeedObtainability.UNKNOWN
        assert need.superseded_reason is None
        assert need.is_outstanding is True

    def test_a_fresh_ask_on_the_same_turn_still_ships(self):
        """Suppression is per-need. A turn that repeats one exhausted ask and
        raises a genuinely new one must still deliver the new one."""
        case = _case(turn=10)
        _need(case, asked_on=[5, 6])
        stale = _FollowUp(body="target-account OIDC provider record")
        fresh = _FollowUp(body="pod restart counts in the payments namespace")
        follow_ups = [stale, fresh]

        link_evidence_suggestions_to_needs(case, follow_ups, {}, 10, _resolve)

        assert follow_ups == [fresh]
        assert fresh.evidence_need_id is not None

    def test_non_evidence_suggestions_are_never_dropped(self):
        """The filter runs over the whole list; only EVIDENCE asks are its
        business."""
        case = _case(turn=10)
        _need(case, asked_on=[5, 6])
        run = _FollowUp(action_type="RUN", body="aws iam get-role ...")
        speech = _FollowUp(action_type="FREE_SPEECH", body="What else can I try?")
        follow_ups = [
            run,
            _FollowUp(body="target-account OIDC provider record"),
            speech,
        ]

        link_evidence_suggestions_to_needs(case, follow_ups, {}, 10, _resolve)

        assert follow_ups == [run, speech]

    def test_a_model_declared_link_does_not_exempt_the_ask(self):
        """Declaring ``evidence_need_id`` correctly is the intended path, not a
        bypass — the loop this closes was a model re-asking with full knowledge
        of the need."""
        case = _case(turn=10)
        need = _need(case, asked_on=[5, 6])
        follow_ups = [_FollowUp(body="wholly different phrasing", need_id=need.need_id)]

        link_evidence_suggestions_to_needs(case, follow_ups, {}, 10, _resolve)

        assert follow_ups == []


# ============================================================
# The surface cap: an exhausted ask yields its slot
# ============================================================


class TestExhaustedNeedsYieldTheirSurfaceSlot:
    def test_a_live_ask_takes_the_slot_an_exhausted_one_would_have_held(self):
        """The rotation shows at most ``_SURFACED_CAUSAL_CAP`` causal asks. A
        need nobody is asking for must not hold one of them — the same argument
        that already excludes UNOBTAINABLE needs."""
        case = _case(turn=10)
        for i in range(_SURFACED_CAUSAL_CAP):
            _need(case, asked_on=[5, 6], request_text=f"exhausted ask {i}")
        live = _need(case, asked_on=[10], request_text="freshly raised ask")

        assert select_surfaced_causal_needs(case) == [live]


# ============================================================
# The prompt block: reported as suppressed, not as pending
# ============================================================


class TestTheBlockReportsTheSuppression:
    def test_an_exhausted_ask_leaves_the_outstanding_list(self):
        case = _case(turn=10)
        _need(case, asked_on=[5, 6], request_text="target-account provider record")
        block = _build_evidence_needs_block(case)

        outstanding_section = block.split("STOPPED surfacing")[0]
        assert "target-account provider record" not in outstanding_section

    def test_an_exhausted_ask_is_named_under_the_suppressed_heading(self):
        """It has to stay visible. The pool is keyed on ``request_text``, so a
        need the model cannot see is a need it re-authors — and the duplicate
        arrives with an empty ask history, resetting the counter the suppression
        is computed from."""
        case = _case(turn=10)
        need = _need(
            case,
            asked_on=[5, 6],
            request_text="target-account provider record",
            motivating_hypothesis_ids=["hyp_000000000001"],
        )
        block = _build_evidence_needs_block(case)

        suppressed_section = block.split("STOPPED surfacing")[1]
        assert need.need_id in suppressed_section
        assert "target-account provider record" in suppressed_section
        assert "hyp_000000000001" in suppressed_section

    def test_the_block_renders_when_every_ask_is_exhausted(self):
        """Progressive activation returns "" for an empty pool. A pool of
        nothing but suppressed asks is not empty — those are exactly the ones
        the model has to dispose of."""
        case = _case(turn=10)
        _need(case, asked_on=[5, 6])

        assert "STOPPED surfacing" in _build_evidence_needs_block(case)

    def test_an_exhausted_ask_is_not_counted_as_hidden_demand(self):
        """The "…and N more not shown" notice exists so the model never reads
        the visible list as the whole demand. A suppressed ask is shown — in its
        own section — so counting it there would double-report it and nudge the
        model to chase what the engine just withheld."""
        case = _case(turn=10)
        for i in range(6):
            _need(case, asked_on=[5, 6], request_text=f"exhausted ask {i}")
        block = _build_evidence_needs_block(case)

        assert "more outstanding need(s) not shown" not in block

    def test_a_live_ask_is_still_rendered_normally(self):
        """Guard the other direction: the section must not swallow the ordinary
        case."""
        case = _case(turn=10)
        live = _need(case, asked_on=[10], request_text="pod restart counts")
        block = _build_evidence_needs_block(case)

        assert "Outstanding needs" in block
        assert live.need_id in block.split("Outstanding needs")[1]
        assert "STOPPED surfacing" not in block
