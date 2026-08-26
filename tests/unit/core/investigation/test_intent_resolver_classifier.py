"""IntentResolver's LLM tier must resolve its settings the way production does.

Regression: `_classify` called ``settings.get_classifier_model()`` on the
top-level ``FaultMavenSettings`` — which has no such attribute (the getter
lives on ``LLMSettings``). The resulting AttributeError was swallowed by the
blanket "classifier failure → safe fallback" except, so the LLM tier of the
resolver had NEVER run in production: every typed response that missed the
exact-match tier silently fell through to conversation. Unit tests didn't
catch it because they mocked settings, and a Mock auto-creates the missing
attribute.

These tests pin:
  1. the settings object used is shaped like production (an object whose
     classifier getter exists ONLY at ``.llm``) — the old code fails this;
  2. the resolved classifier model reaches the router call;
  3. ``CLASSIFIER_PROVIDER`` reaches the router as ``provider_override`` when
     explicitly set, and stays ``None`` (chain routing, today's behavior)
     when not;
  4. the call is budgeted so the tier can actually ANSWER. Resolving the
     settings bug only got as far as making a real API call: at
     ``max_tokens=10`` with no reasoning declaration, a reasoning model spends
     the whole budget on hidden reasoning, the visible body comes back empty,
     and ``_parse_response("")`` returns None — the same "no match" as the
     AttributeError, now billed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import faultmaven.config.settings as settings_module
from faultmaven.config.settings import LLMSettings
from faultmaven.core.investigation.intent_resolver import (
    CLASSIFIER_MAX_TOKENS,
    CLASSIFIER_MIN_OUTPUT_TOKENS,
    IntentResolver,
)
from faultmaven.infrastructure.llm.providers import ReasoningIntent

CHOICES = [
    {
        "label": "Yes, mark as resolved",
        "payload": "resolve it",
        "intent": {"type": "confirm_resolution"},
    },
    {
        "label": "No, keep investigating",
        "payload": "keep going",
        "intent": {"type": "continue_investigation"},
    },
]

# Everything that can steer classifier resolution — cleared so env leaked by
# neighboring test files (CHAT_PROVIDER=local + LOCAL_LLM_MODEL have been seen
# escaping other suites) cannot redirect the assertions.
_ENV_VARS = (
    "CHAT_PROVIDER",
    "CLASSIFIER_PROVIDER",
    "GROQ_CLASSIFIER_MODEL",
    "OPENAI_MODEL",
    "OPENAI_CLASSIFIER_MODEL",
    "LOCAL_LLM_MODEL",
    "LOCAL_LLM_URL",
)


@pytest.fixture
def production_shaped_settings(monkeypatch):
    """A settings object with the getter available ONLY at ``.llm`` — exactly
    the production shape. Deliberately NOT a Mock: a Mock would auto-create
    ``get_classifier_model`` at top level and mask the regression."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")

    settings = SimpleNamespace(llm=LLMSettings())
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    return settings


def _router_returning(content: str) -> AsyncMock:
    router = AsyncMock()
    router.route = AsyncMock(
        return_value=SimpleNamespace(content=content, is_truncated=False)
    )
    return router


@pytest.mark.unit
@pytest.mark.llm
class TestClassifierSettingsResolution:
    @pytest.mark.asyncio
    async def test_classifier_runs_against_production_shaped_settings(
        self, production_shaped_settings
    ):
        """On the pre-fix code this returns None for EVERY input (AttributeError
        swallowed as 'classifier failed'); post-fix the classifier's verdict
        comes back."""
        router = _router_returning("1")
        resolver = IntentResolver(router)

        result = await resolver._classify("go ahead and close this one", CHOICES)

        assert result == {"type": "confirm_resolution"}
        router.route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolved_classifier_model_reaches_the_router(
        self, production_shaped_settings, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_CLASSIFIER_MODEL", "gpt-5.4-mini")
        production_shaped_settings.llm = LLMSettings()

        router = _router_returning("none")
        await IntentResolver(router)._classify("whatever", CHOICES)

        assert router.route.await_args.kwargs["model"] == "gpt-5.4-mini"

    @pytest.mark.asyncio
    async def test_no_override_kwarg_at_all_when_classifier_provider_unset(
        self, production_shaped_settings
    ):
        """With no CLASSIFIER_PROVIDER the call must be byte-identical to the
        pre-role-routing shape: the kwarg is ABSENT, not None — duck-typed
        routers whose route() lacks the parameter (integration test doubles,
        custom LLM_ROUTER_CLASS implementations) must keep working."""
        router = _router_returning("none")
        await IntentResolver(router)._classify("whatever", CHOICES)

        assert "provider_override" not in router.route.await_args.kwargs

    @pytest.mark.asyncio
    async def test_classifier_provider_reaches_router_as_override(
        self, production_shaped_settings, monkeypatch
    ):
        monkeypatch.setenv("CLASSIFIER_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_CLASSIFIER_MODEL", "llama-3.3-70b-versatile")
        production_shaped_settings.llm = LLMSettings()

        router = _router_returning("2")
        result = await IntentResolver(router)._classify("keep digging", CHOICES)

        assert result == {"type": "continue_investigation"}
        kwargs = router.route.await_args.kwargs
        assert kwargs["provider_override"] == "groq"
        assert kwargs["model"] == "llama-3.3-70b-versatile"

    @pytest.mark.asyncio
    async def test_router_failure_still_degrades_to_none(
        self, production_shaped_settings
    ):
        """The safe fallback stays: a real router error means 'no match', it
        must not propagate out of the resolver."""
        router = AsyncMock()
        router.route = AsyncMock(side_effect=RuntimeError("provider down"))

        assert await IntentResolver(router)._classify("whatever", CHOICES) is None


@pytest.mark.unit
@pytest.mark.llm
class TestClassifierCallIsBudgetedToAnswer:
    @pytest.mark.asyncio
    async def test_declares_extraction_and_an_output_floor(
        self, production_shaped_settings
    ):
        """Hidden reasoning bills against the SAME budget as the answer on
        every provider, so a one-token answer still needs the call to say what
        it wants from reasoning (EXTRACTION → the provider's verified minimum)
        and how little visible output it can live with (the floor turns a
        starved body into a raised error instead of a silent None)."""
        router = _router_returning("1")
        await IntentResolver(router)._classify("close it", CHOICES)

        kwargs = router.route.await_args.kwargs
        assert kwargs["reasoning_intent"] is ReasoningIntent.EXTRACTION
        assert kwargs["min_output_tokens"] == CLASSIFIER_MIN_OUTPUT_TOKENS
        assert kwargs["max_tokens"] == CLASSIFIER_MAX_TOKENS

    def test_cap_leaves_room_for_the_reasoning_extraction_only_minimises(self):
        """EXTRACTION resolves to ``"low"``, not ``"none"``, on every family
        outside the verified ones — and low-effort reasoning on a gpt-5.x model
        is hundreds of tokens, all billed against ``max_completion_tokens``. A
        cap sized for the digit alone (the original 10, or a nominal 64) still
        starves the body it was raised to protect."""
        assert CLASSIFIER_MIN_OUTPUT_TOKENS >= 1
        assert CLASSIFIER_MAX_TOKENS >= 128
        assert CLASSIFIER_MAX_TOKENS > CLASSIFIER_MIN_OUTPUT_TOKENS
