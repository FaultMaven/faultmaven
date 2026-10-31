"""Two shapes the offer record fixes, through real turns (#2379, #2408).

#2379 state 1: a resolvable case whose resolution the user declined, then held
on a revised problem statement. ``closed`` read ``suggests_alternative`` there,
so the status menu hid Close while nothing the user could answer offered a
resolve. Close shows; choosing it withdraws the revision and offers the close
as ``resolution_declined``, and confirming it closes the case. Close is hidden
only while a resolve offer is open (ruling 6).

#2408: a model ``resolved`` the case had not earned (NEEDS_INFO) stored a
pending resolve with confirmation chips under a reply asking for more. It is a
request, not an offer (ruling 1): nothing opens, no chips show, Close stays in
the menu, and the ask goes below the reply. The next turn reads the request
once: READY offers the resolve, still NEEDS_INFO offers the close.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    derive_disposition_eligibility,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    CaseState,
    ClosureReason,
    ProblemStatus,
)
from tests.unit.core.investigation.test_offer_record_declines_bind_2379 import (
    _DSU,
    CLOSE_PAIR,
    RESOLVE_PAIR,
    _base_case,
    _declined_resolve,
    _engine,
    _labels,
    _proposes,
    _resolvable_case,
    _revision,
    _to_add,
    _turn,
)

pytestmark = pytest.mark.unit

#: The status menu's Close, as the copilot sends it.
_MENU_CLOSE = {"from_state": "investigating", "to_state": "closed"}


def _confirmation_keys(result) -> list:
    return [
        (f.get("intent") or {}).get("proposal_id")
        for f in result["suggested_follow_ups"]
        if (f.get("intent") or {}).get("type") == "confirmation"
    ]


def _closed(case) -> str:
    return derive_disposition_eligibility(case)["closed"]


class TestCloseUnderAPendingRevision:
    async def test_close_shows_and_closes_as_resolution_declined(self):
        engine, case = await _declined_resolve()
        await _revision(engine, case)
        assert case.offer_record.open.kind == "revision", "premise"
        assert _closed(case) == "ready"

        offered = await _turn(
            engine,
            case,
            "Close this case as unresolved. Summarize what we found so far.",
            intent_type="status_transition",
            intent_data=_MENU_CLOSE,
        )

        # The revision is withdrawn and the status it held returns.
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        revision_answer = next(
            a for a in case.offer_record.answers if a.kind == "revision"
        )
        assert revision_answer.answer == AnswerKind.WITHDRAWN
        offer = case.offer_record.open
        assert offer.kind == "close"
        assert offer.closure_reason == ClosureReason.RESOLUTION_DECLINED
        assert _labels(offered) == CLOSE_PAIR

        key = _confirmation_keys(offered)[0]
        await _turn(
            engine,
            case,
            "Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": key},
        )
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == ClosureReason.RESOLUTION_DECLINED

    async def test_close_is_hidden_beside_an_open_resolve_offer(self):
        case, engine = _resolvable_case(), _engine()
        offered = await _turn(engine, case, "the errors are gone now")

        assert _labels(offered) == RESOLVE_PAIR
        assert _closed(case) == "suggests_alternative"


def _needs_info_case():
    case = _base_case()
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO, "premise"
    return case, readiness.message


async def _asked(engine, case) -> dict:
    """A model ``resolved`` on NEEDS_INFO: the request turn."""
    return await _turn(engine, case, "the fix is in", _proposes("resolved"))


class TestANeedsInfoProposalIsARequest:
    async def test_it_opens_nothing_and_asks_below_the_reply(self):
        case, message = _needs_info_case()
        engine = _engine()

        result = await _asked(engine, case)

        assert case.offer_record.open is None
        assert case.offer_record.resolve_request is not None
        assert _confirmation_keys(result) == []
        assert not set(RESOLVE_PAIR) & set(_labels(result))
        assert derive_disposition_eligibility(case) == {
            "resolved": "needs_info",
            "closed": "ready",
        }
        reply = result["agent_response"]
        assert reply.startswith("Noted.")
        assert message in reply

    async def test_the_next_turn_offers_the_resolve_once_it_is_earned(self):
        case, _ = _needs_info_case()
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(
            engine,
            case,
            "the overnight batch ran clean",
            _DSU(
                evidence_to_add=[
                    _to_add(
                        "causal_absence_evidence",
                        "The provider's ClientIDList now reads sts.amazonaws.com.",
                    ),
                    _to_add(
                        "symptom_absence_evidence",
                        "Zero AssumeRole failures in the overnight batch.",
                    ),
                ]
            ),
        )

        assert case.offer_record.open.kind == "resolve"
        assert _labels(result) == RESOLVE_PAIR
        assert case.offer_record.resolve_request is None

    async def test_the_next_turn_still_short_offers_the_close(self):
        case, _ = _needs_info_case()
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(engine, case, "we have not checked anything else")

        offer = case.offer_record.open
        assert offer.kind == "close"
        assert offer.basis == "needs_info_unmet"
        assert _labels(result) == CLOSE_PAIR
        assert case.offer_record.resolve_request is None
