"""The turn endpoint binds the deadline the LLM retry ladder budgets against.

The deadline-aware ladder is only as good as its binding. Everything in
``turn_budget`` reads ``None`` — "no ceiling, spend freely" — outside a bound
turn, which is exactly right for background jobs and direct-call tests and
exactly wrong if the one production caller forgets to bind. That makes this the
seam the whole fix hangs on: without it every budget test in
``tests/integration/core/test_llm_ladder_turn_budget.py`` still passes and the
running system is unchanged (#1278, #1292).

Asserted from INSIDE ``process_turn``, because that is where the ladder reads it
from, and against the SAME resolved ceiling ``asyncio.wait_for`` is given —
a binding that used a different number would guard against the wrong deadline.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.turn_budget import (
    TURN_BUDGET_RESERVE_SECONDS,
    remaining_turn_budget,
)
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import (
    _resolve_agent_timeout,
    submit_turn,
)
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models import Case

CASE_ID = "case_dead1ead1abc"


def _case() -> Case:
    return Case(
        case_id=CASE_ID,
        # Not the ``Case-YYMMDD-N`` placeholder, so auto-titling is a no-op and
        # this test measures only the binding.
        title="Checkout API 502s",
        description="",
        user_id="user_123",
        enterprise_id="org_test",
        state=CaseState.INQUIRY,
    )


def _user() -> UserDTO:
    return UserDTO(
        user_id="user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        is_active=True,
    )


async def _submit(process_turn) -> TurnResponse:
    case = _case()
    case_service = AsyncMock()
    case_service.get_case = AsyncMock(return_value=case)

    investigation_service = MagicMock()
    investigation_service.process_turn = process_turn

    request = MagicMock()
    request.app.state.llm_provider = None

    return await submit_turn(
        case_id=CASE_ID,
        request=request,
        query="The checkout API is throwing 502s.",
        files=[],
        pasted_content=None,
        intent_type=None,
        intent_data=None,
        input_type=None,
        source_url=None,
        case_service=case_service,
        investigation_service=investigation_service,
        current_user=_user(),
    )


def _ok() -> TurnResponse:
    return TurnResponse(
        agent_response="Looking into it.",
        turn_number=1,
        milestones_completed=[],
        case_state=CaseState.INQUIRY,
        progress_made=True,
    )


@pytest.mark.unit
class TestTheTurnEndpointBindsItsDeadline:
    @pytest.mark.asyncio
    async def test_process_turn_can_see_the_deadline(self):
        """Not "bind_turn_deadline was called" — what the ladder actually reads."""
        seen = {}

        async def process_turn(**_):
            seen["remaining"] = remaining_turn_budget()
            return _ok()

        await _submit(AsyncMock(side_effect=process_turn))

        assert seen["remaining"] is not None, (
            "process_turn ran with no turn deadline bound: the retry ladder "
            "inside it would budget against nothing"
        )

    @pytest.mark.asyncio
    async def test_the_deadline_is_the_ceiling_wait_for_will_enforce(self):
        """The binding and the cancellation must be the same number.

        ``_resolve_agent_timeout`` applies the per-provider
        ``AGENT_PROVIDER_TIMEOUT_OVERRIDES`` map; a binding that used the bare
        ``agent_request_timeout`` instead would budget a hung provider against a
        deadline that is not the one about to cancel it.
        """
        from faultmaven.config.settings import get_settings

        expected, _provider = _resolve_agent_timeout(get_settings())
        seen = {}

        async def process_turn(**_):
            seen["remaining"] = remaining_turn_budget()
            return _ok()

        await _submit(AsyncMock(side_effect=process_turn))

        # Bound just before the wait_for starts, so it is at most the ceiling and
        # only microseconds under it.
        assert expected - 1.0 < seen["remaining"] <= expected

    @pytest.mark.asyncio
    async def test_there_is_a_usable_budget_left_after_the_reserve(self):
        """A binding that left nothing spendable would refuse every LLM call.

        ``agent_request_timeout`` is constrained ``ge=30`` and the reserve is a
        second, so this holds by construction — but it holds only while the two
        are related, and nothing else in the codebase relates them.
        """
        seen = {}

        async def process_turn(**_):
            seen["remaining"] = remaining_turn_budget()
            return _ok()

        await _submit(AsyncMock(side_effect=process_turn))

        assert seen["remaining"] - TURN_BUDGET_RESERVE_SECONDS > 0

    @pytest.mark.asyncio
    async def test_the_deadline_is_bound_during_the_turn_and_not_after(self):
        """Auto-titling runs after the ``wait_for`` and has its own timeout.

        Charging it to the turn budget would make a slow turn's titling refuse
        to run — and, worse, leave a spent deadline bound in this worker's
        context for whatever ran next.

        Asserted as bound-THEN-unbound in one test on purpose. "Unbound
        afterwards" alone is satisfied by never binding at all, so it would go
        on passing if the binding were removed — a guard that cannot fail when
        the thing it guards is deleted.
        """
        seen = {}

        async def process_turn(**_):
            seen["during"] = remaining_turn_budget()
            return _ok()

        await _submit(AsyncMock(side_effect=process_turn))

        assert seen["during"] is not None
        assert remaining_turn_budget() is None


def _settings(provider, overrides):
    """A settings double whose two timeout maps disagree, so a resolver that
    read the wrong one is visible in the number."""
    return SimpleNamespace(
        llm=SimpleNamespace(provider=provider),
        agent=SimpleNamespace(
            agent_request_timeout=120,
            timeout_for_provider=lambda name: overrides.get(name, 120),
        ),
    )


@pytest.mark.unit
class TestResolvingTheCeilingThatGetsBound:
    """``_resolve_agent_timeout`` now shares the LLM router's provider resolver.

    It had no direct test before this change, and sharing is only safe if the
    shared helper resolves the name exactly as the inlined copy did. The three
    shapes that copy handled are a str-enum field, a plain string, and neither
    (env fallback) — and the resolved name is what indexes
    ``AGENT_PROVIDER_TIMEOUT_OVERRIDES``, so getting it wrong silently binds the
    wrong deadline rather than raising.
    """

    def test_a_str_enum_provider_reaches_the_override_map(self):
        settings = _settings(SimpleNamespace(value="gemini"), {"gemini": 240})
        assert _resolve_agent_timeout(settings) == (240.0, "gemini")

    def test_a_plain_string_provider_reaches_the_override_map(self):
        settings = _settings("openai", {"openai": 300})
        assert _resolve_agent_timeout(settings) == (300.0, "openai")

    def test_an_unlisted_provider_takes_the_global_ceiling(self):
        settings = _settings("groq", {"gemini": 240})
        assert _resolve_agent_timeout(settings) == (120.0, "groq")

    def test_the_env_var_is_the_fallback_when_the_field_is_unset(self, monkeypatch):
        """Settings doubles that lack the field are why the env fallback exists."""
        monkeypatch.setenv("CHAT_PROVIDER", "anthropic")
        settings = _settings(None, {"anthropic": 200})
        assert _resolve_agent_timeout(settings) == (200.0, "anthropic")

    def test_no_provider_anywhere_is_reported_as_default(self, monkeypatch):
        monkeypatch.delenv("CHAT_PROVIDER", raising=False)
        settings = _settings(None, {})
        assert _resolve_agent_timeout(settings) == (120.0, "default")
