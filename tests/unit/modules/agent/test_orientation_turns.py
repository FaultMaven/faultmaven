"""Orientation turns through the real ``process_turn``.

Pins the three enhancements to the static greeting: the intent is
server-minted only (a client-sent GREETING is re-derived from the text),
"help" and an EMPTY message are orientation turns too, and the reply is
built from the case's state rather than from onboarding boilerplate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TurnCapService,
    utc_day,
)
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import CaseState, TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit

ORG = "org-personal"


class _Orgs:
    async def get_organization(self, organization_id):
        return SimpleNamespace(organization_id=organization_id, daily_turn_cap=None)


class _People:
    async def is_personal_organization(self, organization_id):
        return True


@pytest.fixture(autouse=True)
def bound_tenant():
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
    from faultmaven.config.tenant_context import set_current_enterprise_id

    set_current_enterprise_id(ORG)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
def engine():
    double = create_autospec(MilestoneEngine, instance=True)

    async def spy(
        *,
        case,
        user_message,
        attachments=None,
        intent_type=None,
        intent_data=None,
        user_id=None,
    ):
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": "engine answer",
            "metadata": {"milestones_completed": [], "progress_made": False},
        }

    double.process_turn = AsyncMock(side_effect=spy)
    router = MagicMock()
    router.route = AsyncMock(
        side_effect=AssertionError("no LLM call expected on an orientation turn")
    )
    double.llm_provider = router
    return double


@pytest.fixture
def ledger():
    return InMemoryTurnLedger()


@pytest.fixture
def service(engine, recording_case_repository, ledger):
    cap = TurnCapService(
        CapPolicyResolver(
            _People(), _Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True
        ),
        ledger,
    )
    return InvestigationService(
        milestone_engine=engine, case_repository=recording_case_repository, turn_cap=cap
    )


@pytest.fixture
def investigating(sample_case, sample_user_id):
    c = sample_case
    c.user_id = sample_user_id
    c.organization_id = ORG
    c.title = "Nightly OOM kills of postgres"
    c.inquiry.proposed_problem_statement = "Nightly OOM kills"
    c.inquiry.problem_statement_confirmed = True
    c.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    c.inquiry.decided_to_investigate = True
    c.inquiry.decision_made_at = datetime.now(timezone.utc)
    c.state = CaseState.INVESTIGATING
    c.current_turn = 1
    c.turn_history = [
        TurnProgress(
            turn_number=1,
            timestamp=datetime.now(timezone.utc),
            progress_made=False,
            outcome=TurnOutcome.DATA_REQUESTED,
            user_message_summary="here is dmesg",
            agent_response_summary="Could you share free -m from db-01?",
        )
    ]
    c.messages = [
        {
            "turn_number": 1,
            "role": "user",
            "content": "here is dmesg",
            "message_type": "user_query",
            "metadata": {},
        },
        {
            "turn_number": 1,
            "role": "assistant",
            "content": "Postgres was the OOM victim. Could you share free -m from db-01?",
            "message_type": "agent_response",
            "metadata": {},
        },
    ]
    return c


@pytest.fixture
def fresh(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.organization_id = ORG
    return sample_case


async def _turn(service, repo, case, **payload):
    await repo.save(case)
    resp = await service.process_turn(
        case_id=case.case_id, user_id=case.user_id, payload=TurnPayload(**payload)
    )
    return resp, repo._storage[case.case_id]


class TestStateAware:
    async def test_hi_mid_investigation_recaps_the_case(
        self, service, recording_case_repository, investigating, engine
    ):
        resp, saved = await _turn(
            service, recording_case_repository, investigating, query="hi"
        )
        assert resp.agent_response.startswith(
            "Hello! We're investigating “Nightly OOM kills of postgres”"
        )
        assert "Where we left off: Postgres was the OOM victim." in resp.agent_response
        assert "describe the problem" not in resp.agent_response
        engine.process_turn.assert_not_called()
        assert saved.messages[-1]["metadata"]["orientation"] == "greeting"
        # Recorded and tagged as an aside (PR #1343 review): not investigation
        # work, hidden from the history the engine sees next turn.
        assert saved.turn_history[-1].outcome == TurnOutcome.OUT_OF_BAND
        assert saved.messages[-2]["metadata"]["out_of_band"] == "orientation"
        assert saved.messages[-1]["metadata"]["out_of_band"] == "orientation"
        assert resp.investigation_turn == 1
        assert resp.turn_number == 2

    async def test_a_pending_terminal_proposal_goes_to_the_engine(
        self, service, recording_case_repository, investigating, engine
    ):
        """ "?" / "hi" / "" over a standing resolve proposal answers THAT question;
        the engine's gate handling owns it (PR #1343 review)."""
        investigating.pending_transition = {"to_state": "resolved"}
        for query in ("hi", "", "help"):
            engine.process_turn.reset_mock()
            await _turn(service, recording_case_repository, investigating, query=query)
            engine.process_turn.assert_called_once()

    async def test_help_is_orientation_not_onboarding(
        self, service, recording_case_repository, investigating, engine
    ):
        resp, saved = await _turn(
            service, recording_case_repository, investigating, query="help"
        )
        assert resp.agent_response.startswith(
            "I can investigate a problem you describe"
        )
        assert "We're investigating" in resp.agent_response
        engine.process_turn.assert_not_called()
        assert saved.messages[-1]["metadata"]["orientation"] == "help"

    async def test_hi_on_a_fresh_case_is_onboarding(
        self, service, recording_case_repository, fresh
    ):
        resp, _ = await _turn(service, recording_case_repository, fresh, query="hi")
        assert resp.agent_response.startswith(
            "Hello! I'm FaultMaven, your AI troubleshooting copilot."
        )
        assert "describe the problem" in resp.agent_response


class TestEmptyMessage:
    async def test_empty_turn_is_answered_charged_and_recorded(
        self, service, recording_case_repository, investigating, engine, ledger
    ):
        resp, saved = await _turn(
            service, recording_case_repository, investigating, query=None
        )
        assert resp.agent_response.startswith(
            "We're investigating “Nightly OOM kills of postgres”"
        )
        assert await ledger.usage(ORG, utc_day()) == 1
        engine.process_turn.assert_not_called()
        assert saved.current_turn == 2
        assert saved.turn_history[-1].turn_number == 2
        assert saved.messages[-2]["content"] == ""
        assert saved.messages[-1]["metadata"]["orientation"] == "empty"

    async def test_whitespace_counts_as_empty(
        self, service, recording_case_repository, investigating, engine
    ):
        resp, _ = await _turn(
            service, recording_case_repository, investigating, query="   "
        )
        assert resp.agent_response.startswith("We're investigating")
        engine.process_turn.assert_not_called()


class TestClientSentGreetingIsNotHonoured:
    async def test_real_text_with_a_greeting_intent_goes_to_the_engine(
        self, service, recording_case_repository, investigating, engine
    ):
        resp, _ = await _turn(
            service,
            recording_case_repository,
            investigating,
            query="the restart did not help, still OOM at 02:00",
            intent=QueryIntent(type=IntentType.GREETING),
        )
        engine.process_turn.assert_called_once()
        assert (
            engine.process_turn.call_args.kwargs["intent_type"]
            == IntentType.CONVERSATION.value
        )
        assert resp.agent_response == "engine answer"

    async def test_greeting_text_with_a_greeting_intent_is_still_a_greeting(
        self, service, recording_case_repository, investigating, engine
    ):
        resp, _ = await _turn(
            service,
            recording_case_repository,
            investigating,
            query="hello",
            intent=QueryIntent(type=IntentType.GREETING),
        )
        engine.process_turn.assert_not_called()
        assert resp.agent_response.startswith("Hello! We're investigating")


class TestTerminal:
    async def test_greeting_on_a_resolved_case(
        self, service, recording_case_repository, investigating, engine
    ):
        case = investigating.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
                "closed_at": datetime.now(timezone.utc),
            }
        )
        resp, _ = await _turn(service, recording_case_repository, case, query="hi")
        assert (
            "This case is resolved: “Nightly OOM kills of postgres”."
            in resp.agent_response
        )
        engine.process_turn.assert_not_called()
