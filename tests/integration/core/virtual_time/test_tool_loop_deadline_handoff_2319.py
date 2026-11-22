"""A turn with tools against a hung provider: the tool loop's deadline is handed
to the fallback ladders (#2319).

The tool loop's send runs outside any retry ladder. Before, whatever it failed
with became ``ToolCallingUnsupportedError`` and the fallback ladder met the same
hung provider fresh, granting it the deadline retry it had already spent: about
``T + 2T + 2`` (92 s at T = 30 s, A = 120 s) where a ladder-only turn costs
``2T + 2``. Now the dispatch records each failed send for the judged call bound
around it (the tool loop's send, each ``with_retry`` attempt), the tool loop
hands its failing send's deadlines to the fallback ladders by provider
configuration (the adapter's label and the model the send targets), and a
ladder counts them toward its deadline cap (``with_retry``'s
``prior_deadlines``): the least handed off over the providers that spent a
deadline in the failed attempt.

The rig is the real send path: a real ``LLMRouter`` (its ``call_external``
ceiling and breaker), a real ``ProviderRegistry`` and the real dispatch, over
labelled stub adapters whose ``_generate`` hangs, raises or answers. The engine
runs its own tool loop and fallback under the turn's deadline and ``wait_for``,
as the turn route applies them. Time is virtual (this directory's
``conftest.py``): every module that reads ``time.monotonic()`` on the path reads
the loop's clock, so every elapsed figure is the schedule the code chose.

Each pin names the mutant it fails on; every one was run against it.
"""

import asyncio
from typing import Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation import llm_error_handler, turn_budget
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine import generation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.turn_budget import bind_turn_deadline
from faultmaven.exceptions import (
    PROVIDER_CIRCUIT_OPEN,
    TURN_BUDGET_EXHAUSTED,
    LLMErrorCategory,
    LLMException,
    declared_llm_category,
)
from faultmaven.infrastructure import base_client
from faultmaven.infrastructure.llm import router as router_module
from faultmaven.infrastructure.llm.dispatch import route_model_call
from faultmaven.infrastructure.llm.providers import ReasoningIntent, StopReason
from faultmaven.infrastructure.llm.providers import base as providers_base
from faultmaven.infrastructure.llm.providers import registry as registry_module
from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    ProviderConfig,
    ToolCall,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from faultmaven.models.interfaces import ToolResult
from tests.model_content import disabled_redaction, vetted
from tests.utils import sockets_refused
from tests.wallclock import virtual_now, virtual_time_module

pytestmark = [pytest.mark.integration, pytest.mark.llm]

RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
#: Long enough that only a ceiling ends it.
HANG = 10_000.0


@pytest.fixture(autouse=True)
def _no_network():
    with sockets_refused():
        yield


@pytest.fixture(autouse=True)
def _virtual_clocks(monkeypatch):
    """Every module that reads ``time.monotonic()`` on the send path reads the
    loop's clock: the deadline, the ladder's cost estimate, the router's and
    the call's ceilings, the breaker, provider health and the engine."""
    for module in (
        turn_budget,
        llm_error_handler,
        router_module,
        base_client,
        providers_base,
        registry_module,
        generation,
    ):
        monkeypatch.setattr(module, "time", virtual_time_module())


@pytest.fixture(autouse=True)
def _no_metering():
    with patch.object(registry_module, "record_provider_call", lambda *a, **k: None):
        yield


# --- The stub adapters -------------------------------------------------------

#: What a send does: hang until a ceiling cuts it, or raise, or answer.
HANGS = "hangs"


def answers_503() -> LLMException:
    return LLMException("upstream 503 over capacity", status_code=503)


def answers_504() -> LLMException:
    """What every adapter makes of its client's own timeout."""
    return LLMException("upstream request timed out", status_code=504)


def answers_overflow_504() -> LLMException:
    """A gateway's 504 whose body says the context is exceeded: a verdict on
    the request, deadline-shaped by its status."""
    return LLMException("upstream timed out: context length exceeded", status_code=504)


def answers_429() -> LLMException:
    return LLMException("rate limited", status_code=429)


def starved() -> LLMException:
    """Gemini's own report of a cut at the cap (#2802's second form)."""
    return LLMException(
        "Response truncated due to token limit (finishReason=MAX_TOKENS).",
        retryable=True,
        category=LLMErrorCategory.OUTPUT_TRUNCATION,
    )


class _Schema(BaseModel):
    agent_response: str = "ok"


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_file",
        "description": "Search evidence",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def calls_search_file() -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="stub",
        model="stub-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=[
            ToolCall(
                id="call_search",
                type="function",
                function={"name": "search_file", "arguments": '{"query": "disk"}'},
            )
        ],
        stop_reason=StopReason.TOOL_CALLS,
    )


class _Adapter:
    """A provider whose ``_generate`` follows a script: one action per send of
    each kind (``tool_loop``: the loop's sends, which carry ``messages``;
    ``single``: every other send), then ``HANGS``. Every send is logged.
    Its configured default model is what a send that names no model targets,
    and with the label it makes the key the dispatch records a failure by."""

    def __init__(self, tool_loop=(), single=(), model=None):
        self.config = ProviderConfig(
            name=self.LABEL, default_model=model or f"{self.LABEL}-default"
        )
        self.script = {"tool_loop": list(tool_loop), "single": list(single)}
        self.sends: List[tuple] = []

    @property
    def provider_name(self) -> str:
        return self.LABEL

    def supports_tool_calling(self, model=None) -> bool:
        return True

    def get_structured_output_capability(self, model=None):
        return StructuredOutputCapability.STRICT

    def get_structured_output_strategy(self, schema, model=None):
        return create_strategy_for_capability(StructuredOutputCapability.STRICT, schema)

    async def _generate(self, **kwargs):
        kind = "tool_loop" if kwargs.get("messages") else "single"
        self.sends.append((kind, virtual_now()))
        script = self.script[kind]
        action = script.pop(0) if script else HANGS
        if action == HANGS:
            await asyncio.sleep(HANG)
            raise AssertionError("a hung send outlived every ceiling")
        if isinstance(action, Callable) and not isinstance(action, LLMResponse):
            raise action()
        return action


def adapter(label: str, model: Optional[str] = None, **script) -> _Adapter:
    """A stub labelled ``label`` (its ``LABEL``), configured for ``model``."""
    return type(f"Stub_{label}", (_Adapter,), {"LABEL": label})(model=model, **script)


def times_out_after(stub: _Adapter, seconds: float) -> None:
    """``stub`` answers every send with its client's own timeout, a 504, after
    ``seconds``: how a dedicated DA provider (no router ceiling) hangs."""

    async def _generate(**kwargs):
        stub.sends.append(("tool_loop", virtual_now()))
        await asyncio.sleep(seconds)
        raise answers_504()

    stub._generate = _generate


# --- The rig -----------------------------------------------------------------


class _Sanitizer:
    """The router's outbound pass, run inline: the real one offloads to a
    thread, which a virtual clock does not wait for. Under SANITIZE_PII=false
    it is never called."""

    async def asanitize(self, value):
        return value


def build_router(monkeypatch, chain: List[_Adapter], request_timeout: float):
    registry = ProviderRegistry(settings=None)
    registry._providers = {a.LABEL: a for a in chain}
    registry._fallback_chain = [a.LABEL for a in chain]
    registry._provider_states = {
        a.LABEL: ProviderState(name=a.LABEL, health=ProviderHealth.UNKNOWN)
        for a in chain
    }
    registry._initialized = True
    monkeypatch.setattr(router_module, "get_registry", lambda: registry)
    router = LLMRouter()
    router.sanitizer = _Sanitizer()
    router.request_timeout = request_timeout
    monkeypatch.setattr(router.settings.llm, "request_timeout", int(request_timeout))
    monkeypatch.setattr(router.settings.llm, "provider_timeout_overrides", {})
    return router


class _Tools:
    """The investigation tool registry: ``search_file``'s answer, after its
    own model send through the router, cut by a ``wait_for``."""

    def __init__(self, router: Optional[LLMRouter] = None):
        self.router = router
        self.answerer_cut = False

    async def execute_tool(self, name, args, context):
        if self.router is not None:
            try:
                await asyncio.wait_for(
                    route_model_call(self.router, prompt=vetted("summarise disk")),
                    timeout=5.0,
                )
            except TimeoutError:
                self.answerer_cut = True
        return ToolResult(success=True, data="no match")


class Outcome:
    def __init__(self, elapsed, code):
        self.elapsed = elapsed
        self.code = code

    def __repr__(self):  # pragma: no cover - failure output only
        return f"<Outcome {self.code} after {self.elapsed:.2f}s>"


async def run_turn(
    router,
    *,
    agent_timeout: float,
    da: Optional[_Adapter] = None,
    da_model: Optional[str] = None,
    tools: Optional[_Tools] = None,
    with_tools: bool = True,
    **intent,
) -> Outcome:
    """One turn's structured call, under the deadline and the ``wait_for`` the
    turn route applies (``AGENT_REQUEST_TIMEOUT`` = ``agent_timeout``)."""
    engine = MilestoneEngine(
        llm_provider=router,
        repository=MagicMock(),
        investigation_tools=tools or _Tools(),
        da_provider=da,
        da_model=da_model,
    )
    engine.deps.llm_error_handler = LLMErrorHandler(RetryConfig())
    kwargs = (
        dict(investigation_tools=[SEARCH_TOOL], tool_context=MagicMock())
        if with_tools
        else {}
    )
    started = virtual_now()
    try:
        with bind_turn_deadline(agent_timeout):
            await asyncio.wait_for(
                engine.generator.generate_structured_output(
                    "why is node-3 NotReady?",
                    _Schema,
                    redaction_ctx=disabled_redaction(),
                    **kwargs,
                    **intent,
                ),
                timeout=agent_timeout,
            )
        code = "answered"
    except MilestoneEngineError as error:
        code = error.error_code
    return Outcome(virtual_now() - started, code)


def sends(stub: _Adapter, kind: Optional[str] = None) -> List[float]:
    return [at for k, at in stub.sends if kind is None or k == kind]


# --- The defect --------------------------------------------------------------


@pytest.mark.asyncio
class TestAHungProviderIsNotGrantedTheRetryItSpentInTheToolLoop:
    """At the shipped timeouts (T = 30 s, A = 120 s)."""

    async def test_the_turn_pays_the_tool_send_and_one_ladder_attempt(
        self, monkeypatch
    ):
        """Fails on ``bf72903bd`` (92 s: the ladder's retry, after its 2 s
        backoff). Mutant: no prior handed to the first fallback ladder."""
        hung = adapter("gemini")
        router = build_router(monkeypatch, [hung], request_timeout=30.0)

        outcome = await run_turn(router, agent_timeout=120.0)

        assert outcome.code == RETRY_EXHAUSTED
        assert outcome.elapsed == pytest.approx(60.0)
        assert sends(hung, "tool_loop") == [0.0]
        assert sends(hung, "single") == [pytest.approx(30.0)]

    async def test_the_refusal_moves_to_retry_exhausted_at_a_long_ceiling(
        self, monkeypatch
    ):
        """Declared behaviour change (T = 120 s, A = 240 s): the ladder's one
        attempt is the provider's last, so its refusal is the ordinary
        ``RETRY_EXHAUSTED``, where ``bf72903bd`` refused the retry it would
        have granted for lack of budget (``TURN_BUDGET_EXHAUSTED``). Both are
        503s. Mutant: no prior."""
        hung = adapter("gemini")
        router = build_router(monkeypatch, [hung], request_timeout=120.0)

        outcome = await run_turn(router, agent_timeout=240.0)

        assert outcome.code == RETRY_EXHAUSTED != TURN_BUDGET_EXHAUSTED
        # 120 s in the tool loop, then the ladder's attempt clamped to what is
        # left of the turn (240 - 120 - 1.0 step - 3.5 commit reserve).
        assert outcome.elapsed == pytest.approx(235.5)
        assert len(sends(hung, "single")) == 1
        from faultmaven.api.exception_handlers import _RETRYABLE_ENGINE_CODES

        assert {RETRY_EXHAUSTED, TURN_BUDGET_EXHAUSTED} <= _RETRYABLE_ENGINE_CODES

    @pytest.mark.parametrize(
        "request_timeout,agent_timeout,elapsed",
        [(30.0, 120.0, 60.0), (30.0, 240.0, 60.0), (120.0, 240.0, 235.5)],
        ids=["T30-A120", "T30-A240", "T120-A240"],
    )
    async def test_the_numbers_the_rules_text_states(
        self, monkeypatch, request_timeout, agent_timeout, elapsed
    ):
        """``.claude/rules/llm-providers.md`` and the error-handling design
        doc state these. On ``bf72903bd``: 92, 92 and 235.5 s."""
        hung = adapter("gemini")
        router = build_router(monkeypatch, [hung], request_timeout=request_timeout)

        outcome = await run_turn(router, agent_timeout=agent_timeout)

        assert outcome.code == RETRY_EXHAUSTED
        assert outcome.elapsed == pytest.approx(elapsed)

    async def test_the_breaker_opens_in_the_second_turn(self, monkeypatch):
        """Three turns against one hung provider and one router: the first
        pays the tool send and one ladder attempt (two breaker failures of
        three), the second's tool send opens the breaker and its ladder is
        refused at once, the third is refused at once. 60 + 30 + 0 s where
        ``bf72903bd`` paid 92 + 0 + 0: the gain is per-turn latency, not total
        cost. Mutant: no prior."""
        hung = adapter("gemini")
        router = build_router(monkeypatch, [hung], request_timeout=30.0)

        turns = [await run_turn(router, agent_timeout=120.0) for _ in range(3)]

        assert [t.code for t in turns] == [
            RETRY_EXHAUSTED,
            PROVIDER_CIRCUIT_OPEN,
            PROVIDER_CIRCUIT_OPEN,
        ]
        assert [t.elapsed for t in turns] == [
            pytest.approx(60.0),
            pytest.approx(30.0),
            pytest.approx(0.0),
        ]


# --- What never counts -------------------------------------------------------


@pytest.mark.asyncio
class TestWhatTheHandOffNeverCounts:
    """Each turn would end at 60 s if the hand-off counted what it must not;
    the hang in the fallback keeps its retry: 30 + 30 + 2 + 30 = 92 s, or 62 s
    where the tool loop's failure cost nothing."""

    async def test_a_request_verdict_504_is_not_a_spent_deadline(self, monkeypatch):
        """An overflow 504 in the tool loop. Mutant: the verdict as bare
        ``is_deadline_shaped`` (no caller-remedy exclusion)."""
        assert declared_llm_category(answers_overflow_504()) in (
            LLMErrorCategory.CONTEXT_OVERFLOW,
        )
        stub = adapter("gemini", tool_loop=[answers_overflow_504])
        router = build_router(monkeypatch, [stub], request_timeout=30.0)

        outcome = await run_turn(router, agent_timeout=120.0)

        assert outcome.code == RETRY_EXHAUSTED
        assert sends(stub, "single") == [pytest.approx(0.0), pytest.approx(32.0)]
        assert outcome.elapsed == pytest.approx(62.0)

    async def test_a_status_failure_in_the_tool_loop_is_not_a_deadline(
        self, monkeypatch
    ):
        """A 503 in the tool loop, then a hang. Mutant: every recorded failure
        counted as a deadline."""
        stub = adapter("gemini", tool_loop=[answers_503])
        router = build_router(monkeypatch, [stub], request_timeout=30.0)

        outcome = await run_turn(router, agent_timeout=120.0)

        assert outcome.code == RETRY_EXHAUSTED
        assert outcome.elapsed == pytest.approx(62.0)
        assert len(sends(stub, "single")) == 2

    async def test_a_dedicated_da_providers_hang_leaves_the_chat_provider_its_retry(
        self, monkeypatch
    ):
        """A dedicated ``DA_PROVIDER`` (sent to directly) hangs in the tool
        loop until its client gives up (504); the chat provider then hangs in
        the fallback and keeps its full deadline ladder. Mutant: the hand-off
        keyed by anything but the provider that failed (one key for every
        send)."""
        da = adapter("fireworks")
        times_out_after(da, 30.0)
        chat = adapter("gemini")
        router = build_router(monkeypatch, [chat], request_timeout=30.0)

        outcome = await run_turn(router, agent_timeout=120.0, da=da)

        assert outcome.code == RETRY_EXHAUSTED
        assert outcome.elapsed == pytest.approx(92.0)
        assert sends(chat, "single") == [pytest.approx(30.0), pytest.approx(62.0)]

    async def test_the_same_vendor_on_another_model_is_another_provider(
        self, monkeypatch
    ):
        """``CHAT_PROVIDER=gemini`` on one model, ``DA_PROVIDER=gemini`` on
        another: one adapter class, one label. The DA provider hangs in the
        tool loop (its send names ``DA_MODEL``); the chat provider's fallback
        ladder (its sends name no model, so its configured default) keeps its
        deadline retry. Mutant: the key the dispatch records by is the label
        alone."""
        da = adapter("gemini", model="gemini-pro")
        times_out_after(da, 30.0)
        chat = adapter("gemini", model="gemini-flash")
        router = build_router(monkeypatch, [chat], request_timeout=30.0)

        outcome = await run_turn(
            router, agent_timeout=120.0, da=da, da_model="gemini-pro"
        )

        assert sends(da, "tool_loop") == [0.0]
        assert outcome.code == RETRY_EXHAUSTED
        assert outcome.elapsed == pytest.approx(92.0)
        assert sends(chat, "single") == [pytest.approx(30.0), pytest.approx(62.0)]

    async def test_a_tools_answerer_cut_between_sends_is_not_counted(self, monkeypatch):
        """Iteration 0 asks for ``search_file``, whose answerer's own model
        send is cut by a 5 s ``wait_for``; iteration 1 fails with a 503; the
        fallback hang keeps its retry. Mutant: the collector bound around the
        loop's iterations (the sends and the tools between them) instead of
        each send."""
        stub = adapter("gemini", tool_loop=[calls_search_file(), answers_503])
        router = build_router(monkeypatch, [stub], request_timeout=30.0)
        tools = _Tools(router)

        outcome = await run_turn(router, agent_timeout=120.0, tools=tools)

        assert tools.answerer_cut
        assert outcome.code == RETRY_EXHAUSTED
        # 5 s answerer, then the ladder's two 30 s attempts and a 2 s backoff.
        assert outcome.elapsed == pytest.approx(67.0)
        assert len(sends(stub, "single")) == 3  # the answerer's, then two


# --- The count ---------------------------------------------------------------


async def _chained_ladder(prior: Dict[str, int], router):
    """One ladder over a router chain, handed ``prior`` (passed only when
    non-empty, so the no-prior pin runs on ``main`` as written)."""

    async def attempt():
        return await route_model_call(router, prompt=vetted("p"))

    handed = {"prior_deadlines": prior} if prior else {}
    with bind_turn_deadline(120.0):
        return await LLMErrorHandler(RetryConfig()).with_retry(
            operation=attempt, **handed
        )


@pytest.mark.asyncio
class TestTheCountIsTheLeastSpentOfTheFailingProviders:
    async def test_a_chain_whose_second_provider_is_fresh_keeps_the_retry(
        self, monkeypatch
    ):
        """Prior ``{a: 1}``; each attempt ``a`` answers 504 and ``b`` hangs:
        ``b`` has spent nothing, so the retry is granted (never-zero for
        ``b``). The prior names the dispatch's keys, ``label/model``; the
        control hands both providers a spent deadline and is refused the
        retry, so the keys are seen to match. Mutant: ``min`` → ``max``."""
        a = adapter("a", single=[answers_504, answers_504])
        b = adapter("b")
        router = build_router(monkeypatch, [a, b], request_timeout=30.0)
        monkeypatch.setattr(
            registry_module.ProviderRegistry,
            "_get_routing_order",
            lambda self: list(self._fallback_chain),
        )

        _, error = await _chained_ladder({"a/a-default": 1}, router)

        assert error.error_code == RETRY_EXHAUSTED
        assert len(sends(a)) == len(sends(b)) == 2

    async def test_control_both_providers_spent_so_no_retry(self, monkeypatch):
        a = adapter("a", single=[answers_504])
        b = adapter("b")
        router = build_router(monkeypatch, [a, b], request_timeout=30.0)
        monkeypatch.setattr(
            registry_module.ProviderRegistry,
            "_get_routing_order",
            lambda self: list(self._fallback_chain),
        )

        _, error = await _chained_ladder({"a/a-default": 1, "b/b-default": 1}, router)

        assert error.error_code == RETRY_EXHAUSTED
        assert len(sends(a)) == len(sends(b)) == 1

    async def test_with_nothing_handed_off_a_chain_ladder_is_as_on_main(
        self, monkeypatch
    ):
        """No tools, no prior: ``a`` answers 504 and ``b`` answers 503, twice,
        then ``a`` 504 and ``b`` 429. The ladder makes the four attempts it
        makes on ``main``. Mutant: the round-102 body's formula,
        ``max(ladder, min(spent) - 1)`` with ``spent`` accumulated."""
        a = adapter("a", single=[answers_504] * 4)
        b = adapter("b", single=[answers_503, answers_503, answers_429, answers_429])
        router = build_router(monkeypatch, [a, b], request_timeout=30.0)
        router.circuit_breaker = None
        monkeypatch.setattr(
            registry_module.ProviderRegistry,
            "_get_routing_order",
            lambda self: list(self._fallback_chain),
        )

        _, error = await _chained_ladder({}, router)

        assert error.error_code == RETRY_EXHAUSTED
        assert len(sends(a)) == 4


# --- #2802 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_starved_reissue_ladder_honours_the_tool_loops_prior(monkeypatch):
    """A tool-loop hang; the fallback's INFERENCE attempt starves (#2802); its
    re-issue without the intent hangs. The re-issue's ladder counts the tool
    loop's deadline: one attempt, 60 s, where without the prior it retries
    (92 s). Mutant: no prior on the #2802 re-issue's ``with_retry``."""
    stub = adapter("gemini", single=[starved])
    router = build_router(monkeypatch, [stub], request_timeout=30.0)

    outcome = await run_turn(
        router,
        agent_timeout=120.0,
        reasoning_intent=ReasoningIntent.INFERENCE,
        min_output_tokens=generation.TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    )

    assert outcome.code == RETRY_EXHAUSTED
    assert sends(stub, "single") == [pytest.approx(30.0), pytest.approx(30.0)]
    assert outcome.elapsed == pytest.approx(60.0)
