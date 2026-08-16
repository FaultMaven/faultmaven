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

import io
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
    router,
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
        ["Case-260101-1", "Case-260816-12", "Case-991231-999", "  Case-260101-1  "],
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
            "Case-2601-1",  # wrong date width
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
            organization_id=TENANT_ORG,
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
            organization_id=TENANT_ORG,
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
                organization_id=TENANT_ORG,
                case_service=service,
                llm_provider=None,
            )

        llm.assert_not_awaited()
        service.update_case.assert_not_awaited()
        assert _is_default_case_title(case.title)

    @pytest.mark.asyncio
    async def test_binds_the_captured_tenant_before_touching_the_database(self):
        """The org must be bound at the moment of the read, not merely at request time.

        Started from the Standalone default — the value an *unbound* context would
        also produce — so the assertion can only pass if the task really re-bound
        the tenant it was handed.
        """
        set_current_org_id(STANDALONE_ORG_ID)
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case)

        seen = {}

        async def _get_case(case_id, user_id=None):
            seen.setdefault("read", get_current_org_id())
            return case

        async def _update(case_id, updates, user_id=None):
            seen["write"] = get_current_org_id()
            case.title = updates["title"]
            return True

        service.get_case = AsyncMock(side_effect=_get_case)
        service.update_case = AsyncMock(side_effect=_update)

        await _auto_title_case_if_default(
            case_id=case.case_id,
            user_id="user_123",
            organization_id=TENANT_ORG,
            case_service=service,
            llm_provider=None,
        )

        assert seen["read"] == TENANT_ORG
        assert seen["write"] == TENANT_ORG

    @pytest.mark.asyncio
    async def test_a_missing_case_is_not_an_error(self):
        service = _service_for(_make_case())
        service.get_case = AsyncMock(return_value=None)

        await _auto_title_case_if_default(
            case_id="case_000000000000",
            user_id="user_123",
            organization_id=TENANT_ORG,
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
            organization_id=TENANT_ORG,
            case_service=service,
            llm_provider=None,
        )

    @pytest.mark.asyncio
    async def test_a_refused_gate_never_escapes_the_task(self):
        case = _make_case()
        service = _service_for(case)

        with patch(
            "faultmaven.modules.case.api.routes._generate_and_persist_title",
            new=AsyncMock(side_effect=ValidationException("too thin")),
        ):
            await _auto_title_case_if_default(
                case_id=case.case_id,
                user_id="user_123",
                organization_id=TENANT_ORG,
                case_service=service,
                llm_provider=None,
            )


# ==============================================================================
# End-to-end through the real response cycle
# ==============================================================================


@pytest.mark.unit
class TestTurnEndpointSchedulesAutoTitling:
    """Drive the mounted route so the background task runs for real.

    ``TestClient`` executes background tasks as part of the response cycle, so a
    title that changed by the time the call returns is positive evidence that the
    task was scheduled *and* executed — not that ``add_task`` was called with
    plausible arguments.
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
        """A turn that never produced an answer has nothing new to name from."""
        response = client.post(f"/api/v1/cases/{case.case_id}/turns", data={})

        assert response.status_code == 400
        assert _is_default_case_title(case.title)

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
