"""The user's own words to the assistant are never offered as output (#2718).

The model names blocks of system output in the user's message; a reply that
quotes the assistant ("You asked for "…" — …", "I haven't made "…"") was named
as output and offered back for saving as data. A located block is dropped, and
counted ``block_user_prose``, when every non-blank line holds a double-quoted
or backticked span and opens with ``You`` plus a speech verb or with a
first-person ``I`` subject. One line of any other shape keeps the block, and
the second-person and level-letter lines programs print stay offered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.core.investigation.chat_data import mint_chat_data_offer
from faultmaven.core.investigation.schemas import ChatDataBlock
from faultmaven.modules.case.contracts import Case
from tests.case_builders import investigating_case

pytestmark = pytest.mark.unit

# The two messages of the report, as the user typed them.
_REPORT_ONE_LINE = (
    "I'd want to know the root cause, what points to it, and what rules out "
    'the alternatives, so I haven\'t made "re-enabling time sync on app-02" '
    'and "forcing an immediate clock step".'
)
_REPORT_THREE_PARAGRAPHS = (
    "You asked for \"whether app-02's time-sync service was disabled or "
    'changed around the failure window" — which one do you mean?\n'
    "\n"
    'You asked for "the chronyd journal from app-02" — I can\'t get that for '
    "you right now.\n"
    "\n"
    'You asked for "it used as evidence" — I can\'t get that for you right now.'
)

_LOG = "2026-11-24T10:00:01Z ERROR db timeout after 30s"


def _case() -> Case:
    return investigating_case(
        case_id="case_000000002718",
        enterprise_id="e",
        current_turn=4,
        confirm_after_build=True,
        statement="first logins fail with 401",
    )


def _whole(message: str) -> ChatDataBlock:
    """The model names the whole message as one block."""
    lines = [line for line in message.splitlines() if line.strip()]
    return ChatDataBlock(first_line=lines[0], last_line=lines[-1])


def _mint(message: str, named, **kwargs):
    counter = MagicMock()
    with patch(
        "faultmaven.core.investigation.chat_data.CHAT_DATA_OFFER_EVENTS_TOTAL",
        counter,
    ):
        case = kwargs.pop("case", None) or _case()
        offer = mint_chat_data_offer(
            case, user_message=message, named_blocks=named, **kwargs
        )
    return case, offer, _prose_count(counter)


def _prose_count(counter: MagicMock) -> int:
    """How many ``block_user_prose`` events were counted: each ``labels()``
    call is followed by its ``inc()``."""
    events = [call.kwargs["event"] for call in counter.labels.call_args_list]
    incs = counter.labels.return_value.inc.call_args_list
    return sum(
        inc.args[0] if inc.args else 1
        for event, inc in zip(events, incs)
        if event == "block_user_prose"
    )


class TestTheReportedMessagesAreNotOffered:
    @pytest.mark.parametrize(
        "message", [_REPORT_ONE_LINE, _REPORT_THREE_PARAGRAPHS], ids=["one", "three"]
    )
    def test_named_whole_they_mint_no_offer(self, message):
        case, offer, dropped = _mint(message, [_whole(message)])

        assert offer is None
        assert case.chat_data_offers == []
        assert dropped == 1


@pytest.mark.parametrize(
    "line",
    [
        # Accepted drops: second-person speech or first-person prose around a
        # double-quoted span, output quoted inline included (owner ruling).
        'You said "yes" to deleting 3 volumes. Proceeding.',
        'I get "FATAL: password authentication failed for user app" when I connect',
        'I ran "kubectl get pods" and got: error: the server has no resource "pods"',
        "I’ve applied the change you suggested: “kubectl rollout restart deploy/x”.",
        "I haven’t made `sudo chronyc makestep` yet",
        'you told me to run "timedatectl status" first',
    ],
)
def test_a_line_of_the_users_speech_is_dropped(line):
    _, offer, dropped = _mint(f"so:\n{line}", [_whole(line)])
    assert offer is None
    assert dropped == 1


@pytest.mark.parametrize(
    "output",
    [
        "You have an error in your SQL syntax; check the manual that corresponds "
        "to your MySQL server version for the right syntax to use near 'SELEC' at "
        "line 1",
        'You are now connected to database "prod" as user "app".',
        "You are in 'detached HEAD' state.",
        "Your branch is ahead of 'origin/main' by 2 commits.",
        "You should consider upgrading via the '/usr/bin/python3 -m pip install "
        "--upgrade pip' command.",
        "You must specify a region. You can also configure your region by running "
        '"aws configure".',
        "You asked to pull from the remote 'origin', but did not specify a branch.",
        "You asked me to deploy 'api' to 'prod-eu'. Deploying now.",
        "Access is denied.",
        "Permission denied (publickey).",
        "Login failed for user 'sa'.",
        "Job for nginx.service failed because the control process exited with "
        'error code.\nSee "systemctl status nginx.service" and "journalctl -xe" '
        "for details.",
        "The term 'kubectl' is not recognized as the name of a cmdlet, function, "
        "script file, or operable program.",
        'i  functions: Watching "/home/u/app/functions" for Cloud Functions...',
        'I (5012) wifi station: connected to ap SSID:"plant-floor"',
        "I tensorflow/core/common_runtime/placer.cc:54] save/Const: (Const): "
        '"/job:localhost/replica:0/task:0/device:CPU:0"',
        'I 2026-11-24 10:00:01 worker Started job "nightly-export"',
        '    I reverted "bump chrony to 4.5"',
        'I1124 10:00:01.123456 1 controller.go:88] syncing "orders/processing"',
        'I/ActivityManager( 123): Start proc "com.acme"',
        'I/O error on device "sda"',
        'I: Retrieving "http://deb.debian.org/debian/dists/bookworm/InRelease"',
        f'You asked for "the logs" — here they are:\n{_LOG}',
    ],
)
def test_output_a_program_printed_is_still_offered(output):
    message = f"got this:\n{output}\nany idea?"
    _, offer, dropped = _mint(message, [_whole(output)])
    assert offer is not None
    assert offer.blocks == [output]
    assert dropped == 0


class TestTheDropLeavesTheRestOfTheTurn:
    def test_a_state_report_still_mints_the_request(self):
        case, offer, dropped = _mint(
            _REPORT_ONE_LINE,
            [_whole(_REPORT_ONE_LINE)],
            state_report=SimpleNamespace(request="the output of chronyc tracking"),
        )

        assert dropped == 1
        assert offer is not None
        assert offer.blocks == []
        assert offer.request == "the output of chronyc tracking"
        assert case.chat_data_offers == [offer]

    def test_a_prose_block_beside_an_output_block_offers_the_output_only(self):
        prose = 'You asked for "the db log" — here it is:'
        message = f"{prose}\n\n{_LOG}"
        _, offer, dropped = _mint(message, [_whole(prose), _whole(_LOG)])

        assert offer is not None
        assert offer.blocks == [_LOG]
        assert dropped == 1

    def test_a_dropped_block_does_not_hide_the_same_text_in_a_later_block(self):
        """The dropped block is not taken as offered: the same line, later in
        the message and followed by output, is offered with it."""
        prose = 'I\'ve run "psql -c select 1" again'
        message = f"{prose}\n\nand the full run:\n{prose}\n{_LOG}"
        _, offer, dropped = _mint(
            message,
            [_whole(prose), ChatDataBlock(first_line=prose, last_line=_LOG)],
        )

        assert offer is not None
        assert offer.blocks == [f"{prose}\n{_LOG}"]
        assert dropped == 1

    def test_the_guard_reads_the_located_typed_text_after_reversal(self):
        """The model saw a redacted message; its anchors carry a placeholder
        that reads as no one's speech. The block the engine locates in what
        the user typed, after reversal, is the user's, and is dropped."""
        typed = 'I\'ve applied "ntpdate 10.4.2.17" like you said'
        _, offer, dropped = _mint(
            f"{typed}\n\n(and a chip the user chose)",
            [ChatDataBlock(first_line="<REDACTED_1>", last_line="<REDACTED_1>")],
            typed_message=typed,
            reverse=lambda text: text.replace("<REDACTED_1>", typed),
        )

        assert offer is None
        assert dropped == 1
