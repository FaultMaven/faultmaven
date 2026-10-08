"""#1748 - how a terminal transition was confirmed, and who kept talking after.

#723 defers two confirmation-model notes until either is observed. The first (a
terminal transition confirmed by a bare weak token that proved spurious) needs
the confirming channel recorded and the next message counted. The second (a
dropdown INQUIRY -> INVESTIGATING that did not transition) cannot occur:
``USER_SELECTABLE_ACTIONS`` offers only CLOSED from INQUIRY and
``earned_edge_refusal`` refuses any other ``status_transition`` at engine entry.

The channel is written onto the confirming turn's record, and BOTH counters are
read from the committed records by the investigation service after the turn's
one commit (``turn_messages._emit_committed_turn``, #1882). So everything that decides a count is
driven here through ``InvestigationService.process_turn`` — the path a click or
a typed reply actually takes — with only the LLM and the database doubled.

#1783 (ruling (a)) narrowed what a channel can be: a terminal proposal executes
only on its click or on a BARE typed consent token, so the verdict corpus and
the gate table below are the pin for consent, and
``TestOnlyAClickOrABareTokenExecutes`` drives the rule through the service.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.terminal_transitions as tt
import faultmaven.modules.agent.domain.services.investigation_service.turn_messages as turn_messages
import faultmaven.modules.case.domain.models.turn as turn_model
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _gate_token_match,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _EXPLICIT_CONFIRM_TOKENS,
    _TARGET_SCOPED_TOKENS,
    _WEAK_CONFIRM_TOKENS,
    confirmation_token_class,
    pending_gate_verdict,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.exceptions import StaleCaseException

pytestmark = pytest.mark.unit

#: The label table. Only a BARE token is consent (#1783, ruling (a)): the whole
#: reply is one token, with only trailing punctuation and positive emoji,
#: shortcodes or emoticons around it. So there are only two typed labels.
LABEL_TABLE = {
    "weak_token": [
        "ok",
        "ok!",
        "ok :)",
        "ok 👍",
        "ok =)",
        "lgtm",
        "sure.",
        "  Okay  ",
        # Slack's wire form of an emoji labels as the Unicode emoji does.
        "ok :+1:",
        "OK :THUMBSUP:",
        # No space: what is left once the shortcode is removed is only "ok".
        "ok:+1:",
    ],
    "explicit_token": [
        "yes",
        "yes 👍",
        "yes :+1:",
        "go ahead",
        "that's right",
        "that\u2019s right",
        "yes!",
        "yes :white_check_mark:",
    ],
}

#: The labels #1783 removed: a typed reply that says more than one token is
#: re-asked, so nothing can produce them.
REMOVED_LABELS = ("explicit_prefixed", "weak_prefixed", "typed_other")

#: A RESOLVED offer standing somewhere else. A card names the offer it presents
#: (#1812), so a CLICK in these tests is built from the case it is sent to
#: (``_click_confirm``). These two are for the cards' text, and for MINTS, whose
#: intent carries this other offer's key: a mint is not a click, and its text
#: decides whatever key it carries.
_ELSEWHERE = SimpleNamespace(
    case_id="case_elsewhere", pending_transition={"proposed_at": "elsewhere"}
)
#: What "Yes, mark as resolved" sends when clicked.
CONFIRM_CARD = _resolution_confirmation_suggestions(_ELSEWHERE)[0]
#: What "Not yet, continue investigating" sends when clicked.
DECLINE_CARD = _resolution_confirmation_suggestions(_ELSEWHERE)[1]

#: The target each target-scoped token consents to, pinned literally. Every
#: other token consents to either target.
OWN_TARGET = {
    "close it": "closed",
    "resolve it": "resolved",
    "mark as resolved": "resolved",
    "mark it as resolved": "resolved",
}

# The #1783 mechanism probe's corpus (round 24, ``probe_1783.py``), verbatim:
# the values are the probe's, and only the invisible characters (U+200B, U+200D,
# U+FEFF) are written as escapes so a reader can see them. A trailing
# ``[closed]``/``[resolved]`` names the target to test against; the default is
# ``resolved``.
MUST_EXECUTE = [
    "ok",
    "ok!",
    "OK.",
    "Ok",
    "okay",
    "sure",
    "lgtm",
    "sounds good",
    "looks good",
    "yes",
    "yes!",
    "Yes.",
    "YES",
    "yes!!",
    "yep",
    "yup",
    "yeah",
    "confirmed",
    "confirm",
    "approve",
    "approved",
    "absolutely",
    "correct",
    "proceed",
    "go ahead",
    "go for it",
    "do it",
    "please do",
    "that's right",
    "that’s right",
    "that’s correct",
    "that's correct",
    "ok 👍",
    "👍 ok",
    "ok 👍🏽",
    "yes ✅",
    "yes ✔️",
    "lgtm 👍",
    "ok :+1:",
    "ok :+1::skin-tone-3:",
    "yes :white_check_mark:",
    "ok :)",
    "yes :-)",
    "ok (y)",
    "  yes  ",
    "yes\n",
    "ok,",
    "ok...",
    "go ahead",
    "go  ahead",
    "mark as resolved",
    "mark it as resolved",
    "resolve it",
]
MUST_NOT_EXECUTE = [
    "ok, don't close it yet",
    "ok, don’t close it yet",
    "ok no",
    "ok, no",
    "sure, do it later",
    "okay i'll confirm with the team",
    "lgtm, not approved yet",
    "ok. mark as resolved later",
    "yes, don't close it yet",
    "do it later",
    "confirm later",
    "ok, wait",
    "ok not now",
    "ok hold on",
    "yes no",
    "ok stop",
    "ok cancel that",
    "ok never mind",
    "yes... actually no",
    "okay, let me double check first",
    "ok after lunch",
    "sure — tomorrow",
    "ok, the issue is back",
    "ok the case is fine as is",
    "yes it is not fixed",
    "❌ close it",
    "❌ ok",
    "!ok",
    "~~ok~~",
    "[ ] close it",
    "- ok",
    "> ok",
    "`ok`",
    "**yes**",
    '"yes"',
    "ok 👎",
    "👎 ok",
    "ok :-1:",
    "ok :thumbsdown:",
    "ok :x:",
    "ok :(",
    "yes :(",
    "ok >:(",
    "ok :/",
    "ok ⏳",
    "ok 🤔",
    "ok :thinking_face:",
    "yes 🛑",
    "ok >:)",
    "ok?",
    "yes?",
    "ok？",
    "ok¿",
    "ok ؟",
    "ok but what is the root cause?",
    "оk",
    "ｏｋ",
    "ok\u200b",
    "\u200bok",
    "o\u200bk",
    "ye\u200bs",
    "yes\u200dno",
    "ok\ufeff",
    "ok go ahead",
    "yes please",
    "yes please close it",
    "ok ok",
    "yes, go ahead",
    "sure, close it",
    "lgtm, confirmed",
    "ok thanks",
    "looks good to me",
    "yes thank you",
    "clo(y)se it",
    "y:)es",
    "o👍k",
    "resolve it[closed]",
    "close it[resolved]",
    "ok 1",
    "yes2",
    "ok :d",
    "ok xd",
    "ok <3",
    "yesterday it was fine",
]


def _with_target(entry: str) -> tuple[str, str]:
    for target in ("closed", "resolved"):
        suffix = f"[{target}]"
        if entry.endswith(suffix):
            return entry[: -len(suffix)], target
    return entry, "resolved"


#: ``pending_gate_verdict`` against a pending RESOLVED: the reply, the answer
#: its intent carries (None: no intent), ``typed`` (False only for a click),
#: and the verdict and channel expected. A typed row with an intent is one the
#: resolver MINTED from the text.
GATE_TABLE = [
    ("ok", None, True, "confirm", "weak_token"),
    ("ok", True, True, "confirm", "weak_token"),
    ("ok", False, True, "reask", None),
    ("ok go ahead", None, True, "reask", None),
    ("ok go ahead", True, True, "reask", None),
    ("ok go ahead", False, True, "reask", None),
    ("ok, don't close it yet", None, True, "reask", None),
    ("ok, don't close it yet", False, True, "reask", None),
    ("no", None, True, "decline", None),
    ("no", True, True, "reask", None),
    # #1813: a typed decline is BARE. This one opens with "no" and says more,
    # so it answers neither way; carrying "?", it takes the escape lane.
    (
        "no, we did not do anything yet \u2014 did you see anything wrong?",
        None,
        True,
        "not_an_answer",
        None,
    ),
    ("no problem, go ahead", None, True, "not_an_answer", None),
    ("hmm", None, True, "not_an_answer", None),
    ("the pod restarted", None, True, "not_an_answer", None),
    ("that works", True, True, "reask", None),
    # A MINTED decline on question-free text still declines (#1813, item 3)...
    ("not now thanks", False, True, "decline", None),
    # ...and the same words typed, with no mint, are re-asked (item 1).
    ("not now thanks", None, True, "not_an_answer", None),
    # A minted decline on a question is not a refusal (#1813, item 2), whether
    # or not the text is a decline token.
    ("no?", False, True, "not_an_answer", None),
    (
        "can we hold off until friday's change window?",
        False,
        True,
        "not_an_answer",
        None,
    ),
    (
        "yes, go ahead and close it, verified the fix in staging and prod",
        None,
        True,
        "reask",
        None,
    ),
    (
        "we will do it in friday's maintenance window instead of now",
        None,
        True,
        "not_an_answer",
        None,
    ),
    ("what happens to the runbook if I close this?", None, True, "not_an_answer", None),
    (
        "ok so here's what I found: the pod restarted at 3pm and the logs show OOM "
        "kills at the same time, memory limit 512Mi",
        None,
        True,
        "not_an_answer",
        None,
    ),
    ("   ", None, True, "not_an_answer", None),
    # A click is read from the intent alone: the card's own text, or anything.
    (CONFIRM_CARD["payload"], True, False, "confirm", "intent"),
    ("ok, don't close it yet", True, False, "confirm", "intent"),
    (DECLINE_CARD["payload"], False, False, "decline", None),
    ("ok", False, False, "decline", None),
]

_ALL_TOKENS = _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS


class TestTheClassifier:
    @pytest.mark.parametrize(
        "label, message",
        [(label, m) for label, rows in LABEL_TABLE.items() for m in rows],
    )
    def test_the_label_table(self, label, message):
        assert confirmation_token_class(message, "resolved") == label

    @pytest.mark.parametrize("to_state", ["resolved", "closed"])
    @pytest.mark.parametrize("token", _WEAK_CONFIRM_TOKENS)
    def test_each_weak_token_alone_is_weak(self, token, to_state):
        assert confirmation_token_class(token, to_state) == "weak_token"

    @pytest.mark.parametrize("token", _EXPLICIT_CONFIRM_TOKENS)
    def test_each_explicit_token_alone_is_explicit(self, token):
        to_state = OWN_TARGET.get(token, "resolved")
        assert confirmation_token_class(token, to_state) == "explicit_token"

    def test_a_target_scoped_token_consents_only_to_its_own_target(self):
        assert confirmation_token_class("close it", "closed") == "explicit_token"
        assert confirmation_token_class("close it", "resolved") is None
        assert confirmation_token_class("resolve it", "closed") is None
        assert confirmation_token_class("mark as resolved", "closed") is None
        assert confirmation_token_class("mark it as resolved", "closed") is None
        assert confirmation_token_class("yes", "closed") == "explicit_token"

    @pytest.mark.parametrize(
        "message, to_state",
        [
            ("clo(y)se it", "closed"),
            ("y:)es", "resolved"),
            ("o\U0001f44dk", "resolved"),
        ],
    )
    def test_a_decoration_inside_a_word_never_reassembles_a_token(
        self, message, to_state
    ):
        """Each against a target the reassembled token WOULD consent to. The
        corpus tests ``clo(y)se it`` against ``resolved``, where target scoping
        refuses ``close it`` anyway and so hides a decoration removed as ``""``
        rather than as a space."""
        assert confirmation_token_class(message, to_state) is None

    @pytest.mark.parametrize(
        "message, to_state",
        [
            ("o\U0001f3fdk", "resolved"),
            ("y\U0001f3fdes", "resolved"),
            ("o\ufe0fk", "resolved"),
            ("clo\ufe0fse it", "closed"),
            ("ok :+\U0001f3fb1:", "resolved"),
            ("ok :\ufe0f)", "resolved"),
        ],
    )
    def test_an_emoji_modifier_inside_a_word_never_reassembles_a_token(
        self, message, to_state
    ):
        """Review round 1: a skin tone or the emoji presentation selector
        (U+FE0F) is replaced by a space like a decoration, never deleted, so
        one inside a word or a decoration splits it."""
        assert confirmation_token_class(message, to_state) is None

    @pytest.mark.parametrize(
        "message",
        [
            "\U0001f44d\U0001f3fd ok",
            "ok \U0001f44d\U0001f3fd",
            "yes \u2714\ufe0f",
            "\u2714\ufe0f yes",
        ],
    )
    def test_an_emoji_modifier_on_a_positive_emoji_keeps_a_reply_bare(self, message):
        assert confirmation_token_class(message, "resolved") is not None

    def test_the_target_map_is_pinned(self):
        assert _TARGET_SCOPED_TOKENS == OWN_TARGET

    @pytest.mark.parametrize("message", ["that works", "", "ok_", "yesterday"])
    def test_what_the_gate_does_not_read_as_consent_is_none(self, message):
        assert confirmation_token_class(message, "resolved") is None

    def test_a_substantive_reply_is_not_a_confirmation(self):
        assert (
            confirmation_token_class("ok but what is the root cause?", "resolved")
            is None
        )

    def test_a_token_later_in_the_reply_does_not_confirm(self):
        assert confirmation_token_class("well, yes", "resolved") is None

    @pytest.mark.parametrize("entry", MUST_EXECUTE)
    def test_every_bare_consent_is_consent(self, entry):
        message, to_state = _with_target(entry)
        assert confirmation_token_class(message, to_state) is not None

    @pytest.mark.parametrize("entry", MUST_NOT_EXECUTE)
    def test_nothing_but_a_bare_consent_is_consent(self, entry):
        """#1783: the replies that open with a token and then refuse or defer,
        the vocabulary combinations and symbol games that broke the two word
        grammars before ruling (a), negative decorations, questions, lookalikes,
        and multi-token consents, which are re-asked rather than executed."""
        message, to_state = _with_target(entry)
        assert confirmation_token_class(message, to_state) is None

    @pytest.mark.parametrize("entry", MUST_EXECUTE)
    def test_every_bare_consent_executes_at_the_gate(self, entry):
        """The corpus read through the gate's own verdict, not only the
        classifier: #1840 made the shape reader lenient, and the gate must
        still execute exactly the bare set."""
        message, to_state = _with_target(entry)
        verdict, _ = pending_gate_verdict(
            message, to_state, intent_value=None, typed=True
        )
        assert verdict == "confirm"

    @pytest.mark.parametrize("entry", MUST_NOT_EXECUTE)
    def test_nothing_but_a_bare_consent_executes_at_the_gate(self, entry):
        message, to_state = _with_target(entry)
        verdict, _ = pending_gate_verdict(
            message, to_state, intent_value=None, typed=True
        )
        assert verdict != "confirm"

    @pytest.mark.parametrize(
        "message, intent_value, typed, verdict, via",
        GATE_TABLE,
    )
    def test_the_gate_verdict_table(self, message, intent_value, typed, verdict, via):
        assert pending_gate_verdict(
            message, "resolved", intent_value=intent_value, typed=typed
        ) == (verdict, via)

    def test_the_removed_labels_are_gone(self):
        assert get_args(TerminalConfirmedVia) == (
            "intent",
            "explicit_token",
            "weak_token",
        )
        assert not set(REMOVED_LABELS) & set(get_args(TerminalConfirmedVia))

    def test_no_two_tokens_can_both_open_one_message(self):
        """Two tokens can both match at a message's start only if one matches
        at the start of the other, so checking the tokens against each other
        covers every message. None does today: the class never depends on the
        longest-match rule. A token added that overlaps another fails here, and
        whoever adds it decides the class knowingly."""
        overlapping = [
            (a, b)
            for a in _ALL_TOKENS
            for b in _ALL_TOKENS
            if a != b and _gate_token_match(a, (b,)) is not None
        ]
        assert overlapping == []

    def test_the_longest_opening_token_wins(self):
        assert _gate_token_match("go ahead now", ("go", "go ahead")) == (
            "go ahead",
            8,
        )
        assert _gate_token_match("go ahead now", ("go ahead", "go")) == (
            "go ahead",
            8,
        )

    def test_the_match_keeps_the_word_boundary(self):
        assert _gate_token_match("ok_", ("ok",)) is None
        assert _gate_token_match("okay", ("ok",)) is None

    def test_the_weak_set_is_exactly_723s_note_1_list(self):
        assert _WEAK_CONFIRM_TOKENS == (
            "ok",
            "okay",
            "sure",
            "sounds good",
            "looks good",
            "lgtm",
        )

    def test_the_explicit_set_is_pinned(self):
        assert _EXPLICIT_CONFIRM_TOKENS == (
            "yes",
            "yeah",
            "yep",
            "yup",
            "correct",
            "confirmed",
            "confirm",
            "approve",
            "approved",
            "absolutely",
            "go ahead",
            "go for it",
            "do it",
            "please do",
            "proceed",
            "mark as resolved",
            "mark it as resolved",
            "resolve it",
            "close it",
            "that's right",
            "that's correct",
        )

    def test_the_two_sets_are_disjoint(self):
        assert not set(_WEAK_CONFIRM_TOKENS) & set(_EXPLICIT_CONFIRM_TOKENS)


@pytest.fixture
def fresh_warnings(monkeypatch):
    """The warn-once memory is per process; each test starts with none."""
    monkeypatch.setattr(turn_model, "_WARNED_UNKNOWN_CHANNELS", set())


def _record(via):
    return TurnProgress(
        turn_number=3,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        terminal_confirmed_via=via,
    )


class TestAStoredChannelNeverBreaksALoad:
    """The loaders rebuild every record with ``TurnProgress(**t)``, so a value
    the ``Literal`` rejected would make the whole case unloadable (review F8)."""

    @pytest.mark.parametrize("stale", REMOVED_LABELS)
    def test_an_unknown_stored_channel_loads_as_none_and_says_so(
        self, caplog, fresh_warnings, stale
    ):
        """A record written before #1783 removed its label still loads."""
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            record = _record(stale)
        assert record.terminal_confirmed_via is None
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert f"'{stale}'" in caplog.records[0].getMessage()

    def test_the_same_stale_value_warns_once(self, caplog, fresh_warnings):
        """Review F11: a stale record is re-read on every load of its case."""
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            _record("bare_weak")
            _record("bare_weak")
            _record("retired_channel")
        assert [r.getMessage().split()[1] for r in caplog.records] == [
            "'bare_weak'",
            "'retired_channel'",
        ]

    @pytest.mark.parametrize("via", [*get_args(TerminalConfirmedVia), None])
    def test_every_known_channel_survives_silently(self, via, caplog, fresh_warnings):
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            record = _record(via)
        assert record.terminal_confirmed_via == via
        assert not caplog.records


# ---------------------------------------------------------------------------
# Driven through InvestigationService.process_turn
# ---------------------------------------------------------------------------

CASE_ID = "case_1748a1748a17"
USER_ID = "user_test"


def _investigating_case(pending: str = "resolved") -> Case:
    case = Case(
        case_id=CASE_ID,
        title="Terminal confirmation counters",
        state=CaseState.INQUIRY,
        user_id=USER_ID,
        enterprise_id="org_test",
        description="pool exhaustion",
        problem_verification=ProblemVerification(
            symptom_statement="pool exhaustion",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "pool exhaustion"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    propose_transition(case, to_state=pending, summary="Resolve it?")
    return case


class _Store:
    """The repository as a database behaves: a save stores a SNAPSHOT and a get
    returns a fresh copy. A turn whose save fails therefore leaves nothing for
    its retry to find, which is what makes the retry below a real one."""

    def __init__(self, case: Case) -> None:
        self._rows = {case.case_id: case.model_copy(deep=True)}
        self._failures: list[tuple] = []

    def fail_next_save(self, exc: Exception, when=lambda case: True) -> None:
        """Raise ``exc`` on the next save for which ``when(case)`` holds."""
        self._failures.append((when, exc))

    async def get(self, case_id: str):
        row = self._rows.get(case_id)
        return row.model_copy(deep=True) if row is not None else None

    async def save(self, case: Case, *, reports=(), checkpoints=()) -> Case:
        for armed in list(self._failures):
            when, exc = armed
            if when(case):
                self._failures.remove(armed)
                raise exc
        self._rows[case.case_id] = case.model_copy(deep=True)
        return case

    def row(self) -> Case:
        return self._rows[CASE_ID]


def _service(store: _Store) -> InvestigationService:
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("no route under test reaches the LLM")
    )
    return InvestigationService(engine, store)


async def _turn(svc: InvestigationService, query: str, intent: QueryIntent = None):
    return await svc.process_turn(
        case_id=CASE_ID,
        user_id=USER_ID,
        payload=TurnPayload(query=query, intent=intent),
    )


def _click_confirm(case: Case) -> QueryIntent:
    """The Yes card's intent as the engine offers it on ``case``, forwarded
    verbatim as clients do: it names ``case``'s standing offer (#1812)."""
    return QueryIntent(**_resolution_confirmation_suggestions(case)[0]["intent"])


@pytest.fixture
def counters():
    with (
        patch.object(turn_messages, "terminal_confirmation_total") as confirmation,
        patch.object(turn_messages, "terminal_followup_total") as followup,
    ):
        yield confirmation, followup


def _offer_the_resolution_cards(store: _Store) -> None:
    """Put the confirmation pair on offer, as the proposing turn stored it."""
    row = store.row()
    row.last_suggestions = [
        {**card, "offered_turn": row.current_turn}
        for card in _resolution_confirmation_suggestions(row)
    ]


def _spy_on_the_engine(svc: InvestigationService) -> AsyncMock:
    spy = AsyncMock(wraps=svc.engine.process_turn)
    svc.engine.process_turn = spy
    return spy


def _mint(svc: InvestigationService, card: dict) -> AsyncMock:
    """Have the intent resolver mint ``card``'s intent from whatever is typed,
    and return a spy on the engine that shows what reached the gate."""
    svc.intent_resolver.resolve = AsyncMock(return_value=card["intent"])
    return _spy_on_the_engine(svc)


class TestTheConfirmationCounter:
    async def test_a_typed_ok_counts_as_weak(self, counters):
        confirmation, followup = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), "ok")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        confirmation.labels.return_value.inc.assert_called_once()
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        followup.labels.assert_not_called()  # the confirming turn itself

    @pytest.mark.parametrize("message", ["yes", "go ahead"])
    async def test_a_typed_bare_explicit_token_counts_as_explicit(
        self, counters, message
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), message)

        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
        )

    # #1783: a typed reply that says more than one token executes nothing, so it
    # counts nothing, whether what follows consents ("ok go ahead") or refuses
    # ("ok, don't close it yet"). These turns were ``weak_prefixed`` and
    # ``explicit_prefixed``; both labels are gone.
    @pytest.mark.parametrize(
        "message",
        [
            "ok go ahead",
            "sure, close it",
            "ok yes",
            "lgtm, confirmed",
            "yes please close it",
            "ok, don't close it yet",
        ],
    )
    async def test_a_token_that_says_more_executes_and_counts_nothing(
        self, counters, message
    ):
        confirmation, followup = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), message)

        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition["to_state"] == "resolved"
        assert store.row().turn_history[-1].terminal_confirmed_via is None
        confirmation.labels.assert_not_called()
        followup.labels.assert_not_called()

    async def test_a_confirming_turn_whose_commit_fails_is_not_counted(self, counters):
        """Review F12, pinning what the metrics doc says, under #1882's single
        commit. The gate's transition commits in the turn's one commit, so when
        that commit conflicts nothing of the turn is stored: the case is still
        INVESTIGATING with the offer standing, no record carries a channel, and
        nothing is counted. The user's retry of the same "ok" is therefore the
        confirmation, not a duplicate of one — it executes and is counted once.
        (Before #1882 the engine had already saved the transition, so the retry
        landed on a RESOLVED case and the confirmation was never counted.)"""
        confirmation, followup = counters
        store = _Store(_investigating_case())
        svc = _service(store)
        store.fail_next_save(StaleCaseException(CASE_ID, 3, 4))

        with pytest.raises(StaleCaseException):
            await _turn(svc, "ok")

        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition
        assert not any(t.terminal_confirmed_via for t in store.row().turn_history)
        confirmation.labels.assert_not_called()
        followup.labels.assert_not_called()

        await _turn(svc, "ok")

        assert store.row().state == CaseState.RESOLVED
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        followup.labels.assert_not_called()

    async def test_an_ok_the_resolver_minted_into_an_intent_is_still_weak(
        self, counters
    ):
        """Review F2: a typed "ok" the resolver turns into a confirmation
        intent is a typed "ok", not a click."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        engine = _spy_on_the_engine(svc)

        await _turn(svc, "ok")

        # The mint happened and reached the gate as an intent — otherwise this
        # measures the plain typed path and cannot tell a click from a mint —
        # and ``typed`` travelled as the engine's own keyword, never inside the
        # client-filled ``intent_data`` (review F10).
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["typed"] is True
        assert "typed" not in engine.await_args.kwargs["intent_data"]
        # A bare token, so the minted confirmation executes (#1783).
        assert store.row().state == CaseState.RESOLVED
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )

    async def test_typed_text_the_resolver_accepted_with_no_token_executes_nothing(
        self, counters
    ):
        """#1783: what was ``typed_other``. The resolver minted a confirmation
        from "that works", which is not a click, and the text is no bare
        token, so the proposal is re-asked and nothing is counted."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        engine = _mint(svc, CONFIRM_CARD)
        assert confirmation_token_class("that works", "resolved") is None

        await _turn(svc, "that works")

        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["typed"] is True
        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition["to_state"] == "resolved"
        confirmation.labels.assert_not_called()

    async def test_a_click_counts_as_intent(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        svc = _service(store)
        engine = _spy_on_the_engine(svc)

        await _turn(svc, CONFIRM_CARD["payload"], intent=_click_confirm(store.row()))

        assert engine.await_args.kwargs["typed"] is False
        assert "typed" not in engine.await_args.kwargs["intent_data"]
        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(via="intent", to_state="resolved")
        assert store.row().turn_history[-1].terminal_confirmed_via == "intent"

    async def test_a_confirmed_close_counts_as_closed(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))

        await _turn(_service(store), "yes")

        assert store.row().state == CaseState.CLOSED
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="closed"
        )

    async def test_the_inv37_pivot_counts_nothing(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))
        pivot = MagicMock(verdict=tt.ClosureReadiness.SUGGEST_RESOLVE, message="r")

        with patch.object(tt, "assess_closure_readiness", return_value=pivot):
            await _turn(_service(store), "ok")

        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition["to_state"] == "resolved"
        assert store.row().turn_history[-1].terminal_confirmed_via is None
        confirmation.labels.assert_not_called()

    async def test_a_raising_counter_never_fails_the_turn(self, counters):
        confirmation, _ = counters
        confirmation.labels.side_effect = RuntimeError("registry down")
        store = _Store(_investigating_case())

        response = await _turn(_service(store), "ok")

        confirmation.labels.assert_called_once()  # it did raise, here
        assert response.agent_response
        assert store.row().state == CaseState.RESOLVED


#: The signature an ENGINE proposer writes on its offer, which a refusal is
#: recorded against.
_SIGNATURE = "SUGGEST_RESOLVE|1|chain"


def _sign_the_offer(store: _Store) -> None:
    """Sign the standing proposal as an engine proposer would. Without it,
    "nothing recorded" would hold vacuously: the thin case here derives no
    signature, so even a real refusal would record nothing."""
    store.row().pending_transition["justifying_signature"] = _SIGNATURE


def _assert_re_asked(store: _Store, response) -> None:
    """The proposal was shown again with its buttons, still stands, executed
    nothing, and recorded no refusal."""
    row = store.row()
    assert row.state == CaseState.INVESTIGATING
    assert row.pending_transition is not None
    assert row.pending_transition["to_state"] == "resolved"
    assert "Please select one of the options above" in response.agent_response
    assert [action.label for action in response.suggested_actions] == [
        card["label"] for card in _resolution_confirmation_suggestions(row)
    ]
    assert row.progress.deferred_disposition_declined_signatures == []
    assert row.turn_history[-1].terminal_confirmed_via is None


class TestOnlyAClickOrABareTokenExecutes:
    """#1783, ruling (a). A pending terminal proposal executes on its click or
    on a BARE typed consent token, and on nothing else, whatever the intent
    resolver minted from the text. Every other answer that is not a decline is
    re-asked with the proposal's buttons, never reaches the LLM (the harness's
    generator raises if it does), and records no refusal, every time it is
    sent."""

    async def test_a_typed_refusal_after_a_weak_token_is_re_asked(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)
        svc = _service(store)

        response = await _turn(svc, "ok, don't close it yet")

        _assert_re_asked(store, response)
        svc.engine.generator.generate_structured_output.assert_not_called()
        confirmation.labels.assert_not_called()

    @pytest.mark.parametrize("message", ["ok, don't close it yet", "hmm"])
    async def test_the_same_non_answer_sent_twice_is_re_asked_twice(
        self, counters, message
    ):
        """The one-re-present cap (#656) withdrew the proposal, and recorded a
        refusal, on a second non-answer. A re-ask never becomes a decline."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)
        svc = _service(store)

        for _ in range(2):
            response = await _turn(svc, message)
            _assert_re_asked(store, response)

        svc.engine.generator.generate_structured_output.assert_not_called()
        confirmation.labels.assert_not_called()

    async def test_a_minted_confirmation_on_more_than_a_token_is_re_asked(
        self, counters
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)
        _offer_the_resolution_cards(store)
        svc = _service(store)
        engine = _mint(svc, CONFIRM_CARD)

        response = await _turn(svc, "ok go ahead")

        # The mint reached the gate as a typed confirmation, so the re-ask is
        # the gate's answer to it, not to plain text.
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["intent_data"]["value"] is True
        assert engine.await_args.kwargs["typed"] is True
        _assert_re_asked(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_minted_decline_on_a_bare_token_is_re_asked(self, counters):
        """The text says yes and the mint says no: they disagree, so neither
        wins. The card is shown again, and nothing is recorded."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)
        _offer_the_resolution_cards(store)
        svc = _service(store)
        engine = _mint(svc, DECLINE_CARD)

        response = await _turn(svc, "ok")

        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["intent_data"]["value"] is False
        assert engine.await_args.kwargs["typed"] is True
        _assert_re_asked(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_curly_apostrophe_consent_executes(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), "that\u2019s right")

        assert store.row().state == CaseState.RESOLVED
        assert store.row().turn_history[-1].terminal_confirmed_via == "explicit_token"
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
        )

    async def test_close_it_does_not_confirm_a_pending_resolve(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)

        response = await _turn(_service(store), "close it")

        _assert_re_asked(store, response)
        confirmation.labels.assert_not_called()

    async def test_close_it_closes_a_pending_close(self, counters):
        """The positive control for the row above. The case is not resolvable,
        so INV-37 does not pivot the close to a resolve."""
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))

        await _turn(_service(store), "close it")

        assert store.row().state == CaseState.CLOSED
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="closed"
        )

    async def test_a_bare_no_still_declines_and_records_the_refusal(self, counters):
        """An explicit decline stays a decline. Also the positive control for
        every "nothing recorded" above: on this harness a refusal records."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _sign_the_offer(store)

        response = await _turn(_service(store), "no")

        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition is None
        assert row.progress.deferred_disposition_declined_signatures == [_SIGNATURE]
        assert "Please select one of the options above" not in response.agent_response
        confirmation.labels.assert_not_called()


class TestTheFollowUpCounter:
    async def _resolved_on_ok(self, counters) -> tuple[_Store, InvestigationService]:
        store = _Store(_investigating_case())
        svc = _service(store)
        await _turn(svc, "ok")
        assert store.row().state == CaseState.RESOLVED
        counters[1].labels.assert_not_called()
        return store, svc

    async def test_only_the_first_message_after_counts_even_on_the_greeting_route(
        self, counters
    ):
        """Review F4: a greeting never reaches the engine, and is counted."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await _turn(svc, "hello")
        # The orientation lane answered it, not the engine's terminal branch.
        assert store.row().messages[-2]["metadata"].get("orientation") == "greeting"
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

        await _turn(svc, "hello")
        followup.labels.assert_called_once()  # still once: not the second message

    async def test_a_follow_up_whose_save_fails_counts_once_on_its_retry(
        self, counters
    ):
        """Review F3: counted after the save, so a retried turn counts once."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        store.fail_next_save(StaleCaseException(CASE_ID, 3, 4))

        with pytest.raises(StaleCaseException):
            await _turn(svc, "hello")
        followup.labels.assert_not_called()

        await _turn(svc, "hello")
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

    async def test_a_click_after_the_confirmation_is_not_a_follow_up(self, counters):
        """Review F10: the confirmation card clicked again is not a user who
        kept talking to the case."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, *, plan, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        # The Yes card that stood before the typed "ok" resolved the case.
        stale_click = _click_confirm(_investigating_case())
        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, CONFIRM_CARD["payload"], intent=stale_click)

        answered.assert_awaited_once()  # the click reached the terminal case
        followup.labels.assert_not_called()

    async def test_a_runbook_card_click_is_not_a_follow_up(self, counters):
        """Review F5: the ack turn's runbook card carries no intent and arrives
        as its payload text. The terminal handler's own recogniser says so."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        created = AsyncMock(
            side_effect=lambda case, metadata, *, plan, dedup_confirmed=False: {
                "agent_response": "Creating your runbook draft.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(
            svc.engine.terminal.runbooks, "handle_runbook_creation", new=created
        ):
            await _turn(svc, GENERATE_RUNBOOK_PAYLOAD)

        created.assert_awaited_once()  # the text reached the card's branch
        followup.labels.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [REGENERATE_RESOLUTION_SUMMARY_PAYLOAD, REGENERATE_CLOSURE_SUMMARY_PAYLOAD],
    )
    async def test_a_regenerate_card_click_is_not_a_follow_up(self, counters, payload):
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        regenerated = AsyncMock(
            side_effect=lambda case, metadata, *, plan: {
                "agent_response": "Regenerated.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(
            TerminalTurnHandler, "_handle_report_regeneration", new=regenerated
        ):
            await _turn(svc, payload)

        regenerated.assert_awaited_once()
        followup.labels.assert_not_called()

    @pytest.mark.parametrize("query", ["", "   "])
    async def test_an_empty_turn_is_not_a_follow_up(self, counters, query):
        """Review F3: an empty turn (a bare Slack mention) is an orientation
        request, answered on the greeting route with no intent."""
        _, followup = counters
        store = _Store(_investigating_case("closed"))
        svc = _service(store)
        await _turn(svc, "yes")
        assert store.row().state == CaseState.CLOSED

        await _turn(svc, query)

        assert store.row().messages[-2]["metadata"].get("orientation") == "empty"
        followup.labels.assert_not_called()

    async def test_the_runbook_text_on_a_closed_case_is_typed(self, counters):
        """Review F9: the runbook card acts only on a RESOLVED case, so on a
        CLOSED one its text goes to Q&A like any typed message — and is counted
        like one, by the same eligibility the handler dispatches on."""
        _, followup = counters
        store = _Store(_investigating_case("closed"))
        svc = _service(store)
        await _turn(svc, "yes")
        assert store.row().state == CaseState.CLOSED
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, *, plan, user_id=None: {
                "agent_response": "Runbooks need a resolved case.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, GENERATE_RUNBOOK_PAYLOAD)

        answered.assert_awaited_once()  # Q&A answered it: not a card here
        followup.labels.assert_called_once_with(via="explicit_token", to_state="closed")

    async def _answered_on_terminal(self, svc, message):
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, *, plan, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )
        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, message)
        answered.assert_awaited_once()

    @pytest.mark.parametrize("again", ["ok", "OK", " ok "])
    async def test_a_resubmitted_confirmation_is_not_a_follow_up(self, counters, again):
        """Review F1 of e98382937: a double submit carries nothing new."""
        confirmation, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await self._answered_on_terminal(svc, again)

        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        followup.labels.assert_not_called()

    async def test_a_different_message_after_the_confirmation_is_a_follow_up(
        self, counters
    ):
        """The positive control for the resubmission screen."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await self._answered_on_terminal(svc, "wait, it's still failing")

        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")

    async def test_a_client_sent_greeting_is_typed(self, counters):
        """Review F8: the service ignores a client-sent GREETING and re-derives
        the intent from the text, so the user typed "hi" — the effective intent
        is None even though the request carried one."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await _turn(svc, "hi", intent=QueryIntent(type=IntentType.GREETING))

        assert store.row().messages[-2]["metadata"].get("orientation") == "greeting"
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")


_SERIES_CHILD = r"""
import json
from typing import get_args

from prometheus_client import REGISTRY

import faultmaven.core.investigation.lifecycle_metrics as lifecycle_metrics

series = {}
for family in REGISTRY.collect():
    if family.name not in (
        "faultmaven_terminal_confirmation",
        "faultmaven_terminal_followup",
    ):
        continue
    for sample in family.samples:
        if sample.name.endswith("_total"):
            key = f"{family.name}|{sample.labels['via']}|{sample.labels['to_state']}"
            series[key] = sample.value
print("@@RESULT@@" + json.dumps({"file": lifecycle_metrics.__file__, "series": series}))
"""


def test_every_series_is_exposed_at_zero_before_any_turn():
    """Review F2 of e98382937: a series born by its first increment is born at
    1, and the documented ``increase()`` query never sees that event. Every
    child of both counters must exist at 0 from import.

    A subprocess, because the shim decides real-or-NoOp when the counters are
    CREATED, at import: ``ENABLE_METRICS`` has to be set before that, and
    ``prometheus-client`` is a cloud extra (``.venv-cloud``), so the
    standalone leg skips.
    """
    pytest.importorskip("prometheus_client")
    repo_root = Path(__file__).resolve().parents[4]
    env = dict(os.environ)
    env["ENABLE_METRICS"] = "true"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH")) if p
    )

    proc = subprocess.run(
        [sys.executable, "-c", _SERIES_CHILD],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith("@@RESULT@@")
    )
    result = json.loads(line[len("@@RESULT@@") :])

    # The child measured THIS tree, not an editable install elsewhere.
    assert result["file"].startswith(str(repo_root))
    expected = {
        f"{family}|{via}|{to_state}": 0.0
        for family in (
            "faultmaven_terminal_confirmation",
            "faultmaven_terminal_followup",
        )
        for via in get_args(TerminalConfirmedVia)
        for to_state in (CaseState.RESOLVED.value, CaseState.CLOSED.value)
    }
    assert result["series"] == expected
