"""Server-side auto-titling of cases still holding the ``Case-YYMMDD-N`` placeholder.

Titling policy has always lived on the server, but the *trigger* used to live in
each client — and two of the three never pulled it. The Slack agent and the
Dashboard never called ``POST /cases/{case_id}/title`` at all, so their cases kept
the placeholder forever; the Copilot re-implemented the threshold in TypeScript.
fm#1069. The trigger now sits behind the turn endpoint, which every client goes
through, so the policy actually applies everywhere.

Two properties carry the design and are tested here rather than assumed:

1. **It really runs, through the real ASGI stack.** ``BackgroundTasks`` is not a
   promise the endpoint makes; it is behaviour of the response cycle. The tests in
   ``TestTurnEndpointSchedulesAutoTitling`` drive a mounted app with ``TestClient``
   and assert the title changed *after* the response, not that ``add_task`` was
   called.
2. **The tenant is still bound when the write happens.** ``get_current_org_id`` is
   total — an unbound context answers the Standalone org rather than failing — so
   a task that lost the binding would not raise, it would quietly address the
   wrong tenant. The org in force at the moment of the write is asserted directly.
"""

import asyncio
import io
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.exceptions import ValidationException
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import (
    _auto_title_case_if_default,
    _di_get_case_service_dependency,
    _is_default_case_title,
    _TitleSubstanceTooThin,
    router,
    submit_turn,
)
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models import Case

TENANT_ORG = "org_tenant_alpha"


def _make_case(**overrides) -> Case:
    defaults = {
        "case_id": f"case_{uuid4().hex[:12]}",
        "title": "Case-260101-1",
        "description": "",
        "user_id": "user_123",
        "organization_id": TENANT_ORG,
        "state": CaseState.INQUIRY,
    }
    defaults.update(overrides)
    return Case(**defaults)


def _service_for(case: Case) -> AsyncMock:
    """Case service that serves ``case`` and applies title writes to it."""
    service = AsyncMock()
    service.get_case = AsyncMock(return_value=case)
    service.get_case_conversation_context = AsyncMock(return_value="")

    async def _update(case_id, updates, user_id=None):
        if "title" in updates:
            case.title = updates["title"]
        return True

    service.update_case = AsyncMock(side_effect=_update)
    return service


# ==============================================================================
# The cost bound
# ==============================================================================


@pytest.mark.unit
class TestIsDefaultCaseTitle:
    """The placeholder test is the *only* thing bounding auto-titling cost.

    A case is titled at most once because this stops answering True the moment a
    real title lands. Nothing else limits it — the ``title_generation`` preset in
    ``config/protection.py`` is configured but never checked (fm#985 item 12), so
    a design leaning on that limiter would have no bound at all.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Case-260101-1",
            "Case-260816-12",
            "Case-991231-999",
            "  Case-260101-1  ",
            # The pre-1519b1ec (2026-01-28) 4-digit MMDD form. Every case created
            # before that day still carries it; matching only the current width
            # would leave exactly those rows unnameable forever.
            "Case-1106-1",
            "Case-0127-1",
        ],
    )
    def test_placeholders_are_recognised(self, title):
        assert _is_default_case_title(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "Postgres connection pool exhaustion",
            # Anchored on BOTH ends: a user-chosen title that merely starts or
            # ends like the placeholder must never be silently overwritten.
            "Case-260101-1 follow-up",
            "Re: Case-260101-1",
            "Case-26011-1",  # 5 digits — neither the MMDD nor the YYMMDD width
            "Case-260101",  # no sequence
            "case-260101-1",  # lowercase is not what create_case writes
            "New Chat",
            "",
            None,
        ],
    )
    def test_real_titles_are_left_alone(self, title):
        assert _is_default_case_title(title) is False


# ==============================================================================
# The task itself
# ==============================================================================


@pytest.mark.unit
class TestAutoTitleTask:
    @pytest.mark.asyncio
    async def test_titles_a_placeholder_case_with_substance(self):
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)

        await _auto_title_case_if_default(
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=None,
        )

        assert not _is_default_case_title(case.title)
        service.update_case.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_titled_case_is_left_alone_without_reading_context(self):
        """The cost bound: a named case costs one read and stops.

        Asserting that the *context* was never fetched — not just that no write
        happened — is what pins the short-circuit. A version that generated a
        title and then declined to save it would also leave the title intact, and
        would spend an LLM call on every turn forever.
        """
        case = _make_case(title="Postgres connection pool exhaustion")
        service = _service_for(case)

        await _auto_title_case_if_default(
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=None,
        )

        assert case.title == "Postgres connection pool exhaustion"
        service.get_case_conversation_context.assert_not_awaited()
        service.update_case.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thin_case_is_refused_without_an_llm_call(self):
        """A case too thin to name costs reads, not tokens.

        This is what makes it safe to attempt on every turn: the substance gate
        refuses *before* ``_generate_title_with_llm`` is reached.
        """
        case = _make_case()
        service = _service_for(case)

        with patch(
            "faultmaven.modules.case.api.routes._generate_title_with_llm",
            new=AsyncMock(),
        ) as llm:
            await _auto_title_case_if_default(
                case_id=case.case_id,
                user_id="user_123",
                case_service=service,
                llm_provider=None,
            )

        llm.assert_not_awaited()
        service.update_case.assert_not_awaited()
        assert _is_default_case_title(case.title)

    @pytest.mark.asyncio
    async def test_a_titler_that_stops_answering_loses_rather_than_delays(self):
        """The attempt sits on the turn's critical path, so it must be bounded.

        Without the timeout a hung provider would hold a turn's answer open for
        the whole client wait — the turn succeeded, and only its name failed.
        """
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)

        async def _never_returns(*args, **kwargs):
            await asyncio.sleep(3600)

        with (
            patch(
                "faultmaven.modules.case.api.routes._generate_and_persist_title",
                new=AsyncMock(side_effect=_never_returns),
            ),
            patch(
                "faultmaven.modules.case.api.routes.AUTO_TITLE_TIMEOUT_SECONDS", 0.05
            ),
        ):
            await asyncio.wait_for(
                _auto_title_case_if_default(
                    case_id=case.case_id,
                    user_id="user_123",
                    case_service=service,
                    llm_provider=None,
                ),
                timeout=5,
            )

        assert _is_default_case_title(case.title)

    @pytest.mark.asyncio
    async def test_a_missing_case_is_not_an_error(self):
        service = _service_for(_make_case())
        service.get_case = AsyncMock(return_value=None)

        await _auto_title_case_if_default(
            case_id="case_000000000000",
            user_id="user_123",
            case_service=service,
            llm_provider=None,
        )

        service.update_case.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_write_never_escapes_the_task(self):
        """Nothing about a turn may be reported as failed because naming failed.

        The task runs after the response, so a raised exception would surface as
        an unhandled error in the server log for a turn the user saw succeed.
        """
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)
        service.update_case = AsyncMock(side_effect=RuntimeError("database is gone"))

        await _auto_title_case_if_default(
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=None,
        )

    @pytest.mark.asyncio
    async def test_a_refused_gate_never_escapes_the_task(self):
        case = _make_case()
        service = _service_for(case)

        with patch(
            "faultmaven.modules.case.api.routes._generate_and_persist_title",
            new=AsyncMock(side_effect=_TitleSubstanceTooThin("too thin")),
        ):
            await _auto_title_case_if_default(
                case_id=case.case_id,
                user_id="user_123",
                case_service=service,
                llm_provider=None,
            )

    @pytest.mark.asyncio
    async def test_a_broken_titler_is_reported_while_a_thin_case_is_not(self, caplog):
        """The gate refusing and the titler failing must not look the same.

        Both refuse to produce a title and both are a 422 to a client, but one is
        the gate working and the other is the titler broken. Auto-titling runs
        unattended on every turn, so a systematically failing titler that logged
        at DEBUG — alongside every ordinary thin case — would emit nothing at all
        at default level.
        """
        case = _make_case()
        service = _service_for(case)

        # The gate refusing: quiet.
        with patch(
            "faultmaven.modules.case.api.routes._generate_and_persist_title",
            new=AsyncMock(side_effect=_TitleSubstanceTooThin("too thin")),
        ):
            with caplog.at_level(logging.WARNING):
                await _auto_title_case_if_default(
                    case_id=case.case_id,
                    user_id="user_123",
                    case_service=service,
                    llm_provider=None,
                )
        assert caplog.records == []

        # Generation failing (LLM *and* extractive fallback): visible. This is a
        # bare ValidationException — the same type the gate used to raise.
        with patch(
            "faultmaven.modules.case.api.routes._generate_and_persist_title",
            new=AsyncMock(
                side_effect=ValidationException(
                    "Cannot generate meaningful title from available context"
                )
            ),
        ):
            with caplog.at_level(logging.WARNING):
                await _auto_title_case_if_default(
                    case_id=case.case_id,
                    user_id="user_123",
                    case_service=service,
                    llm_provider=None,
                )
        assert any("Auto-titling failed" in r.message for r in caplog.records)


# ==============================================================================
# End-to-end through the real response cycle
# ==============================================================================


@pytest.mark.unit
class TestTurnEndpointNamesTheCase:
    """Drive the mounted route so the titling runs through the real request cycle.

    Note what ``TestClient`` alone cannot tell you: it drains background tasks
    before returning, so "the title changed by the time ``client.post`` returned"
    is equally true of a deferred task. The ordering that matters — the write
    lands *before the turn answers*, which is what stops the next turn's full-row
    save from clobbering it — is pinned by
    ``test_the_write_lands_before_the_turn_answers`` below, which calls the
    handler directly.
    """

    @pytest.fixture
    def case(self):
        c = _make_case()
        c.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        return c

    @pytest.fixture
    def client(self, case):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")  # router already carries /cases

        service = _service_for(case)
        app.state.case_service = service
        app.state.llm_provider = None

        investigation_service = MagicMock()
        investigation_service.process_turn = AsyncMock(
            return_value=TurnResponse(
                agent_response="Looking into it.",
                turn_number=1,
                milestones_completed=[],
                case_state=CaseState.INQUIRY,
                progress_made=True,
            )
        )

        from faultmaven.api.v1.auth_dependencies import require_authentication
        from faultmaven.api.v1.dependencies import get_investigation_service

        user = UserDTO(
            user_id="user_123",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            is_active=True,
        )
        app.dependency_overrides[require_authentication] = lambda: user
        app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
        app.dependency_overrides[get_investigation_service] = (
            lambda: investigation_service
        )

        with TestClient(app) as client:
            client.fm_service = service
            client.fm_investigation_service = investigation_service
            yield client

    def test_a_turn_names_a_placeholder_case(self, client, case):
        assert _is_default_case_title(case.title)

        response = client.post(
            f"/api/v1/cases/{case.case_id}/turns",
            data={"query": "The checkout API is throwing 502s."},
        )

        assert response.status_code == 200
        assert response.json()["turn_number"] == 1
        # The turn's own answer is unchanged by titling...
        assert not _is_default_case_title(
            case.title
        ), "background auto-titling did not run"

    def test_a_turn_leaves_an_already_named_case_alone(self, client, case):
        case.title = "Checkout 502s after v2.1.3"

        response = client.post(
            f"/api/v1/cases/{case.case_id}/turns",
            data={"query": "Any update?"},
        )

        assert response.status_code == 200
        assert case.title == "Checkout 502s after v2.1.3"
        client.fm_service.update_case.assert_not_awaited()

    def test_a_failing_turn_schedules_no_titling(self, client, case):
        """A turn that never produced an answer has nothing new to name from.

        An EMPTY post is no longer the way to make a turn fail — it is accepted
        as an orientation turn now — so the service itself is made to fail.
        """
        client.fm_investigation_service.process_turn.side_effect = RuntimeError(
            "provider down"
        )
        response = client.post(
            f"/api/v1/cases/{case.case_id}/turns",
            data={"query": "The checkout API is throwing 502s."},
        )

        assert response.status_code >= 500
        assert _is_default_case_title(case.title)

    def test_an_empty_turn_is_accepted_by_the_route(self, client, case):
        """No query, no file, no paste: the route no longer answers 400 (#1343,
        contract 2.8.0); the service receives an empty TurnPayload and answers
        it with a state-aware orientation."""
        response = client.post(f"/api/v1/cases/{case.case_id}/turns", data={})

        assert response.status_code == 200
        payload = client.fm_investigation_service.process_turn.await_args.kwargs[
            "payload"
        ]
        assert payload.has_query is False
        assert payload.has_attachments is False

    def test_an_upload_only_turn_names_the_case(self, client, case):
        """The population fm#1069 is about: the user typed nothing, they uploaded.

        Under the old turn gate this case could not be titled until the human had
        typed five times, which an upload-driven investigation may never do.
        """
        response = client.post(
            f"/api/v1/cases/{case.case_id}/turns",
            files={
                "files": ("app.log", io.BytesIO(b"ERROR pool exhausted"), "text/plain")
            },
        )

        assert response.status_code == 200
        assert not _is_default_case_title(case.title)


# ==============================================================================
# The ordering that closes the clobber window
# ==============================================================================


@pytest.mark.unit
class TestTitlingOrdering:
    """The write must land before the turn answers, not merely before the test does.

    The metadata channel skips the version bump on purpose, so the title write is
    invisible to OCC. That cuts both ways: the engine's versioned full-row ``save``
    writes ``title`` from its own in-memory snapshot, so a turn that loaded the
    case *before* the title landed will save the placeholder straight back over it,
    and OCC will not object. Finishing the titling before the response is what
    orders them — a client cannot submit turn N+1 until turn N has answered.

    Calling the handler directly is what makes this checkable. Through
    ``TestClient`` a deferred background task also completes before the call
    returns, so an end-to-end assertion cannot tell the two placements apart.
    """

    @pytest.mark.asyncio
    async def test_the_write_lands_before_the_turn_answers(self):
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)

        investigation_service = MagicMock()
        investigation_service.process_turn = AsyncMock(
            return_value=TurnResponse(
                agent_response="Looking into it.",
                turn_number=1,
                milestones_completed=[],
                case_state=CaseState.INQUIRY,
                progress_made=True,
            )
        )

        request = MagicMock()
        request.app.state.llm_provider = None
        user = UserDTO(
            user_id="user_123",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            is_active=True,
        )

        response = await submit_turn(
            case_id=case.case_id,
            request=request,
            query="The checkout API is throwing 502s.",
            files=[],
            pasted_content=None,
            intent_type=None,
            intent_data=None,
            input_type=None,
            source_url=None,
            case_service=service,
            investigation_service=investigation_service,
            current_user=user,
        )

        # By the time the handler has produced its response object, the name is
        # already persisted. Deferring the titling would leave the placeholder
        # here, and the next turn would load — and re-save — that placeholder.
        assert response.turn_number == 1
        assert not _is_default_case_title(case.title)
        service.update_case.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_titling_inherits_the_request_tenant_binding(self):
        """No explicit re-bind: it runs in the request's own context, so it must
        simply *stay* there.

        ``get_current_org_id`` is total — an unbound context answers the Standalone
        org rather than failing — so a titling call that had drifted out of the
        request context would not raise, it would quietly address the wrong tenant.
        Binding a non-Standalone org and asserting the write saw it is what
        distinguishes "inherited the tenant" from "fell back to the default".
        """
        set_current_org_id(TENANT_ORG)
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)

        seen = {}

        async def _update(case_id, updates, user_id=None):
            seen["write"] = get_current_org_id()
            case.title = updates["title"]
            return True

        service.update_case = AsyncMock(side_effect=_update)

        await _auto_title_case_if_default(
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=None,
        )

        assert seen["write"] == TENANT_ORG
        assert seen["write"] != STANDALONE_ORG_ID
