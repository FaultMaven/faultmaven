"""K13 (#1812): through ``POST /api/v1/cases/{case_id}/turns``, the key a
confirmation card names reaches the engine, which executes the click only when
it names the offer standing.

The body is Slack-shaped: the card's intent forwarded verbatim, plus
``"user_confirmed": true`` (``faultmaven-slack-agent`` ``rendering.py``). The
route pops ``type`` and builds ``QueryIntent`` from the rest, and ``QueryIntent``
has no ``extra`` config, so a key it did not declare would be dropped here
silently and every click refused.

The mounted router, the real ``InvestigationService`` and the real engine run;
auth, the case service's access check and the repository are doubled, and the
LLM seam raises, because no path under test reaches it.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import faultmaven.modules.case.api.routes.conversation as conversation
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    STALE_OFFER_LINE,
)
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress

pytestmark = pytest.mark.integration

CASE_ID = "case_1812eeeeeeee"
USER_ID = "user_1812"


def _case_with_a_resolve_offer() -> Case:
    case = Case(
        case_id=CASE_ID,
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id=USER_ID,
        enterprise_id="org_1812",
        description="checkout 503s",
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 503",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "checkout 503s"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    propose_transition(case, to_state="resolved", summary="Resolve it?")
    return case


class _Store:
    """A repository as a database behaves: a save stores a snapshot."""

    def __init__(self, case: Case) -> None:
        self._row = case.model_copy(deep=True)

    async def get(self, case_id: str):
        return self._row.model_copy(deep=True) if case_id == CASE_ID else None

    async def save(self, case: Case, *, reports=(), receipt=None) -> Case:
        self._row = case.model_copy(deep=True)
        return case

    def row(self) -> Case:
        return self._row


@pytest.fixture
def mounted():
    store = _Store(_case_with_a_resolve_offer())
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("no path under test reaches the LLM")
    )
    engine_spy = AsyncMock(wraps=engine.process_turn)
    engine.process_turn = engine_spy
    service = InvestigationService(engine, store)

    case_service = MagicMock()
    case_service.get_case = AsyncMock(side_effect=lambda cid, uid: store.row())

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.llm_provider = None
    user = UserDTO(
        user_id=USER_ID,
        username="slack",
        email="slack@example.com",
        display_name="Slack",
        is_active=True,
    )
    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: service

    with patch.object(conversation, "_auto_title_case_if_default", new=AsyncMock()):
        # A scratch app built here, listed as such in
        # tests/unit/architecture/test_app_boot_is_shared.py.
        with TestClient(app) as client:
            yield client, store, engine_spy


def _post_click(client, card: dict):
    """The card clicked in Slack: its intent forwarded, plus user_confirmed."""
    return client.post(
        f"/api/v1/cases/{CASE_ID}/turns",
        data={
            "query": card["payload"],
            "intent_type": card["intent"]["type"],
            "intent_data": json.dumps({**card["intent"], "user_confirmed": True}),
        },
    )


def test_the_card_key_reaches_the_engine_and_the_click_executes(mounted):
    client, store, engine_spy = mounted
    yes = _resolution_confirmation_suggestions(store.row())[0]
    key = yes["intent"]["proposal_id"]
    assert key == store.row().pending_transition["proposed_at"]

    response = _post_click(client, yes)

    assert response.status_code == 200, response.text
    assert engine_spy.await_args.kwargs["intent_data"]["proposal_id"] == key
    assert engine_spy.await_args.kwargs["typed"] is False
    assert store.row().state == CaseState.RESOLVED
    assert store.row().turn_history[-1].terminal_confirmed_via == "intent"


def test_a_card_for_another_offer_executes_nothing(mounted):
    """The control: the same route, a card naming an offer that is not the
    standing one. Without it, the test above would pass on a route that drops
    the key and an engine that ignores it."""
    client, store, _ = mounted
    before = dict(store.row().pending_transition)
    stale = _resolution_confirmation_suggestions(store.row())[0]
    stale["intent"]["proposal_id"] = "2026-01-01T00:00:00+00:00"

    response = _post_click(client, stale)

    assert response.status_code == 200, response.text
    assert store.row().state == CaseState.INVESTIGATING
    assert store.row().pending_transition == before
    assert response.json()["agent_response"].startswith(STALE_OFFER_LINE)
