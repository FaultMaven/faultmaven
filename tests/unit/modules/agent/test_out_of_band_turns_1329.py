"""#1329 — an out-of-band message is answered outside the investigation.

Reported: a haiku request mid-investigation ran the full pipeline — the daily
tenant turn was charged, the case context and forced tools were spent on it,
and it landed in the investigation history. These tests drive the real
``InvestigationService.process_turn`` over the recording repository and the
real ``TurnCapService`` with an in-memory ledger, with the two LLM calls the
lane makes (one-token triage, short answer) doubled on the router.

What must hold: the ledger is untouched; the engine is never called; the
message clock STILL advances by one (every persisted exchange does — the
#500/#1264 invariant); the recorded turn carries ``OUT_OF_BAND`` and is
excluded from the investigation-turn count the response reports. And the
controls: an attachment, a typed answer to an offered choice, a pending gate
or a "1" from the triage all keep the old, charged path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TurnCapService,
    utc_day,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.agent.domain.services.out_of_band import (
    TRIAGE_MAX_TOKENS,
    OutOfBandKind,
)
from faultmaven.modules.case.domain.models import CaseState, TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit

ORG = "org-personal"
HAIKU = (
    "Forget the server for a second. Can you write a haiku about a sleepy cat, "
    "and also tell me what the capital of Australia is?"
)
ANSWER = "Soft paws, warm sunbeam... Canberra. Shall we get back to the OOM kills?"


class _Orgs:
    async def get_organization(self, organization_id):
        return SimpleNamespace(organization_id=organization_id, daily_turn_cap=None)


class _People:
    async def is_personal_organization(self, organization_id):
        return True


def _cap(ledger):
    return TurnCapService(
        CapPolicyResolver(
            _People(), _Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True
        ),
        ledger,
    )


@pytest.fixture(autouse=True)
def bound_tenant():
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import set_current_org_id

    set_current_org_id(ORG)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _router(triage_verdict: str):
    """One router double serves both calls; the triage is told apart by its cap."""

    async def route(**kwargs):
        if kwargs.get("max_tokens") == TRIAGE_MAX_TOKENS:
            return SimpleNamespace(content=triage_verdict)
        return SimpleNamespace(content=ANSWER)

    router = MagicMock()
    router.route = AsyncMock(side_effect=route)
    return router


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
    return double


@pytest.fixture
def case(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.organization_id = ORG
    sample_case.title = "Nightly OOM kills of postgres"
    sample_case.inquiry.proposed_problem_statement = "Nightly OOM kills"
    sample_case.inquiry.problem_statement_confirmed = True
    sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    sample_case.inquiry.decided_to_investigate = True
    sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)
    sample_case.state = CaseState.INVESTIGATING
    # Turn 1 already consumed AND recorded, so the persisted clock (the
    # recording repository writes ``effective_current_turn``) reads 1 and the
    # turn under test is 2 — a seeded message at turn 1 with an unrecorded
    # clock would collide with it.
    sample_case.current_turn = 1
    sample_case.turn_history = [
        TurnProgress(
            turn_number=1,
            timestamp=datetime.now(timezone.utc),
            progress_made=False,
            outcome=TurnOutcome.DATA_REQUESTED,
            user_message_summary="postgres OOM-killed nightly",
            agent_response_summary="Could you share dmesg?",
        )
    ]
    sample_case.messages = [
        {
            "turn_number": 1,
            "role": "user",
            "content": "postgres OOM-killed nightly",
            "message_type": "user_query",
        },
        {
            "turn_number": 1,
            "role": "assistant",
            "content": "Could you share dmesg?",
            "message_type": "agent_response",
        },
    ]
    return sample_case


def _service(engine, repo, ledger, verdict="2"):
    engine.llm_provider = _router(verdict)
    return InvestigationService(
        milestone_engine=engine, case_repository=repo, turn_cap=_cap(ledger)
    )


async def _turn(service, repo, case, query=HAIKU, **payload):
    await repo.save(case)
    before = repo._storage[case.case_id].current_turn
    resp = await service.process_turn(
        case_id=case.case_id,
        user_id=case.user_id,
        payload=TurnPayload(query=query, **payload),
    )
    return resp, before, repo._storage[case.case_id]


class TestOutOfBandTurn:
    async def test_not_charged_and_engine_untouched(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger)
        resp, before, saved = await _turn(service, recording_case_repository, case)
        assert await ledger.usage(ORG, utc_day()) == 0
        engine.process_turn.assert_not_called()
        assert resp.agent_response == ANSWER

    async def test_message_clock_still_advances_and_turn_is_recorded_out_of_band(
        self, engine, recording_case_repository, case
    ):
        service = _service(engine, recording_case_repository, InMemoryTurnLedger())
        resp, before, saved = await _turn(service, recording_case_repository, case)
        assert saved.current_turn == before + 1
        assert resp.turn_number == before + 1
        assert saved.turn_history[-1].turn_number == before + 1
        assert saved.turn_history[-1].outcome == TurnOutcome.OUT_OF_BAND
        assert saved.turn_history[-1].progress_made is False
        user_rows = [
            m
            for m in saved.messages
            if m["role"] == "user" and m["turn_number"] == before + 1
        ]
        agent_rows = [
            m
            for m in saved.messages
            if m["role"] == "assistant" and m["turn_number"] == before + 1
        ]
        assert len(user_rows) == 1 and len(agent_rows) == 1
        assert user_rows[0]["metadata"]["out_of_band"] == "off_topic"
        assert agent_rows[0]["metadata"]["out_of_band"] == "off_topic"

    async def test_investigation_turn_excludes_the_aside(
        self, engine, recording_case_repository, case
    ):
        service = _service(engine, recording_case_repository, InMemoryTurnLedger())
        resp, before, saved = await _turn(service, recording_case_repository, case)
        assert resp.investigation_turn == saved.investigation_turn_count
        assert resp.investigation_turn == resp.turn_number - 1

    async def test_follow_ups_point_back_to_the_case(
        self, engine, recording_case_repository, case
    ):
        service = _service(engine, recording_case_repository, InMemoryTurnLedger())
        resp, *_ = await _turn(service, recording_case_repository, case)
        labels = [a.label for a in resp.suggested_actions]
        assert any(label.startswith("Back to: Nightly OOM kills") for label in labels)

    async def test_agent_meta_is_free_without_a_triage_call(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger, verdict="1")
        resp, before, saved = await _turn(
            service, recording_case_repository, case, query="What model are you?"
        )
        assert await ledger.usage(ORG, utc_day()) == 0
        engine.process_turn.assert_not_called()
        caps = [
            c.kwargs.get("max_tokens") for c in engine.llm_provider.route.call_args_list
        ]
        assert TRIAGE_MAX_TOKENS not in caps
        assert (
            saved.messages[-2]["metadata"]["out_of_band"]
            == OutOfBandKind.AGENT_META.value
        )


class TestControls:
    async def test_triage_one_keeps_the_charged_engine_path(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger, verdict="1")
        resp, before, saved = await _turn(service, recording_case_repository, case)
        assert await ledger.usage(ORG, utc_day()) == 1
        engine.process_turn.assert_called_once()
        assert saved.turn_history[-1].outcome != TurnOutcome.OUT_OF_BAND
        assert resp.investigation_turn == resp.turn_number

    async def test_an_attachment_is_never_triaged(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger, verdict="2")
        service.preprocessing_service = None
        service._preprocess_attachment = AsyncMock(
            return_value=SimpleNamespace(
                uploaded_file=None,
                classification_failed=True,
                suggested_types=None,
                attachment_filename="x.log",
            )
        )
        try:
            await _turn(
                service,
                recording_case_repository,
                case,
                attachments=[
                    Attachment(
                        content=b"log", filename="x.log", content_type="text/plain"
                    )
                ],
            )
        except Exception:
            pass  # the stubbed preprocessing is not the subject; the charge is
        assert await ledger.usage(ORG, utc_day()) == 1
        caps = [
            c.kwargs.get("max_tokens") for c in engine.llm_provider.route.call_args_list
        ]
        assert TRIAGE_MAX_TOKENS not in caps

    async def test_a_pending_gate_reply_is_never_triaged(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger, verdict="2")
        case.pending_transition = {"to_state": "resolved"}
        await _turn(
            service, recording_case_repository, case, query="write me a haiku instead"
        )
        assert await ledger.usage(ORG, utc_day()) == 1
        engine.process_turn.assert_called_once()

    async def test_a_typed_answer_to_an_offered_choice_is_incident_work(
        self, engine, recording_case_repository, case
    ):
        """Resolution runs BEFORE triage and its verdict is reused, not repeated."""
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger, verdict="2")
        resolved = {"type": "conversation"}
        service.intent_resolver.resolve = AsyncMock(return_value=resolved)
        case.last_suggestions = [
            {
                "label": "Return to OOM remediation",
                "payload": "Return to OOM remediation",
                "action_type": "DECIDE",
                "intent": resolved,
                "offered_turn": case.current_turn,
            }
        ]
        await _turn(
            service,
            recording_case_repository,
            case,
            query="return to the remediation please",
        )
        assert await ledger.usage(ORG, utc_day()) == 1
        engine.process_turn.assert_called_once()
        assert service.intent_resolver.resolve.await_count == 1

    async def test_triage_failure_is_charged(
        self, engine, recording_case_repository, case
    ):
        ledger = InMemoryTurnLedger()
        service = _service(engine, recording_case_repository, ledger)
        engine.llm_provider.route = AsyncMock(
            side_effect=RuntimeError("classifier down")
        )
        await _turn(service, recording_case_repository, case)
        assert await ledger.usage(ORG, utc_day()) == 1
        engine.process_turn.assert_called_once()
