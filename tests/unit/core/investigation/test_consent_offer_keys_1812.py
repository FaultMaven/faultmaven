"""The consent seam: a click answers only the offer it names (#1812), Gate 1
reads typed consent through the bare screen (#1794), a consent-opener is no
deflection (#1808), a typed decline is bare (#1813), and the re-ask says what
a typed confirmation must look like (#1814).

Driven through ``MilestoneEngine.process_turn``: the gate's answer is the
engine's, and a direct call to a helper proves nothing about where it is called
from. Only the LLM seam is doubled. Where the turn must not reach it, the
double raises; where it must, the double answers and is asserted awaited.

The invariant these pin, from the plan:

* A confirmation click commits only the offer standing when it arrives, and
  only when it names that offer. Any other click executes nothing, withdraws
  nothing and records nothing. It re-shows the standing offer, or says none is
  open.
* Gate 1 commits only on its current click, or on a turn whose typed text is
  bare consent.
* A typed reply is recorded as a refusal only when it is one bare decline
  token, or an escape-lane deflection carrying no ``?`` that does not open with
  a consent token. A minted decline on question-free text still declines.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.milestone_engine.engine as engine_module
import faultmaven.core.investigation.milestone_engine.response_application as response_application
import faultmaven.core.investigation.milestone_engine.transition_turns as transition_turns
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _gate1_statement_presentation,
    _investigation_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    _narration_asserts_disposition,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _close_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    TYPED_CONFIRMATION_LINE,
    _consent_prefix,
    gate1_offer_key,
    offer_click_refusal,
    terminal_offer_key,
)
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    STALE_OFFER_LINE,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.core.investigation.terminal_transitions import (
    cancel_pending_transition,
    propose_transition,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.conclusion import (
    ConfidenceLevel,
    RootCauseConclusion,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType

pytestmark = pytest.mark.unit

STATEMENT = "Checkout API returns 503 for all users since 14:00 UTC"
REVISED = "Checkout API returns 503 for EU users only since 14:00 UTC"
SIGNATURE = "SUGGEST_CLOSE|1|chain"
REPLY = "Here is what the logs say about the 503s."


class _SeamReached(Exception):
    """Raised by the doubled LLM seam: reaching it proves the turn fell through."""


def _repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine(response=None) -> MilestoneEngine:
    """An engine whose LLM seam answers ``response``, or raises when None."""
    engine = MilestoneEngine(MagicMock(), _repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=response, side_effect=None if response else _SeamReached()
    )
    return engine


def _investigating(*, cause: bool = False, absence: bool = False) -> Case:
    """An INVESTIGATING case; with ``cause``, a root cause and fix on record;
    with ``absence`` too, a confirmed elimination, which makes it resolvable
    (SUGGEST_RESOLVE, and READY)."""
    case = Case(
        case_id="case_1812aaaaaaaa",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1812",
        enterprise_id="org_1812",
        description="checkout 503s",
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 503",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = STATEMENT
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 5
    if cause:
        case.progress.symptom_verified = True
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="The checkout pool's max connections was lowered to 5.",
            mechanism="Requests queue past the gateway timeout and return 503.",
            confidence_level=ConfidenceLevel.CONFIDENT,
            likelihood=0.85,
        )
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Restore the checkout pool size",
                longterm_fix="Set max connections back to 50.",
            )
        ]
    if absence:
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                primary_purpose="confirm the cause was eliminated",
                summary="After the pool fix the 503s stopped and did not return.",
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="user",
                collected_at_turn=1,
            )
        )
    return case


def _pending(to_state: str = "resolved", **kw) -> Case:
    """An INVESTIGATING case with a signed terminal offer standing, so a
    recorded refusal is visible and "nothing recorded" is not vacuous."""
    case = _investigating(**kw)
    propose_transition(case, to_state=to_state, summary=f"Shall I {to_state} it?")
    case.pending_transition["justifying_signature"] = SIGNATURE
    return case


def _gate1_case(statement: str = STATEMENT) -> Case:
    """An INQUIRY case whose statement stood before this turn: Gate 1 pending."""
    case = Case(
        case_id="case_1812bbbbbbbb",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1812",
        enterprise_id="org_1812",
        description="",
    )
    case.inquiry.proposed_problem_statement = statement
    case.inquiry.problem_statement_confirmed = False
    case.current_turn = 2
    return case


def _click(value: bool, key) -> dict:
    """``intent_data`` for a confirmation card clicked: its value, and the key
    of the offer the card named (None: a card that named none)."""
    data = {"value": value}
    if key is not None:
        data["proposal_id"] = key
    return data


def _keys(follow_ups) -> list:
    return [(f.get("intent") or {}).get("proposal_id") for f in follow_ups]


def _investigation_reply():
    return InvestigationResponse_Diagnosis(agent_response=REPLY, state_updates={})


@pytest.fixture
def refused():
    with patch.object(transition_turns, "confirmation_click_refused_total") as counter:
        yield counter


async def _turn(engine, case, message="", **kw):
    return await engine.process_turn(case=case, user_message=message, **kw)


def _assert_refused_on_terminal(result, case, before: dict):
    """Nothing executed, nothing withdrawn, nothing recorded; the reply names
    the earlier offer and re-shows the standing one with its current key."""
    assert case.state == CaseState.INVESTIGATING
    assert case.pending_transition == before, "the standing offer moved"
    assert case.progress.deferred_disposition_declined_signatures == []
    assert result["agent_response"].startswith(f"{STALE_OFFER_LINE}\n\n")
    assert "Please select one of the options above" in result["agent_response"]
    assert _keys(result["suggested_follow_ups"]) == [before["proposed_at"]] * 2


# =============================================================================
# The keys
# =============================================================================


class TestTheOfferKeys:
    def test_a_terminal_offer_is_named_by_its_proposal(self):
        case = _investigating()
        propose_transition(case, to_state="resolved", summary="r")
        first = terminal_offer_key(case.pending_transition)
        assert first == case.pending_transition["proposed_at"]
        cancel_pending_transition(case)
        propose_transition(case, to_state="resolved", summary="r")
        assert (
            terminal_offer_key(case.pending_transition) != first
        ), "a re-proposal is a new offer; its key must be fresh"
        assert terminal_offer_key(None) is None

    def test_a_gate1_offer_is_named_by_its_wording(self):
        key = gate1_offer_key(STATEMENT)
        assert key.startswith("gate1:") and len(key) == 22
        assert gate1_offer_key(f"  {STATEMENT}\n") == key, "only the ends are stripped"
        assert gate1_offer_key(REVISED) != key, "a revision is a new offer"

    @pytest.mark.parametrize(
        "intent_data, standing, expected",
        [
            ({"value": True, "proposal_id": "k"}, "k", None),
            ({"value": True, "proposal_id": "old"}, "k", "stale"),
            ({"value": True}, "k", "untargeted"),
            ({"value": True, "proposal_id": ""}, "k", "untargeted"),
            ({"value": True, "proposal_id": None}, None, "untargeted"),
            ({"value": True, "proposal_id": "k"}, None, "stale"),
            (None, "k", "untargeted"),
        ],
    )
    def test_the_refusal_reason(self, intent_data, standing, expected):
        assert offer_click_refusal(intent_data, standing) == expected

    def test_both_cards_of_every_pair_carry_the_standing_key(self):
        case = _pending("resolved")
        assert (
            _keys(_resolution_confirmation_suggestions(case))
            == [case.pending_transition["proposed_at"]] * 2
        )
        case = _pending("closed")
        assert (
            _keys(_close_confirmation_suggestions(case))
            == [case.pending_transition["proposed_at"]] * 2
        )
        assert (
            _keys(_investigation_confirmation_suggestions(_gate1_case()))
            == [gate1_offer_key(STATEMENT)] * 2
        )

    def test_a_pair_built_with_no_offer_standing_carries_no_key_and_says_so(
        self, caplog
    ):
        """The safe direction: never a 500, and a click on it is refused."""
        with caplog.at_level("ERROR"):
            pair = _resolution_confirmation_suggestions(_investigating())
        assert _keys(pair) == [None, None]
        assert "confirmation_pair_without_offer" in caplog.text


# =============================================================================
# #1812: a click answers only the offer it names
# =============================================================================


class TestAClickAnswersOnlyTheOfferItNames:
    async def test_k1_the_current_key_executes(self, refused):
        case = _pending("resolved")
        engine = _engine()
        await _turn(
            engine,
            case,
            "Yes, the issue is resolved.",
            intent_type="confirmation",
            intent_data=_click(True, terminal_offer_key(case.pending_transition)),
        )
        assert case.state == CaseState.RESOLVED
        assert case.turn_history[-1].terminal_confirmed_via == "intent"
        refused.labels.assert_not_called()

    @pytest.mark.parametrize("value", [True, False])
    async def test_k2_a_stale_key_executes_nothing(self, refused, value):
        case = _pending("resolved")
        before = dict(case.pending_transition)
        engine = _engine()
        result = await _turn(
            engine,
            case,
            "Yes, the issue is resolved.",
            intent_type="confirmation",
            intent_data=_click(value, "2026-01-01T00:00:00+00:00"),
        )
        _assert_refused_on_terminal(result, case, before)
        refused.labels.assert_called_once_with(gate="terminal", reason="stale")
        engine.generator.generate_structured_output.assert_not_awaited()

    @pytest.mark.parametrize("value", [True, False])
    async def test_k3_a_click_naming_no_offer_executes_nothing(self, refused, value):
        """A card rendered before the keys shipped, or Copilot's marker-built
        buttons (faultmaven-copilot#291)."""
        case = _pending("closed")
        before = dict(case.pending_transition)
        result = await _turn(
            _engine(),
            case,
            "Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data=_click(value, None),
        )
        _assert_refused_on_terminal(result, case, before)
        refused.labels.assert_called_once_with(gate="terminal", reason="untargeted")

    async def test_k4_a_gate1_card_on_a_pending_close_closes_nothing(self, refused):
        """The defeat pass's ``stale_gate1_yes_on_pending_close``."""
        case = _gate1_case()
        propose_transition(case, to_state="closed", summary="Close it?")
        before = dict(case.pending_transition)
        gate1_yes = _investigation_confirmation_suggestions(case)[0]["intent"]

        result = await _turn(
            _engine(),
            case,
            "Yes, that's correct. Let's investigate.",
            intent_type="confirmation",
            intent_data=_click(True, gate1_yes["proposal_id"]),
        )

        assert case.state == CaseState.INQUIRY
        assert case.pending_transition == before
        assert case.inquiry.problem_statement_confirmed is False
        assert result["agent_response"].startswith(STALE_OFFER_LINE)
        assert _keys(result["suggested_follow_ups"]) == [before["proposed_at"]] * 2
        refused.labels.assert_called_once_with(gate="terminal", reason="stale")

    async def test_k5_a_withdrawn_resolve_card_does_not_close_the_case(self, refused):
        """The issue's scenario: a RESOLVE offer is withdrawn, a CLOSE is
        proposed later, and the old "Yes, mark as resolved" is clicked."""
        case = _investigating()
        propose_transition(case, to_state="resolved", summary="Resolve it?")
        old_yes = _resolution_confirmation_suggestions(case)[0]["intent"]
        cancel_pending_transition(case)
        propose_transition(case, to_state="closed", summary="Close it?")
        assert old_yes["proposal_id"] != terminal_offer_key(case.pending_transition)
        before = dict(case.pending_transition)

        result = await _turn(
            _engine(),
            case,
            "Yes, the issue is resolved.",
            intent_type="confirmation",
            intent_data=_click(True, old_yes["proposal_id"]),
        )

        assert case.state == CaseState.INVESTIGATING, "the stale card closed the case"
        assert case.pending_transition == before
        assert result["agent_response"].startswith(STALE_OFFER_LINE)
        refused.labels.assert_called_once_with(gate="terminal", reason="stale")

    async def test_k6_a_terminal_card_on_gate1_reshows_gate1_and_counts_inv01(
        self, refused
    ):
        case = _gate1_case()
        with (
            patch.object(
                transition_turns, "engine_owned_affordance_served_total"
            ) as served,
            patch.object(
                transition_turns, "gate1_statement_composed_total"
            ) as composed,
        ):
            result = await _turn(
                _engine(),
                case,
                "Yes, the issue is resolved.",
                intent_type="confirmation",
                intent_data=_click(True, "2026-01-01T00:00:00+00:00"),
            )

        assert case.inquiry.problem_statement_confirmed is False
        assert case.state == CaseState.INQUIRY
        assert result["agent_response"] == (
            f"{STALE_OFFER_LINE}\n\n{_gate1_statement_presentation(case)}"
        )
        assert STATEMENT in result["agent_response"]
        assert _keys(result["suggested_follow_ups"]) == [gate1_offer_key(STATEMENT)] * 2
        refused.labels.assert_called_once_with(gate="gate1", reason="stale")
        # INV-01's pair, one for one.
        served.labels.assert_called_once_with(gate="gate1")
        served.labels.return_value.inc.assert_called_once()
        composed.inc.assert_called_once()

    async def test_k7_a_gate1_card_for_a_revised_statement_is_refused(self, refused):
        case = _gate1_case(REVISED)
        result = await _turn(
            _engine(),
            case,
            "Yes, that's correct. Let's investigate.",
            intent_type="confirmation",
            intent_data=_click(True, gate1_offer_key(STATEMENT)),
        )
        assert case.inquiry.problem_statement_confirmed is False
        assert REVISED in result["agent_response"]
        assert _keys(result["suggested_follow_ups"]) == [gate1_offer_key(REVISED)] * 2
        refused.labels.assert_called_once_with(gate="gate1", reason="stale")

    async def test_k8_the_current_gate1_click_commits_and_refuses_nothing(
        self, refused
    ):
        """The click commits at 0c, before the LLM. The LLM is then handed the
        card's payload, which is not bare, and reads it honestly as a
        confirmation: that turn refused nothing, so ``not_bare`` must not
        move (``not problem_statement_confirmed`` gates it)."""
        case = _gate1_case()
        engine = _engine(
            InquiryResponse(
                agent_response="Starting the investigation.",
                state_updates={"user_confirmed_investigation": True},
            )
        )
        with patch.object(
            response_application, "inquiry_handshake_deferred_total"
        ) as deferred:
            await _turn(
                engine,
                case,
                "Yes, that's correct. Let's investigate.",
                intent_type="confirmation",
                intent_data=_click(True, gate1_offer_key(STATEMENT)),
            )
        assert case.inquiry.problem_statement_confirmed is True
        assert case.state == CaseState.INVESTIGATING
        deferred.labels.assert_not_called()
        refused.labels.assert_not_called()

    async def test_k9_a_stale_no_withdraws_and_records_nothing(self, refused):
        case = _pending("closed")
        before = dict(case.pending_transition)
        result = await _turn(
            _engine(),
            case,
            "Not yet — I'd like to continue investigating.",
            intent_type="confirmation",
            intent_data=_click(False, "2026-01-01T00:00:00+00:00"),
        )
        _assert_refused_on_terminal(result, case, before)

    @pytest.mark.parametrize("shape", ["investigating", "inquiry-without-statement"])
    async def test_k10_a_click_with_nothing_standing_is_the_line_alone(
        self, refused, shape
    ):
        if shape == "investigating":
            case = _investigating()
        else:
            case = _gate1_case()
            case.inquiry.proposed_problem_statement = None
        engine = _engine()
        result = await _turn(
            engine,
            case,
            "Yes",
            intent_type="confirmation",
            intent_data=_click(True, "2026-01-01T00:00:00+00:00"),
        )
        assert result["agent_response"] == STALE_OFFER_LINE
        assert result["suggested_follow_ups"] == []
        engine.generator.generate_structured_output.assert_not_awaited()
        refused.labels.assert_called_once_with(gate="none", reason="stale")

    async def test_k11_the_close_card_does_not_confirm_its_resolve_pivot(self, refused):
        """INV-37: the close click on a resolvable case pivots to a RESOLVED
        offer. That is a new offer, and the close card does not name it."""
        case = _pending("closed", cause=True, absence=True)
        close_key = terminal_offer_key(case.pending_transition)
        engine = _engine()

        pivot = await _turn(
            engine,
            case,
            "Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data=_click(True, close_key),
        )
        assert case.pending_transition["to_state"] == "resolved"
        resolve_key = terminal_offer_key(case.pending_transition)
        assert resolve_key != close_key
        assert _keys(pivot["suggested_follow_ups"]) == [resolve_key] * 2

        again = await _turn(
            engine,
            case,
            "Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data=_click(True, close_key),
        )
        assert (
            case.state == CaseState.INVESTIGATING
        ), "the close card executed the pivot"
        assert again["agent_response"].startswith(STALE_OFFER_LINE)
        refused.labels.assert_called_once_with(gate="terminal", reason="stale")

    async def test_k12_a_typed_bare_yes_minted_with_a_stale_key_executes(self, refused):
        """A minted intent is not a click: the text decides, and a bare "yes"
        is consent to whatever stands, whichever card the resolver matched."""
        case = _pending("resolved")
        await _turn(
            _engine(),
            case,
            "yes",
            intent_type="confirmation",
            intent_data=_click(True, "2026-01-01T00:00:00+00:00"),
            typed=True,
        )
        assert case.state == CaseState.RESOLVED
        assert case.turn_history[-1].terminal_confirmed_via == "explicit_token"
        refused.labels.assert_not_called()

    @pytest.mark.parametrize("where", ["investigating", "inquiry-gate1-pending"])
    async def test_k15_an_in_key_not_yet_click_is_answered_by_the_gate(
        self, refused, where
    ):
        """0b consumed it: the refusal is recorded and the offer withdrawn, and
        the substantive payload falls through to 0c, which must NOT refuse it
        there. The turn reaches the LLM and Gate 1 is untouched."""
        if where == "investigating":
            case = _pending("closed")
            engine = _engine(_investigation_reply())
        else:
            case = _gate1_case()
            propose_transition(case, to_state="closed", summary="Close it?")
            case.pending_transition["justifying_signature"] = SIGNATURE
            engine = _engine(InquiryResponse(agent_response=REPLY, state_updates={}))
        key = terminal_offer_key(case.pending_transition)

        result = await _turn(
            engine,
            case,
            "Not yet — I'd like to continue investigating.",
            intent_type="confirmation",
            intent_data=_click(False, key),
        )

        assert case.pending_transition is None, "the declined offer was not withdrawn"
        assert case.progress.deferred_disposition_declined_signatures == [SIGNATURE]
        engine.generator.generate_structured_output.assert_awaited()
        assert STALE_OFFER_LINE not in result["agent_response"]
        refused.labels.assert_not_called()
        if where != "investigating":
            assert case.inquiry.problem_statement_confirmed is False
            assert case.inquiry.proposed_problem_statement == STATEMENT

    async def test_a_refused_click_carrying_an_upload_records_the_upload(self, refused):
        """A stale or untargeted click is refused deterministically even when
        the turn carries a file, as today's click-confirm path consumes one.
        Nothing executes or is withdrawn, and the upload's reading is still
        recorded on the turn (``_finish_deterministic_turn``)."""
        case = _pending("resolved")
        before = dict(case.pending_transition)
        upload = [
            {
                "file_id": "file_1812aaaaaaaa",
                "filename": "app.log",
                "data_type": "log",
                "is_novel": True,
            }
        ]
        result = await _turn(
            _engine(),
            case,
            "Yes, the issue is resolved.",
            attachments=upload,
            intent_type="confirmation",
            intent_data=_click(True, None),
        )
        _assert_refused_on_terminal(result, case, before)
        assert result["metadata"]["novel_files_uploaded"] == ["file_1812aaaaaaaa"]
        assert case.turn_history[-1].progress_made is True
        refused.labels.assert_called_once_with(gate="terminal", reason="untargeted")


# =============================================================================
# #1794: Gate 1 reads a minted confirmation through the bare screen (0c)
# =============================================================================


class TestAMintedGate1ConfirmationIsScreened:
    @pytest.mark.parametrize("message", ["yes please", "ok, don't start yet"])
    async def test_a_mint_on_text_that_is_not_bare_commits_nothing(self, message):
        case = _gate1_case()
        engine = _engine(InquiryResponse(agent_response=REPLY, state_updates={}))
        with patch.object(
            engine_module, "inquiry_handshake_deferred_total"
        ) as deferred:
            result = await _turn(
                engine,
                case,
                message,
                intent_type="confirmation",
                intent_data=_click(True, gate1_offer_key(STATEMENT)),
                typed=True,
            )
        assert case.inquiry.problem_statement_confirmed is False
        assert case.state == CaseState.INQUIRY
        deferred.labels.assert_called_once_with(reason="not_bare")
        engine.generator.generate_structured_output.assert_awaited()
        # Gate 1 stays pending, so the engine composes its card (#1607).
        assert STATEMENT in result["agent_response"]

    async def test_a_mint_on_a_bare_token_commits(self):
        case = _gate1_case()
        engine = _engine(InquiryResponse(agent_response=REPLY, state_updates={}))
        with patch.object(
            engine_module, "inquiry_handshake_deferred_total"
        ) as deferred:
            await _turn(
                engine,
                case,
                "yes",
                intent_type="confirmation",
                intent_data=_click(True, "gate1:stale0000000000"),
                typed=True,
            )
        assert case.inquiry.problem_statement_confirmed is True
        assert case.state == CaseState.INVESTIGATING
        deferred.labels.assert_not_called()


# =============================================================================
# #1808: a reply that opens with consent is no deflection
# =============================================================================

LONG_CONSENT = (
    "Yes, go ahead and close it. We verified the fix in staging and in prod "
    "overnight and the error has not come back since."
)
EVIDENCE = (
    "ok so here's what I found: the pod restarted at 3pm and the logs show OOM "
    "kills at the same time, memory limit 512Mi"
)


class TestAConsentOpenerIsNoDeflection:
    @pytest.mark.parametrize(
        "message",
        [
            LONG_CONSENT,
            EVIDENCE,
            "ok but we need to wait for the weekend soak first",
            "sure thing but hold off until the change window closes",
        ],
    )
    async def test_withdrawn_and_processed_and_nothing_recorded(self, message):
        case = _pending("closed")
        engine = _engine()
        with pytest.raises(MilestoneEngineError):
            await _turn(engine, case, message)
        assert case.pending_transition is None
        engine.generator.generate_structured_output.assert_awaited()
        assert case.progress.deferred_disposition_declined_signatures == []

    @pytest.mark.parametrize(
        "message",
        [
            "we will do it in friday's maintenance window instead of now",
            "no problem, go ahead and close it, the fix held overnight in prod",
            "yesterday we rolled it back and it is fine now ok",
        ],
    )
    async def test_a_deflection_still_records_the_refusal(self, message):
        """The control: no consent opens these, so the fm#1122 rule stands."""
        assert not _consent_prefix(message)
        case = _pending("closed")
        with pytest.raises(MilestoneEngineError):
            await _turn(_engine(), case, message)
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == [SIGNATURE]

    @pytest.mark.parametrize(
        "message",
        [
            "👍 go ahead and close it, I verified the fix in prod today",
            "okay-ish, still seeing some errors in the logs every hour",
        ],
    )
    async def test_a_short_consent_opener_is_re_asked(self, message):
        """Consent-openers of 41-100 characters with no ``?`` or " but " are
        consent-SHAPED, so the gate re-asks them (#1783) rather than withdraw
        them. The emoji-first shape noted on #1808 joins them: with the
        decoration treated first it opens with "go ahead". "okay-ish" opens
        with "okay" through the word boundary, the ruling's stated cost."""
        assert _consent_prefix(message)
        case = _pending("closed")
        before = dict(case.pending_transition)
        result = await _turn(_engine(), case, message)
        assert case.pending_transition == before
        assert "Please select one of the options above" in result["agent_response"]
        assert case.progress.deferred_disposition_declined_signatures == []


# =============================================================================
# #1813: a typed decline is bare; a minted decline on a question is no refusal
# =============================================================================


class TestADeclineIsBare:
    @pytest.mark.parametrize("message", ["no problem, go ahead", "no worries"])
    async def test_a_positive_reply_opening_with_no_is_re_asked(self, message):
        case = _pending("closed")
        before = dict(case.pending_transition)
        result = await _turn(_engine(), case, message)
        assert case.pending_transition == before
        assert "Please select one of the options above" in result["agent_response"]
        assert case.progress.deferred_disposition_declined_signatures == []

    async def test_a_minted_decline_on_a_question_withdraws_and_records_nothing(
        self,
    ):
        case = _pending("closed")
        with pytest.raises(MilestoneEngineError):
            await _turn(
                _engine(),
                case,
                "can we hold off until friday's change window?",
                intent_type="confirmation",
                intent_data={"value": False},
                typed=True,
            )
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == []

    async def test_a_minted_decline_on_question_free_text_still_declines(self):
        case = _pending("closed")
        result = await _turn(
            _engine(),
            case,
            "not now thanks",
            intent_type="confirmation",
            intent_data={"value": False},
            typed=True,
        )
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == [SIGNATURE]
        assert "remains open" in result["agent_response"]

    async def test_a_bare_typed_decline_still_declines(self):
        case = _pending("closed")
        result = await _turn(_engine(), case, "Not yet.")
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == [SIGNATURE]
        assert "remains open" in result["agent_response"]


# =============================================================================
# #1814: the re-ask says what a typed confirmation must look like
# =============================================================================


class TestTheReaskSaysHowToConfirm:
    @pytest.mark.parametrize("to_state", ["resolved", "closed"])
    async def test_the_terminal_re_ask_carries_the_line(self, to_state):
        case = _pending(to_state)
        result = await _turn(_engine(), case, "yes please")
        text = result["agent_response"]
        assert "Please select one of the options above to continue." in text
        assert text.endswith(
            "Please select one of the options above to continue.\n\n"
            f"{TYPED_CONFIRMATION_LINE}"
        )

    def test_gate1_presentation_carries_the_line(self):
        assert _gate1_statement_presentation(_gate1_case()).endswith(
            f"\n\n{TYPED_CONFIRMATION_LINE}"
        )

    def test_the_line_has_no_quote_marks_and_claims_no_disposition(self):
        """The grammar rejects a quoted token, and INV-40's completion-phrase
        scan must not fire on the engine's own copy."""
        assert '"' not in TYPED_CONFIRMATION_LINE
        assert "'" not in TYPED_CONFIRMATION_LINE
        assert not _narration_asserts_disposition(TYPED_CONFIRMATION_LINE)
        assert not _narration_asserts_disposition(STALE_OFFER_LINE)
        assert not _narration_asserts_disposition(
            _gate1_statement_presentation(_gate1_case())
        )
