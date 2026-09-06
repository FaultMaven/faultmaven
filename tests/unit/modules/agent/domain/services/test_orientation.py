"""Orientation turns — greeting, "help", the empty message — answered from case state."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultmaven.modules.agent.domain.services.orientation import (
    OrientationKind,
    back_to_investigation_follow_up,
    build_orientation,
    detect_orientation,
    last_investigation_message,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
    InvestigationStage,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


class TestDetect:
    @pytest.mark.parametrize(
        "text",
        [
            "hi",
            "Hello!",
            "hey faultmaven",
            "Greetings.",
            "hello there",
            "good morning FaultMaven",
            "yo",
        ],
    )
    def test_greetings(self, text):
        assert detect_orientation(text) == OrientationKind.GREETING

    @pytest.mark.parametrize(
        "text",
        [
            "help",
            "Help me",
            "help please",
            "what can you do?",
            "how can you help",
            "HELP FaultMaven!",
        ],
    )
    def test_help(self, text):
        assert detect_orientation(text) == OrientationKind.HELP

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty(self, text):
        assert detect_orientation(text) == OrientationKind.EMPTY

    @pytest.mark.parametrize(
        "text",
        [
            "Hi, the db is down",
            "help, nginx is returning 502s",
            "what should I do next?",
            "yes",
            "hello world is printed twice in the log",
            "can you help me read this dmesg?",
            # PR #1343 review: next-step questions and a bare "?" belong to the
            # engine (which has its own handling for "?" over a pending gate).
            "?",
            "??",
            "What should I do?",
            "how does this work?",
            "what do you do",
        ],
    )
    def test_incident_text_falls_through(self, text):
        assert detect_orientation(text) is None


def _inquiry(proposed=None, confirmed=False):
    return Case(
        case_id="case_aabb11223344",
        title="Nightly OOM kills of postgres",
        description="d",
        user_id="u",
        organization_id="o",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(
            proposed_problem_statement=proposed, problem_statement_confirmed=confirmed
        ),
    )


def _investigating(needs=(), messages=()):
    case = Case(
        case_id="case_aabb11223344",
        title="Nightly OOM kills of postgres",
        description="d",
        user_id="u",
        organization_id="o",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Nightly OOM kills",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        current_turn=6,
    )
    case.problem_verification = ProblemVerification(
        symptom_statement="postgres OOM-killed nightly", severity="HIGH"
    )
    case.messages = list(messages)
    # Duck-typed needs are enough: the recap reads ``state`` and ``request_text``.
    case.__dict__["_needs"] = list(needs)
    return case


class TestBuild:
    def test_fresh_inquiry_greeting_is_onboarding(self):
        reply = build_orientation(_inquiry(), OrientationKind.GREETING)
        assert reply["agent_response"].startswith(
            "Hello! I'm FaultMaven, your AI troubleshooting copilot."
        )
        assert "describe the problem" in reply["agent_response"]
        assert [f["action_type"] for f in reply["suggested_follow_ups"]] == [
            "FREE_SPEECH",
            "EVIDENCE",
        ]

    def test_fresh_inquiry_help_and_empty_lead_with_capabilities(self):
        for kind in (OrientationKind.HELP, OrientationKind.EMPTY):
            text = build_orientation(_inquiry(), kind)["agent_response"]
            assert "I can investigate a problem you describe" in text
            assert "Hello!" not in text

    def test_proposed_statement_awaiting_confirmation(self):
        reply = build_orientation(
            _inquiry(proposed="Checkout pods crash-loop after deploy"),
            OrientationKind.GREETING,
        )
        assert "about to confirm the problem statement" in reply["agent_response"]
        assert "Checkout pods crash-loop after deploy" in reply["agent_response"]
        assert "describe the problem" not in reply["agent_response"]

    def test_investigating_recaps_instead_of_onboarding(self, monkeypatch):
        case = _investigating(
            messages=[
                {
                    "role": "assistant",
                    "content": "The kernel log shows postgres was the OOM victim. Could you share free -m from db-01?",
                    "metadata": {},
                },
                {
                    "role": "user",
                    "content": "tell me a joke",
                    "metadata": {"out_of_band": "off_topic"},
                },
                {
                    "role": "assistant",
                    "content": "Why did the pod get evicted? ...",
                    "metadata": {"out_of_band": "off_topic"},
                },
            ]
        )
        reply = build_orientation(case, OrientationKind.GREETING)
        text = reply["agent_response"]
        assert text.startswith(
            "Hello! We're investigating “Nightly OOM kills of postgres” — diagnosing the cause."
        )
        assert (
            "Where we left off: The kernel log shows postgres was the OOM victim."
            in text
        )
        assert "pod get evicted" not in text  # the aside is skipped
        assert "describe the problem" not in text
        labels = [f["label"] for f in reply["suggested_follow_ups"]]
        assert labels == ["Back to: Nightly OOM kills of postgres"]

    def test_empty_message_mid_investigation_is_a_bare_recap(self):
        text = build_orientation(_investigating(), OrientationKind.EMPTY)[
            "agent_response"
        ]
        assert text.startswith("We're investigating")
        assert "Hello" not in text and "I can investigate" not in text

    @pytest.mark.parametrize(
        "state, word", [(CaseState.RESOLVED, "resolved"), (CaseState.CLOSED, "closed")]
    )
    def test_terminal_case(self, state, word):
        case = _investigating().model_copy(
            update={
                "state": state,
                "resolved_at": datetime.now(timezone.utc),
                "closed_at": datetime.now(timezone.utc),
            }
        )
        reply = build_orientation(case, OrientationKind.GREETING)
        assert (
            f"This case is {word}: “Nightly OOM kills of postgres”."
            in reply["agent_response"]
        )
        assert "open a new case" in reply["agent_response"]
        assert "describe the problem" not in reply["agent_response"]
        assert [f["action_type"] for f in reply["suggested_follow_ups"]] == [
            "FREE_SPEECH"
        ]
