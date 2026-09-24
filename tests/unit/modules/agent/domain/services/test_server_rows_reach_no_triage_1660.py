"""The out-of-band triage prompt never quotes a row the server wrote (#1660).

#1451's rule — no row the server wrote is rendered to a model as something a
party SAID — was enforced on five surfaces by PR #1658. The triage classifier
was a sixth: it is shown the assistant's last message, picked by
``orientation.last_investigation_message``, and that helper applied no check.
A turn the model failed to answer therefore put ``[Response withheld by safety
filter]`` in front of the classifier as the assistant's own words.

Skipped, not marked: the classifier answers with one routing digit, so it can
not go on to build on server text as its own; what it needs is what the
assistant last actually asked. Because that can now be an older turn's answer,
the block says "last answer", not "previous message".

Driven through ``OutOfBandTriage.triage``, which is the path that builds the
prompt and routes it, with only the router doubled so the routed prompt can be
read back.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_WITHHELD_TEXT,
)
from faultmaven.modules.agent.domain.services.orientation import (
    EMPTY_AGENT_RESPONSE_TEXT,
    EMPTY_TURN_TEXT,
    last_investigation_message,
)
from faultmaven.modules.agent.domain.services.out_of_band import OutOfBandTriage
from faultmaven.modules.agent.domain.services.query_classifier import classify_query
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_USER_EMPTY,
)

pytestmark = pytest.mark.unit

#: Every text the server writes into an assistant row today — the engine's four
#: stop-reason placeholders and the service backstop's — plus one no writer
#: produces, so an implementation that matches known TEXTS instead of reading
#: the flag fails here the day a fifth placeholder is added.
PLACEHOLDERS = [
    RESPONSE_WITHHELD_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    EMPTY_AGENT_RESPONSE_TEXT,
    "(a placeholder no writer produces yet)",
]

#: A message that reaches the classifier: long enough to pass the word gate,
#: no continuation vocabulary, and the open ``directed_analysis`` bucket.
ASIDE = (
    "Forget the server for a second. Can you write a haiku about a sleepy cat, "
    "and also tell me what the capital of Australia is?"
)

REAL_ANSWER = "Postgres was the OOM victim. Could you share free -m from db-01?"

LABEL = "The assistant's last answer began:"


def _row(role: str, content: str, **metadata) -> dict:
    return {"role": role, "content": content, "metadata": metadata}


def _placeholder_row(text: str) -> dict:
    return _row("assistant", text, **{MESSAGE_METADATA_AGENT_SYNTHESIZED: True})


def _case(messages: list) -> SimpleNamespace:
    """A case under way: two investigation turns behind the in-flight one."""
    return SimpleNamespace(
        title="Nightly OOM kills of postgres",
        state=SimpleNamespace(value="investigating"),
        messages=messages,
        current_turn=3,
        investigation_turn_at=lambda n: max(0, min(n, 3)),
    )


async def _routed_prompt(case) -> str:
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(content="1"))
    await OutOfBandTriage(router).triage(case, ASIDE, classify_query(ASIDE))
    router.route.assert_awaited_once()
    (message,) = router.route.call_args.kwargs["messages"]
    return message["content"]


class TestTheTriagePrompt:
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    async def test_a_placeholder_is_passed_over_for_the_last_real_answer(
        self, placeholder
    ):
        """The newest assistant row is the server's; the one before it is the
        model's. The classifier is shown the model's, labelled as what it is —
        the last ANSWER, which is not the previous message here."""
        prompt = await _routed_prompt(
            _case(
                [
                    _row("user", "here is dmesg"),
                    _row("assistant", REAL_ANSWER),
                    _row("user", "what does that mean for the pool?"),
                    _placeholder_row(placeholder),
                ]
            )
        )
        assert placeholder not in prompt
        # Positive control: the block is still there, carrying a real answer.
        assert f"{LABEL}\n<<<\n{REAL_ANSWER}" in prompt
        assert "previous message" not in prompt

    async def test_with_no_real_answer_there_is_no_last_answer_block(self):
        prompt = await _routed_prompt(
            _case(
                [
                    _row("user", "here is dmesg"),
                    _placeholder_row(RESPONSE_WITHHELD_TEXT),
                ]
            )
        )
        assert RESPONSE_WITHHELD_TEXT not in prompt
        assert LABEL not in prompt

    async def test_an_answer_is_still_quoted(self):
        """The guard reads the flag: an unflagged answer is the model's."""
        prompt = await _routed_prompt(
            _case([_row("user", "here is dmesg"), _row("assistant", REAL_ANSWER)])
        )
        assert REAL_ANSWER in prompt

    async def test_no_stored_user_row_reaches_it(self):
        """The user side (#1434) has no way in: the only user text the triage
        prompt carries is the live message it classifies. Pinned, because a
        prompt that one day adds "the user's previous message" must apply
        ``is_server_written_user_row`` when it does."""
        prompt = await _routed_prompt(
            _case(
                [
                    _row("user", "an earlier user turn zq-7"),
                    _row("assistant", REAL_ANSWER),
                    _row(
                        "user", EMPTY_TURN_TEXT, **{MESSAGE_METADATA_USER_EMPTY: True}
                    ),
                ]
            )
        )
        assert EMPTY_TURN_TEXT not in prompt
        assert "zq-7" not in prompt
        assert ASIDE in prompt


class TestTheSharedHelper:
    """``last_investigation_message`` also feeds the greeting's "Where we left
    off" — its other caller, tested through ``process_turn`` in
    ``test_orientation_turns``."""

    def test_it_returns_none_when_the_model_never_answered(self):
        case = _case([_row("user", "hi"), _placeholder_row(RESPONSE_EMPTY_TEXT)])
        assert last_investigation_message(case) is None

    def test_asides_and_orientation_replies_are_still_passed_over(self):
        """The two exclusions it already had, beside the new one."""
        case = _case(
            [
                _row("assistant", REAL_ANSWER),
                _row(
                    "assistant", "Why did the pod get evicted?", out_of_band="off_topic"
                ),
                _row(
                    "assistant", "Hello! We're investigating…", orientation="greeting"
                ),
                _placeholder_row(RESPONSE_TRUNCATED_TEXT),
            ]
        )
        assert last_investigation_message(case).startswith("Postgres was the OOM")
