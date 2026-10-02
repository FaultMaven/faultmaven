"""The consent seam's second pass: #1838, #1839, #1840 and #1841.

The invariant these pin, from the plan:

* An irreversible terminal transition executes only on its own card's click,
  or on a typed reply that is a bare consent token. A status-dropdown re-pick
  of the pending target re-shows the card (#1838), and no LLM-written DECIDE
  card sends a text the gate reads as a bare reply (#1839).
* A reply that opens with consent, or that carries a question in any script,
  is never recorded as a refusal (#1840). The gate itself reads strictly, so a
  reply it does not recognise as typed reaches the LLM (#1840 review).
* A bare typed consent commits a pending Gate 1 without the LLM (#1841).
* #1783's must-execute and must-not-execute corpora are unchanged
  (``test_terminal_confirmation_counters_1748.py``).

The grammar rows are unit tables over ``transition_consent``. Everything else
is driven through ``MilestoneEngine.process_turn`` with only the LLM seam
doubled: where the turn must not reach it, the double raises.

Every non-ASCII character is spelled with a ``\\N{...}`` escape, so what each
row carries is visible in the source.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.milestone_engine.engine as engine_module
import faultmaven.core.investigation.milestone_engine.turn_records as turn_records
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _DECLINE_TOKENS,
    _EXPLICIT_CONFIRM_TOKENS,
    _WEAK_CONFIRM_TOKENS,
    _consent_prefix,
    card_reads_as_bare_reply,
    confirmation_token_class,
    gate1_bare_consent,
    gate1_offer_key,
    is_bare_gate_reply,
    opens_with_consent_loosely,
    pending_gate_verdict,
    terminal_offer_key,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _flatten_follow_ups,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    SuggestedFollowUp,
)
from faultmaven.core.investigation.terminal_transitions import (
    QUESTION_MARKS,
    QUESTION_SHORTCODES,
    is_question,
    is_substantive_reply,
    propose_transition,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress

pytestmark = pytest.mark.unit

ZWSP = "\N{ZERO WIDTH SPACE}"
TEXT_SELECTOR = "\N{VARIATION SELECTOR-15}"
CHECK = "\N{HEAVY CHECK MARK}"
DASH = "\N{EM DASH}"

STATEMENT = "Checkout API returns 503 for all users since 14:00 UTC"
SIGNATURE = "SUGGEST_CLOSE|1|chain"
REPLY = "Here is what the logs say about the 503s."

#: Copilot's status-dropdown text for an INVESTIGATING case's Close pick
#: (``faultmaven-copilot`` ``case-service.ts``, ``CASE_ACTION_MESSAGES``). Over
#: the gate's 40-character substantive bound, so as text it would escape.
DROPDOWN_CLOSE = "Close this case as unresolved. Summarize what we found so far."

#: Appended to a consent opener to make a long consenting reply.
TAIL = " go ahead and close it, the fix held overnight and the alerts are quiet"


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


def _investigating() -> Case:
    case = Case(
        case_id="case_1838aaaaaaaa",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1838",
        enterprise_id="org_1838",
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
    return case


def _pending_close() -> Case:
    """An INVESTIGATING case with a signed CLOSE offer standing, so a recorded
    refusal is visible and "nothing recorded" is not vacuous."""
    case = _investigating()
    propose_transition(case, to_state="closed", summary="Shall I close it?")
    case.pending_transition["justifying_signature"] = SIGNATURE
    return case


def _gate1_case() -> Case:
    """An INQUIRY case whose statement stood before this turn: Gate 1 pending."""
    case = Case(
        case_id="case_1841bbbbbbbb",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1841",
        enterprise_id="org_1841",
        description="",
    )
    case.inquiry.proposed_problem_statement = STATEMENT
    case.inquiry.problem_statement_confirmed = False
    case.current_turn = 2
    return case


def _keys(follow_ups) -> list:
    return [(f.get("intent") or {}).get("proposal_id") for f in follow_ups]


def _assert_card_reshown(result, case, before: dict) -> None:
    """Nothing executed, nothing withdrawn, nothing recorded, and the standing
    offer's card shown again with its own key."""
    assert case.state == CaseState.INVESTIGATING
    assert case.pending_transition == before, "the standing offer moved"
    assert case.progress.deferred_disposition_declined_signatures == []
    assert "Please select one of the options above" in result["agent_response"]
    assert _keys(result["suggested_follow_ups"]) == [before["proposed_at"]] * 2


# =============================================================================
# #1840: one grammar, two strengths
# =============================================================================

LRM = "\N{LEFT-TO-RIGHT MARK}"
SHY = "\N{SOFT HYPHEN}"
RLI = "\N{RIGHT-TO-LEFT ISOLATE}"
EMOJI_SELECTOR = "\N{VARIATION SELECTOR-16}"
SKIN = "\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"
THUMBS = "\N{THUMBS UP SIGN}"

#: Over 100 characters, so substantive even to ``is_substantive_reply``.
PROCEED_ON_ERROR = (
    "proceed_on_error=false in the job config is the real culprit for the "
    "failures we saw overnight in east"
)

#: ``pending_gate_verdict`` at a pending CLOSE for a typed reply with no
#: intent, and whether the escape lane's record rule would record it as a
#: refusal once it escapes (over 40 characters, or a question). The rule's two
#: exemptions are a loose consent opener (``opens_with_consent_loosely``) and
#: a question (``is_question``). A short row never escapes, so ``no`` with
#: U+200B is re-asked by the engine whatever this column says.
#:
#: The verdict is the strict reading (#1840 review): a reply the gate does not
#: recognise as consent-shaped as typed is ``not_an_answer``, so a long one is
#: withdrawn and reaches the LLM. Only whether it is RECORDED reads loosely.
SHAPE_ROWS = [
    # U+FE0E is an emoji modifier, as U+FE0F is, after an emoji.
    (f"yes {CHECK}{TEXT_SELECTOR}", "confirm", False),
    # The bare readers stay strict (#1783's corpus): never executed.
    (f"yes{ZWSP}", "reask", False),
    (f"{ZWSP}yes", "not_an_answer", False),
    (f"no{ZWSP}", "not_an_answer", True),
    # A long consenting reply under markup or an invisible character: the gate
    # does not take it, so it is processed, and it is never recorded.
    (f"{ZWSP}Yes,{TAIL}", "not_an_answer", False),
    (f"{LRM}Yes,{TAIL}", "not_an_answer", False),
    (f"{SHY}Yes,{TAIL}", "not_an_answer", False),
    (f"{RLI}Yes,{TAIL}", "not_an_answer", False),
    (f"*Yes*,{TAIL}", "not_an_answer", False),
    (f"**Yes**{TAIL}", "not_an_answer", False),
    (f"_Yes_,{TAIL}", "not_an_answer", False),
    (f"__Yes__,{TAIL}", "not_an_answer", False),
    (f'"Yes" {DASH}{TAIL}', "not_an_answer", False),
    (
        f"\N{LEFT DOUBLE QUOTATION MARK}Yes\N{RIGHT DOUBLE QUOTATION MARK} "
        f"{DASH}{TAIL}",
        "not_an_answer",
        False,
    ),
    (
        f"\N{DOUBLE LOW-9 QUOTATION MARK}Yes\N{LEFT DOUBLE QUOTATION MARK} "
        f"{DASH}{TAIL}",
        "not_an_answer",
        False,
    ),
    (
        f"\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}Yes"
        f"\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} {DASH}{TAIL}",
        "not_an_answer",
        False,
    ),
    (f"'Yes' {DASH}{TAIL}", "not_an_answer", False),
    (
        "_go ahead_ and close it, the fix held overnight and the alerts are quiet",
        "not_an_answer",
        False,
    ),
    (
        '"ok" status from the canary was a lie, the 503s are back now',
        "not_an_answer",
        False,
    ),
    # Undecorated consent openers are the gate's, as on ``main``: re-asked.
    ("Yes, go ahead and close it, the fix held overnight", "reask", False),
    (
        "that's right, close it, the fix held overnight and the alerts are quiet",
        "reask",
        False,
    ),
    # A backtick quotes a word rather than using it, and ``*``/``_`` inside an
    # identifier are part of it: each of these is processed AND recorded.
    (f"`yes`{TAIL}", "not_an_answer", True),
    ("`ok` is false in the /health response from node-3 again", "not_an_answer", True),
    (
        "confirm_timeout is 5s on payments, that's the culprit here",
        "not_an_answer",
        True,
    ),
    (
        "ok_status flag never flipped on the canary, we can't close yet",
        "not_an_answer",
        True,
    ),
    (PROCEED_ON_ERROR, "not_an_answer", True),
    # A question in any script, as an emoji or as Slack's shortcode, is
    # substantive and never recorded.
    ("can we close it on friday\N{FULLWIDTH QUESTION MARK}", "not_an_answer", False),
    ("\N{INVERTED QUESTION MARK}ok", "not_an_answer", False),
    ("ok \N{ARABIC QUESTION MARK}", "not_an_answer", False),
    ("ok\N{GREEK QUESTION MARK}", "not_an_answer", False),
    ("friday \N{BLACK QUESTION MARK ORNAMENT}", "not_an_answer", False),
    ("hmm \N{INTERROBANG}", "not_an_answer", False),
    (
        "can we close it on friday instead :question: the soak needs the weekend",
        "not_an_answer",
        False,
    ),
    # The ASCII semicolon the Greek question mark looks like is not a question.
    ("ok;", "reask", False),
    # Negatives: these must still record the refusal.
    (
        "~~ok, close it~~ actually no, keep it open, we still see errors in prod",
        "not_an_answer",
        True,
    ),
    (
        f"No, keep it open {DASH} errors are still coming in from the east region",
        "not_an_answer",
        True,
    ),
    (
        f"*No*, keep it open {DASH} errors are still coming in from the east region",
        "not_an_answer",
        True,
    ),
    (
        "don't close it, the fix has not been verified in the east region yet",
        "not_an_answer",
        True,
    ),
    (
        f"no_way, keep it open {DASH} errors are still coming in from the east "
        "region",
        "not_an_answer",
        True,
    ),
    (
        f"_No_, keep it open {DASH} errors are still coming in from the east region",
        "not_an_answer",
        True,
    ),
    (
        f"'no' {DASH} keep it open, errors are still coming in from the east region",
        "not_an_answer",
        True,
    ),
]

#: A modifier is part of the emoji or symbol before it, and an invisible
#: character anywhere else (#1840 review, F8): the bare class each reply gets
#: against a pending CLOSE.
MODIFIER_ROWS = [
    # After a letter, a digit or a space, or at the start: it stays.
    (f"yes{TEXT_SELECTOR}", None),
    (f"{TEXT_SELECTOR}ok", None),
    (f"close{TEXT_SELECTOR} it", None),
    (f"yes{EMOJI_SELECTOR}", None),
    (f"close{EMOJI_SELECTOR} it", None),
    (f"ok{SKIN}", None),
    (f"ok {TEXT_SELECTOR}", None),
    (f"o{SKIN}k", None),
    (f"ok{SKIN}{EMOJI_SELECTOR}", None),
    # After an emoji or a symbol: it is part of it.
    (f"yes {CHECK}{TEXT_SELECTOR}", "explicit_token"),
    (f"yes {CHECK}{EMOJI_SELECTOR}", "explicit_token"),
    (f"ok {THUMBS}{SKIN}", "weak_token"),
    (f"{THUMBS}{SKIN} ok", "weak_token"),
    # A modifier after a modifier still modifies the emoji they follow.
    (f"ok {THUMBS}{SKIN}{EMOJI_SELECTOR}", "weak_token"),
]


class TestOneGrammarTwoStrengths:
    @pytest.mark.parametrize("message, verdict, recordable", SHAPE_ROWS)
    def test_the_shape_table(self, message, verdict, recordable):
        assert pending_gate_verdict(
            message, "closed", intent_value=None, typed=False
        ) == (verdict, "explicit_token" if verdict == "confirm" else None)
        # The escape lane records a refusal only for a reply that neither
        # opens with consent, read loosely, nor carries a question.
        exempt = opens_with_consent_loosely(message) or is_question(message)
        assert exempt is (not recordable)

    @pytest.mark.parametrize("message, via", MODIFIER_ROWS)
    def test_a_modifier_counts_only_on_an_emoji_or_a_symbol(self, message, via):
        assert confirmation_token_class(message, "closed") == via

    @pytest.mark.parametrize("message", [f"*Yes*,{TAIL}", f"{ZWSP}Yes,{TAIL}"])
    def test_the_gate_reads_strictly_and_the_record_rule_loosely(self, message):
        """The two readers on one reply: the gate does not take it
        (``_consent_prefix``, so the LLM sees it), and its withdrawal is not
        recorded (``opens_with_consent_loosely``)."""
        assert not _consent_prefix(message)
        assert opens_with_consent_loosely(message)

    @pytest.mark.parametrize("mark", sorted(QUESTION_MARKS))
    def test_every_question_mark_is_a_question(self, mark):
        assert is_question(f"ok {mark}")
        assert is_substantive_reply(f"ok {mark}")
        assert pending_gate_verdict(
            f"ok {mark}", "closed", intent_value=None, typed=False
        ) == ("not_an_answer", None)

    @pytest.mark.parametrize("code", QUESTION_SHORTCODES)
    def test_every_question_shortcode_is_a_question(self, code):
        assert is_question(f"ok {code}")
        assert is_question(f"ok {code.upper()}"), "read case-insensitively"
        assert pending_gate_verdict(
            f"ok {code}", "closed", intent_value=None, typed=False
        ) == ("not_an_answer", None)

    def test_the_question_marks_are_pinned(self):
        assert QUESTION_MARKS == frozenset(
            "?"
            "\N{FULLWIDTH QUESTION MARK}"
            "\N{INVERTED QUESTION MARK}"
            "\N{ARABIC QUESTION MARK}"
            "\N{REVERSED QUESTION MARK}"
            "\N{SMALL QUESTION MARK}"
            "\N{PRESENTATION FORM FOR VERTICAL QUESTION MARK}"
            "\N{GREEK QUESTION MARK}"
            "\N{BLACK QUESTION MARK ORNAMENT}"
            "\N{WHITE QUESTION MARK ORNAMENT}"
            "\N{INTERROBANG}"
            "\N{EXCLAMATION QUESTION MARK}"
            "\N{QUESTION EXCLAMATION MARK}"
            "\N{DOUBLE QUESTION MARK}"
        )
        assert QUESTION_SHORTCODES == (":question:", ":grey_question:", ":interrobang:")
        assert not is_question("ok;") and not is_question("")
        assert not is_question(None)

    def test_a_minted_decline_on_a_question_in_any_script_is_no_refusal(self):
        """#1813's rule, read through ``is_question``. A long text, so the
        engine sends it down the escape lane rather than re-asking it."""
        text = (
            "can we hold off on closing until friday's change window "
            "instead\N{FULLWIDTH QUESTION MARK}"
        )
        assert pending_gate_verdict(text, "closed", intent_value=False, typed=True) == (
            "not_an_answer",
            None,
        )
        # The control: the same words with no question mark still decline.
        assert pending_gate_verdict(
            text[:-1], "closed", intent_value=False, typed=True
        ) == ("decline", None)


def _processing_engine() -> MilestoneEngine:
    return _engine(
        InvestigationResponse_Diagnosis(agent_response=REPLY, state_updates={})
    )


class TestTheShapeRowsThroughTheEngine:
    @pytest.mark.parametrize(
        "message",
        [
            f"{ZWSP}Yes, go ahead and close it, the fix held overnight",
            f"_Yes_,{TAIL}",
            f"{LRM}Yes,{TAIL}",
            f"{SHY}Yes,{TAIL}",
            f"{RLI}Yes,{TAIL}",
            '"ok" status from the canary was a lie, the 503s are back now',
            # Over 100 characters, so substantive to every reader.
            "_Yes_, go ahead and close it. We verified the fix in staging and in "
            "prod overnight and the error has not come back since.",
        ],
    )
    async def test_a_consent_opener_under_markup_is_processed_and_not_recorded(
        self, message
    ):
        """The gate does not take it: the offer is withdrawn and the LLM
        processes the reply. It opens with consent once the markup is read
        past, so the withdrawal is not recorded as a refusal."""
        case = _pending_close()
        engine = _processing_engine()
        await engine.process_turn(case=case, user_message=message)
        assert case.pending_transition is None, "the gate took the turn"
        assert case.progress.deferred_disposition_declined_signatures == []
        engine.generator.generate_structured_output.assert_awaited()

    @pytest.mark.parametrize(
        "message",
        [
            "`ok` is false in the /health response from node-3 again",
            "confirm_timeout is 5s on payments, that's the culprit here",
            "ok_status flag never flipped on the canary, we can't close yet",
            PROCEED_ON_ERROR,
            f"`yes`{TAIL}",
            # Strikethrough negates.
            "~~ok, close it~~ actually no, keep it open, we still see errors in prod",
        ],
    )
    async def test_a_deflection_is_processed_and_recorded(self, message):
        """As on ``main``: the reply reaches the LLM and the refusal is
        recorded. A backtick, and ``*``/``_`` inside an identifier, are not
        markup around a consent word."""
        case = _pending_close()
        engine = _processing_engine()
        await engine.process_turn(case=case, user_message=message)
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == [SIGNATURE]
        engine.generator.generate_structured_output.assert_awaited()

    async def test_an_undecorated_consent_opener_is_still_re_asked(self):
        """The control, unchanged from ``main``: the gate takes it."""
        case = _pending_close()
        before = dict(case.pending_transition)
        engine = _engine()
        result = await engine.process_turn(
            case=case,
            user_message="Yes, go ahead and close it, the fix held overnight",
        )
        _assert_card_reshown(result, case, before)
        engine.generator.generate_structured_output.assert_not_awaited()

    @pytest.mark.parametrize(
        "message",
        [
            # Short: substantive to the gate only because it is a question.
            "\N{INVERTED QUESTION MARK}ok",
            "can we close it on friday\N{FULLWIDTH QUESTION MARK}",
            "friday \N{BLACK QUESTION MARK ORNAMENT}",
            "friday :question:",
            # Long: the record rule alone keeps it unrecorded.
            "what happens to the runbook if we close this case right "
            "now\N{FULLWIDTH QUESTION MARK}",
            "what happens to the runbook if we close this case right now :question:",
        ],
    )
    async def test_a_question_in_any_script_withdraws_and_records_nothing(
        self, message
    ):
        case = _pending_close()
        engine = _processing_engine()
        await engine.process_turn(case=case, user_message=message)
        assert case.pending_transition is None, "the question did not withdraw"
        assert case.progress.deferred_disposition_declined_signatures == []
        engine.generator.generate_structured_output.assert_awaited()


# =============================================================================
# #1839: no LLM-written DECIDE card sends a bare gate reply
# =============================================================================

BARE_GATE_REPLIES = [
    "Proceed",
    "Yes",
    "Yes!",
    "yes \N{THUMBS UP SIGN}",
    "Close it",
    "Resolve it",
    "Mark as resolved",
    "No",
    "Not yet",
    "Wait",
    "Cancel",
    "Stop",
    "ok",
    "nope",
    "hold on",
]
NOT_BARE = [
    f"Proceed{ZWSP}",
    "**Proceed**",
    "Yes, check the pool config",
    "Check the pool config",
    "Go ahead and restart the pods",
    "Yes please",
    "Sure thing",
]
BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
#: Not bare as written, but bare as a client sends it: invisible characters
#: gone and whitespace trimmed (JavaScript's ``trim()`` strips U+FEFF).
BARE_AS_SENT = [f"{BOM}Close it", f"Yes{BOM}", f"Proceed{ZWSP}"]
#: A payload neither way, so the card ships as written.
CARD_NOT_BARE = [text for text in NOT_BARE if text not in BARE_AS_SENT]


class TestTheBareGateReply:
    @pytest.mark.parametrize("text", BARE_GATE_REPLIES)
    def test_a_bare_gate_reply(self, text):
        assert is_bare_gate_reply(text)

    @pytest.mark.parametrize("text", NOT_BARE)
    def test_not_a_bare_gate_reply(self, text):
        assert not is_bare_gate_reply(text)

    @pytest.mark.parametrize(
        "text, gate1",
        [
            ("Proceed", True),
            ("Yes", True),
            ("Yes!", True),
            ("yes \N{THUMBS UP SIGN}", True),
            ("ok", True),
            ("Close it", False),
            ("Resolve it", False),
            ("Mark as resolved", False),
            *((decline, False) for decline in ("No", "Not yet", "Wait", "Cancel")),
        ],
    )
    def test_gate1_consent_is_a_subset(self, text, gate1):
        assert gate1_bare_consent(text) is gate1
        assert is_bare_gate_reply(text)

    @pytest.mark.parametrize(
        "token", _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS + _DECLINE_TOKENS
    )
    def test_it_moves_with_the_bare_readers(self, token):
        """Every token either bare reader accepts is a bare gate reply, so a
        token added to a list is covered without touching the card rule."""
        assert is_bare_gate_reply(token)

    @pytest.mark.parametrize("text", [*BARE_GATE_REPLIES, *BARE_AS_SENT, " Proceed "])
    def test_a_card_reads_as_sent(self, text):
        assert card_reads_as_bare_reply(text)

    @pytest.mark.parametrize("text", [f"{BOM}Close it", f"Yes{BOM}", f"Proceed{ZWSP}"])
    def test_the_strict_reader_alone_misses_what_a_client_sends(self, text):
        """The reason ``card_reads_as_bare_reply`` exists (#1840 review, F4)."""
        assert not is_bare_gate_reply(text)

    @pytest.mark.parametrize("text", CARD_NOT_BARE)
    def test_a_card_that_is_not_bare_either_way(self, text):
        assert not card_reads_as_bare_reply(text)


def _card(label, payload, action_type="DECIDE") -> SuggestedFollowUp:
    return SuggestedFollowUp(label=label, action_type=action_type, payload=payload)


@pytest.fixture
def card_counter():
    with patch.object(turn_records, "llm_decide_card_bare_payload_total") as counter:
        yield counter


class TestTheCardRule:
    @pytest.mark.parametrize(
        "payload", [*BARE_GATE_REPLIES, *BARE_AS_SENT, " Proceed "]
    )
    def test_a_bare_payload_sends_the_label(self, card_counter, payload):
        out = _flatten_follow_ups([_card("Check the pool config", payload)], {})
        assert out == [
            {
                "label": "Check the pool config",
                "action_type": "DECIDE",
                "payload": "Check the pool config",
            }
        ]
        card_counter.labels.assert_called_once_with(action="label")
        card_counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.parametrize(
        "label, payload",
        [
            ("Yes", "Proceed"),
            ("Close it", "Yes"),
            ("No", "Not yet"),
            (f"{BOM}Yes", "Proceed"),
            (f"Close it{ZWSP}", "Yes"),
        ],
    )
    def test_a_card_whose_label_is_bare_too_is_dropped(
        self, card_counter, label, payload
    ):
        keep = _card("Check the pool config", "Check the pool config")
        out = _flatten_follow_ups([_card(label, payload), keep], {})
        assert [f["label"] for f in out] == ["Check the pool config"]
        card_counter.labels.assert_called_once_with(action="dropped")
        card_counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.parametrize(
        "label",
        [
            # A command would be coerced to RUN, so a click would submit it.
            "kubectl rollout restart deploy/api",
            # A results handoff the user never sent would be coerced to EVIDENCE.
            "Here are the logs from node-3",
            "I re-ran it, check the output",
        ],
    )
    def test_a_card_whose_label_fails_the_payload_nets_is_dropped(
        self, card_counter, label, caplog
    ):
        """The label never met ``SuggestedFollowUp``'s safety nets; the card
        rule puts it through them before sending it (#1840 review, F5)."""
        with caplog.at_level("INFO", logger=turn_records.__name__):
            out = _flatten_follow_ups([_card(label, "Proceed")], {})
        assert out == []
        card_counter.labels.assert_called_once_with(action="dropped")
        assert "would not stay a DECIDE payload" in caplog.text

    def test_a_label_that_passes_the_nets_is_sent(self, card_counter):
        out = _flatten_follow_ups([_card("Restart the pods", "Proceed")], {})
        assert out[0]["payload"] == "Restart the pods"
        assert out[0]["action_type"] == "DECIDE"

    @pytest.mark.parametrize("payload", CARD_NOT_BARE)
    def test_a_payload_that_is_not_bare_is_untouched(self, card_counter, payload):
        out = _flatten_follow_ups([_card("Yes", payload)], {})
        assert out[0]["payload"] == payload
        card_counter.labels.assert_not_called()

    def test_a_run_card_is_untouched(self, card_counter):
        """A RUN click copies its command and never submits."""
        out = _flatten_follow_ups([_card("Yes", "Proceed", action_type="RUN")], {})
        assert out[0]["payload"] == "Proceed"
        card_counter.labels.assert_not_called()

    def test_the_rewrite_is_logged_with_the_label(self, card_counter, caplog):
        with caplog.at_level("INFO", logger=turn_records.__name__):
            _flatten_follow_ups([_card("Check the pool config", "Proceed")], {})
        assert "Check the pool config" in caplog.text
        assert "#1839" in caplog.text

    async def test_through_the_engine(self, card_counter):
        """An engine turn whose mocked LLM offers ``DECIDE {label: "Check the
        pool config", payload: "Proceed"}``: the card the turn ships sends its
        label."""
        case = _investigating()
        engine = _engine(
            InvestigationResponse_Diagnosis(
                agent_response=REPLY,
                state_updates={},
                suggested_follow_ups=[_card("Check the pool config", "Proceed")],
            )
        )
        result = await engine.process_turn(case=case, user_message="what next")
        cards = [
            f
            for f in result["suggested_follow_ups"]
            if f["label"] == "Check the pool config"
        ]
        assert [f.get("payload") for f in cards] == ["Check the pool config"]
        assert all(
            f.get("payload") != "Proceed" for f in result["suggested_follow_ups"]
        )
        card_counter.labels.assert_called_once_with(action="label")


# =============================================================================
# #1838: a status-dropdown re-pick of the pending target re-shows the card
# =============================================================================


class TestADropdownRePickReshowsTheCard:
    async def test_two_picks_then_the_card_closes_it(self):
        case = _investigating()
        engine = _engine()

        first = await engine.process_turn(
            case=case,
            user_message=DROPDOWN_CLOSE,
            intent_type="status_transition",
            intent_data={"from_state": "investigating", "to_state": "closed"},
        )
        assert case.state == CaseState.INVESTIGATING
        assert case.pending_transition["to_state"] == "closed"
        key = terminal_offer_key(case.pending_transition)
        assert _keys(first["suggested_follow_ups"]) == [key] * 2
        # Signed, so a refusal recorded by the second pick would be visible.
        case.pending_transition["justifying_signature"] = SIGNATURE
        before = dict(case.pending_transition)

        second = await engine.process_turn(
            case=case,
            user_message=DROPDOWN_CLOSE,
            intent_type="status_transition",
            intent_data={"from_state": "investigating", "to_state": "closed"},
        )
        _assert_card_reshown(second, case, before)
        engine.generator.generate_structured_output.assert_not_awaited()

        await engine.process_turn(
            case=case,
            user_message="Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": key},
        )
        assert case.state == CaseState.CLOSED
        assert case.turn_history[-1].terminal_confirmed_via == "intent"

    async def test_a_minted_status_transition_on_a_bare_token_still_closes(self):
        """Unchanged: a MINTED intent's text decides, and ``close it`` is bare
        consent to a CLOSE."""
        case = _pending_close()
        await _engine().process_turn(
            case=case,
            user_message="close it",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
            typed=True,
        )
        assert case.state == CaseState.CLOSED
        assert case.turn_history[-1].terminal_confirmed_via == "explicit_token"


# =============================================================================
# #1841: Gate 1 reads a bare typed consent
# =============================================================================


def _inquiry_reply(confirmed: bool = False) -> InquiryResponse:
    return InquiryResponse(
        agent_response=REPLY,
        state_updates={"user_confirmed_investigation": confirmed},
    )


class TestGate1ReadsABareTypedConsent:
    @pytest.mark.parametrize("intent_type", [None, "conversation"])
    @pytest.mark.parametrize(
        "message", ["yes", "Yes!", "ok \N{THUMBS UP SIGN}", "proceed"]
    )
    async def test_a_bare_consent_commits_without_the_llm_flag(
        self, intent_type, message
    ):
        """No intent, no stored card to mint from, and an LLM that does not
        set ``user_confirmed_investigation``: the engine reads it itself."""
        case = _gate1_case()
        engine = _engine(_inquiry_reply(confirmed=False))
        await engine.process_turn(
            case=case, user_message=message, intent_type=intent_type
        )
        assert case.inquiry.problem_statement_confirmed is True
        assert case.state == CaseState.INVESTIGATING
        assert case.inquiry.proposed_problem_statement == STATEMENT

    @pytest.mark.parametrize(
        "message", ["yes but check the pool first", "Close it", "yes please"]
    )
    async def test_what_is_not_a_bare_gate1_consent_commits_nothing(self, message):
        case = _gate1_case()
        engine = _engine(_inquiry_reply(confirmed=False))
        await engine.process_turn(case=case, user_message=message)
        assert case.inquiry.problem_statement_confirmed is False
        assert case.state == CaseState.INQUIRY

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"user_message": "yes"},
            {
                "user_message": "Yes, that's correct. Let's investigate.",
                "intent_type": "confirmation",
                "intent_data": {
                    "value": True,
                    "proposal_id": gate1_offer_key(STATEMENT),
                },
            },
        ],
        ids=["typed", "click"],
    )
    async def test_the_click_and_the_typed_reply_commit_through_one_helper(
        self, kwargs
    ):
        case = _gate1_case()
        with patch.object(
            engine_module, "_commit_gate1", wraps=engine_module._commit_gate1
        ) as commit:
            await _engine(_inquiry_reply()).process_turn(case=case, **kwargs)
        commit.assert_called_once()
        assert case.inquiry.problem_statement_confirmed is True
