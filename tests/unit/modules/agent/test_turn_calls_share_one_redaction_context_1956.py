"""#1956: every model call made on a turn's behalf sends through the turn's ONE
case redaction context.

#1952 put every prompt the milestone engine itself sends through the case-scoped
``CaseRedactionContext``. The other calls a turn makes did not get it and leaned
on the router's own ``SANITIZE_PII`` pass: ``case_evidence_search`` and ``kb_qa``
synthesis, ``deep_analysis``, the intent classifier, the out-of-band triage and
answer, and the case title. The router's pass produces the same text, so this
was not a raw leak with the shipped router; what it cost is the case REGISTRY
(a value redacted only by the router never enters it, so its placeholder in an
answer cannot be reversed in the reply the user reads) and dependence on
whichever router ``LLM_ROUTER_CLASS`` configures.

Each test drives one send site with a recording fake provider (no live LLM
call) and a redaction double whose output is recognisable, and asserts what the
provider RECEIVED. The service tests drive the real ``InvestigationService``
over a real ``CaseRedactionContext`` (a fake sanitizer and an in-memory
registry store) and assert that one context serves the whole turn and that the
reply the user is shown — and the assistant row stored — is reversed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import fakeredis.aioredis
import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TurnCapService,
)
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.modules.agent.domain.services.investigation_service import (
    service as service_module,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.agent.domain.services.out_of_band import TRIAGE_MAX_TOKENS
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.case_evidence_qa import AnswerFromCaseEvidence
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    CaseEvidenceQAAdapter,
    KBToolAdapter,
)
from faultmaven.modules.case.api import title_generation
from faultmaven.modules.case.contracts import CaseState, InquiryData, UploadedFile
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit

RAW = "alice@example.com"
PSEUDONYM = "<EMAIL_ADDRESS_1956c0de1956c0de>"


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
    )


class _MarkingRedaction:
    """Stands in for ``CaseRedactionContext``: rewrites the one entity to a
    recognisable pseudonym, records what it was handed, reverses it back."""

    def __init__(self, raw: str = RAW) -> None:
        self.raw = raw
        self.seen: list[str] = []
        self.save = AsyncMock()

    async def asanitize(self, text: str) -> str:
        self.seen.append(text)
        return text.replace(self.raw, PSEUDONYM)

    async def asanitize_messages(self, messages):
        return [{**m, "content": await self.asanitize(m["content"])} for m in messages]

    def reverse(self, text: str) -> str:
        return text.replace(PSEUDONYM, RAW)


class _RecordingRouter:
    """A provider double that records every call and answers ``content``."""

    def __init__(self, content: str = "ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self.content = content

    async def route(self, **kwargs):
        self.calls.append(kwargs)
        return _response(self.content)

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return _response(self.content)


def _sent(call: dict[str, Any]) -> str:
    if "messages" in call:
        return "\n".join(m["content"] for m in call["messages"])
    return call["prompt"]


def _assert_redacted(router: _RecordingRouter) -> None:
    assert router.calls, "the provider was never called"
    for call in router.calls:
        text = _sent(call)
        assert RAW not in text, text
        assert PSEUDONYM in text, text


def _tool_context(redaction, case=None) -> ToolContext:
    return ToolContext(
        session_id="case_1956",
        case_id="case_1956",
        enterprise_id="ent",
        user_id="user",
        kb_scope_filter={"scope": "global"},
        in_memory_case=case,
        redaction_ctx=redaction,
    )


class _Store:
    """A vector store whose one chunk carries the raw value."""

    async def search(self, **_):
        return [
            {
                "content": f"ERROR login failed for {RAW} from 10.0.0.7",
                "metadata": {"source": "app.log", "title": "app.log"},
                "score": 0.95,
            }
        ]


# ---------------------------------------------------------------------------
# The ToolContext carries the turn's context to the tools.
# ---------------------------------------------------------------------------


class TestTheToolContextCarriesTheTurnsRedaction:
    async def test_build_tool_context_sets_it(self):
        from faultmaven.core.investigation.milestone_engine.generation import (
            StructuredOutputGenerator,
        )

        generator = StructuredOutputGenerator(
            deps=SimpleNamespace(
                team_service=None, share_repository=None, repository=None
            ),
            vectorizer=None,
        )
        redaction = _MarkingRedaction()
        case = SimpleNamespace(case_id="case_1956", enterprise_id="ent", progress=None)
        with patch(
            "faultmaven.core.investigation.milestone_engine.generation."
            "case_retrieval_scope",
            new=AsyncMock(return_value={"scope": "x"}),
        ):
            ctx = await generator.build_tool_context(
                case, user_id="u", redaction_ctx=redaction
            )
        assert ctx.redaction_ctx is redaction

    def test_with_execution_id_keeps_it(self):
        redaction = _MarkingRedaction()
        assert (
            _tool_context(redaction).with_execution_id("exec_1").redaction_ctx
            is redaction
        )


# ---------------------------------------------------------------------------
# Tools that call a model themselves.
# ---------------------------------------------------------------------------


class TestCaseEvidenceSearchSendsRedactedChunks:
    """The registry harm: case evidence is stored raw by design, so these
    chunks carry raw values into the synthesis prompt."""

    async def test_the_synthesis_prompt_is_case_redacted(self):
        router = _RecordingRouter(content=f"The failures are for {PSEUDONYM}.")
        redaction = _MarkingRedaction()
        adapter = CaseEvidenceQAAdapter(
            AnswerFromCaseEvidence(vector_store=_Store(), llm_router=router)
        )

        result = await adapter.execute_with_context(
            {"question": f"who failed to log in? {RAW}?"}, _tool_context(redaction)
        )

        assert result.success, result.error
        _assert_redacted(router)
        # The answer goes back into the loop with the placeholder, which the
        # engine reverses in the final reply from the same registry.
        assert PSEUDONYM in result.data


class TestKBQASendsRedactedSynthesis:
    async def test_the_synthesis_prompt_is_case_redacted(self):
        router = _RecordingRouter(content="Rotate the credential.")
        redaction = _MarkingRedaction()
        adapter = KBToolAdapter(AnswerFromKB(vector_store=_Store(), llm_router=router))

        result = await adapter.execute_with_context(
            {"question": f"what to do when {RAW} cannot log in?"},
            _tool_context(redaction),
        )

        assert result.success, result.error
        _assert_redacted(router)


class TestDeepAnalysisSendsRedactedSections:
    async def test_the_analysis_prompt_is_case_redacted(self):
        router = _RecordingRouter(content=f"{PSEUDONYM} failed twice.")
        storage = SimpleNamespace(
            retrieve_file=AsyncMock(
                return_value=f"line one\nauth failure user={RAW}\nline three".encode()
            )
        )
        tool = DeepAnalysisTool(LocalTier2Service(router, storage))
        case = Case(
            case_id="case_aabb19561956",
            title="t",
            description="d",
            user_id="user",
            enterprise_id="ent",
            state=CaseState.INVESTIGATING,
            inquiry=InquiryData(
                problem_statement_confirmed=True, proposed_problem_statement="t"
            ),
            evidence=[],
            uploaded_files=[
                UploadedFile(
                    file_id="file_fa0e00001956",
                    filename="auth.log",
                    size_bytes=64,
                    content_type="text/plain",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    uploaded_by="user",
                    data_type="logs",
                    storage_ref="ent/case/auth.log",
                )
            ],
        )

        result = await tool.execute_with_context(
            {"evidence_id": "file_fa0e00001956", "query": "auth failure"},
            _tool_context(_MarkingRedaction(), case),
        )

        assert result.success, result.error
        _assert_redacted(router)


# ---------------------------------------------------------------------------
# The intent classifier.
# ---------------------------------------------------------------------------


class TestTheIntentClassifierSendsTheRedactedMessage:
    async def test_the_classifier_prompt_is_case_redacted(self):
        router = _RecordingRouter(content="none")
        choices = [
            {
                "label": "Yes, close the case",
                "intent": {"type": "confirmation", "value": True},
            }
        ]

        await IntentResolver(router).resolve(
            user_message=f"tell {RAW} first",
            last_suggestions=choices,
            redaction_ctx=_MarkingRedaction(),
        )

        _assert_redacted(router)


# ---------------------------------------------------------------------------
# The case title: redacted on the way out, never reversed on the way back.
# ---------------------------------------------------------------------------


class TestTheTitle:
    async def test_the_title_prompt_is_case_redacted(self):
        router = _RecordingRouter(content="Login Failures On The Auth Service")
        signals = f"login failures for {RAW} on the auth service " * 40

        title, source = await title_generation._generate_title_with_llm(
            "context",
            SimpleNamespace(title="Case-261012-1", description=""),
            8,
            None,
            signals,
            router,
            routing_signal=signals,
            redaction_ctx=_MarkingRedaction(),
            sanitizer=_FakeSanitizer(),
        )

        assert source == "llm"
        _assert_redacted(router)

    @pytest.mark.parametrize(
        "title",
        [
            f"Login Failures For {PSEUDONYM} Account",
            f"{PSEUDONYM} Login Failures",
            "Login Failures For <IP_ADDRESS_0a1b2c3d4e5f> Hosts",
        ],
    )
    def test_a_placeholder_anywhere_is_not_a_title(self, title):
        assert title_generation.is_title_valid(title) is False

    def test_the_control_without_a_placeholder_is_a_title(self):
        assert title_generation.is_title_valid("Login Failures For Admin Account")

    async def test_a_model_title_with_a_placeholder_is_never_reversed(self):
        """The ruling: a title is shown to every reader of the case, so a real
        value restored into it would be a disclosure. It falls back instead."""
        router = _RecordingRouter(content=f"Login Failures For {PSEUDONYM} Account")
        signals = "login failures on the auth service for the admin account " * 20

        title, source = await title_generation._generate_title_with_llm(
            "context",
            SimpleNamespace(title="Case-261012-1", description=""),
            8,
            None,
            signals,
            router,
            routing_signal=signals,
            redaction_ctx=_MarkingRedaction(),
            sanitizer=_FakeSanitizer(),
        )

        assert source != "llm"
        assert PSEUDONYM not in title and RAW not in title

    async def test_the_title_loads_the_cases_context_through_the_loader(self):
        # A host name: the title's own regex scrub (``_sanitize_title_content``)
        # does not touch it, so only the case pass can redact it.
        host = "auth-db-01.corp.internal"
        loaded = _MarkingRedaction(raw=host)
        loader = AsyncMock(return_value=loaded)
        router = _RecordingRouter(content="Login Failures On The Auth Service")
        case = SimpleNamespace(
            case_id="case_1956",
            title="Case-261012-1",
            description="",
            inquiry=SimpleNamespace(
                proposed_problem_statement=f"Login failures on {host}",
                problem_statement_confirmed=True,
            ),
            evidence=[],
            uploaded_files=[],
        )
        case_service = SimpleNamespace(
            get_case_conversation_context=AsyncMock(
                return_value=f"User: login failures on {host} " * 40
            ),
            update_case=AsyncMock(return_value=True),
            get_case=AsyncMock(
                side_effect=lambda *_a, **_k: SimpleNamespace(title=router.title)
            ),
        )
        router.title = "Login Failures On The Auth Service"
        # A sanitizer the title's disclosure gate can run (#1967); the loader
        # is a double, so this one only has to reach it unchanged.
        sanitizer, redis_client = _FakeSanitizer(), object()

        with patch.object(title_generation, "load_case_redaction", new=loader):
            await title_generation._generate_and_persist_title(
                case=case,
                case_id="case_1956",
                user_id="user",
                case_service=case_service,
                llm_provider=router,
                max_words=8,
                hint=None,
                correlation_id="c",
                sanitizer=sanitizer,
                redis_client=redis_client,
            )

        loader.assert_awaited_once_with("case_1956", sanitizer, redis_client)
        assert router.calls
        for call in router.calls:
            assert host not in _sent(call) and PSEUDONYM in _sent(call)
        loaded.save.assert_awaited()


# ---------------------------------------------------------------------------
# The service: one context for the whole turn, and the reply reversed.
# ---------------------------------------------------------------------------


class _FakeSanitizer:
    """``sanitize_text_with_registry`` over a few literal values, each with a
    fixed placeholder (as the keyed pseudonym is a function of the value)."""

    VALUES = {RAW: ("EMAIL_ADDRESS", PSEUDONYM)}

    def sanitize_text_with_registry(self, text, registry):
        for value, (kind, placeholder) in self.VALUES.items():
            if value in text:
                registry.setdefault(kind, {}).setdefault(value, placeholder)
                text = text.replace(value, placeholder)
        return text

    def apply_regex_redaction(self, text, registry):
        """The in-process half finds none of these values: they stand for
        analyzer entities, so the case title's source scrub (#1967) leaves
        them to the case pass these tests are about."""
        return text


def _context() -> CaseRedactionContext:
    return CaseRedactionContext(
        case_id="case_1956",
        sanitizer=_FakeSanitizer(),
        redis_client=fakeredis.aioredis.FakeRedis(),
        enabled=True,
    )


class _Orgs:
    async def get_organization(self, organization_id):
        return SimpleNamespace(organization_id=organization_id, daily_turn_cap=None)


QUERY = (
    "Forget the server for a second. Can you write a haiku about the cat that "
    f"belongs to {RAW}, and tell me the capital of Australia?"
)


def _router(answer: str, verdict: str = "2"):
    calls: list[dict[str, Any]] = []

    async def route(**kwargs):
        calls.append(kwargs)
        if kwargs.get("max_tokens") == TRIAGE_MAX_TOKENS:
            return SimpleNamespace(content=verdict)
        return SimpleNamespace(content=answer)

    router = MagicMock()
    router.route = AsyncMock(side_effect=route)
    router.calls = calls
    return router


@pytest.fixture(autouse=True)
def bound_tenant():
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
    from faultmaven.config.tenant_context import set_current_enterprise_id

    set_current_enterprise_id("ent-1956")
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
def case(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.title = "Nightly OOM kills of postgres"
    sample_case.inquiry.proposed_problem_statement = "Nightly OOM kills"
    sample_case.inquiry.problem_statement_confirmed = True
    sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    sample_case.state = CaseState.INVESTIGATING
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
        {"turn_number": 1, "role": "user", "content": "postgres OOM-killed nightly"},
        {"turn_number": 1, "role": "assistant", "content": "Could you share dmesg?"},
    ]
    return sample_case


def _engine(router, answer: str = "engine answer"):
    double = create_autospec(MilestoneEngine, instance=True)

    async def spy(*, case, **_kwargs):
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": answer,
            "metadata": {"milestones_completed": [], "progress_made": False},
        }

    double.process_turn = AsyncMock(side_effect=spy)
    double.deps = SimpleNamespace(
        llm_provider=router, sanitizer=None, redis_client=None
    )
    return double


def _service(engine, repo):
    cap = TurnCapService(
        CapPolicyResolver(_Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True),
        InMemoryTurnLedger(),
    )
    return InvestigationService(
        milestone_engine=engine, case_repository=repo, turn_cap=cap
    )


async def _turn(service, repo, case, ctx, query=QUERY):
    await repo.save(case)
    loader = AsyncMock(return_value=ctx)
    with patch.object(service_module, "load_case_redaction", new=loader):
        resp = await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(query=query),
        )
    return resp, repo._storage[case.case_id], loader


def _assistant_rows(saved, turn):
    return [
        m
        for m in saved.messages
        if m["role"] == "assistant" and m["turn_number"] == turn
    ]


class TestOneContextPerTurn:
    async def test_the_out_of_band_turn_sends_redacted_and_stores_the_reply_reversed(
        self, recording_case_repository, case
    ):
        ctx = _context()
        router = _router(answer=f"A haiku for the cat of {PSEUDONYM}. Canberra.")
        service = _service(_engine(router), recording_case_repository)

        resp, saved, loader = await _turn(service, recording_case_repository, case, ctx)

        loader.assert_awaited_once()
        triage = [c for c in router.calls if c["max_tokens"] == TRIAGE_MAX_TOKENS]
        answers = [c for c in router.calls if c["max_tokens"] != TRIAGE_MAX_TOKENS]
        assert triage and answers
        for call in triage + answers:
            text = _sent(call)
            assert RAW not in text and PSEUDONYM in text, text
        # The reply the user reads and the row stored are reversed from the
        # turn's registry, which the answer prompt's redaction filled.
        assert RAW in resp.agent_response and PSEUDONYM not in resp.agent_response
        [row] = _assistant_rows(saved, 2)
        assert row["content"] == f"A haiku for the cat of {RAW}. Canberra."
        assert await ctx.redis_client.hgetall(
            "redaction:registry:case_1956"
        ), "the registry the turn grew was not saved"

    async def test_the_engine_receives_the_same_context_the_triage_used(
        self, recording_case_repository, case
    ):
        ctx = _context()
        router = _router(answer="unused", verdict="1")
        engine = _engine(router)
        service = _service(engine, recording_case_repository)

        await _turn(service, recording_case_repository, case, ctx)

        assert engine.process_turn.call_args.kwargs["redaction_ctx"] is ctx
        [triage] = router.calls
        assert RAW not in _sent(triage) and PSEUDONYM in _sent(triage)

    async def test_a_terminal_answer_is_reversed_too(
        self, recording_case_repository, case
    ):
        """The terminal Q&A path never handed its context back, so a
        placeholder in its answer reached the user. The service reverses every
        route's reply with the turn's one context."""
        ctx = _context()
        ctx.sanitize(f"seen earlier: {RAW}")  # the registry knows the value
        case = case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
                "closed_at": datetime.now(timezone.utc),
            }
        )
        engine = _engine(_router(answer="unused"), answer=f"It was {PSEUDONYM}.")
        service = _service(engine, recording_case_repository)

        resp, saved, _ = await _turn(
            service, recording_case_repository, case, ctx, query="who was it?"
        )

        assert engine.process_turn.call_args.kwargs["redaction_ctx"] is ctx
        assert resp.agent_response == f"It was {RAW}."
        [row] = _assistant_rows(saved, 2)
        assert row["content"] == f"It was {RAW}."


# ---------------------------------------------------------------------------
# Concurrency: contexts on one case key never drop each other's entries.
# ---------------------------------------------------------------------------

IP_A, PH_A = "10.20.30.40", "<IP_ADDRESS_a0a0a0a0a0a0a0a0>"
IP_B, PH_B = "10.99.88.77", "<IP_ADDRESS_b0b0b0b0b0b0b0b0>"
IP_D, PH_D = "10.55.55.55", "<IP_ADDRESS_d0d0d0d0d0d0d0d0>"
# A host name: the title's own regex scrub leaves it, so the title's case pass
# is what registers it.
HOST, PH_HOST = "db-primary-01.corp", "<HOSTNAME_e0e0e0e0e0e0e0e0>"


class _IpSanitizer(_FakeSanitizer):
    VALUES = {
        IP_A: ("IP_ADDRESS", PH_A),
        IP_B: ("IP_ADDRESS", PH_B),
        IP_D: ("IP_ADDRESS", PH_D),
        HOST: ("HOSTNAME", PH_HOST),
        RAW: ("EMAIL_ADDRESS", PSEUDONYM),
    }


@pytest.fixture
def redaction_on(monkeypatch):
    """``load_case_redaction`` is enabled by ``should_redact``: a sanitizer and
    ``SANITIZE_PII``. Pinned on, so the test runs the same under both CI
    profiles."""
    from tests.utils import reset_settings_singleton

    monkeypatch.setenv("SANITIZE_PII", "true")
    reset_settings_singleton()
    yield
    reset_settings_singleton()


class _CopyRepo:
    """Each turn reads its own copy of the case, as a database read would."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get(self, case_id):
        stored = self.store.get(case_id)
        return stored.model_copy(deep=True) if stored is not None else None

    async def save(self, case, *, reports=(), receipt=None):
        self.store[case.case_id] = case.model_copy(deep=True)
        return case


def _locking_engine(sanitizer, redis, script):
    """The REAL ``MilestoneEngine.process_turn`` (its per-case lock, its spend
    tracker), with ``_process_turn_impl`` standing in for prompt build and the
    model: it sends the script's prompt through the turn's context, holds,
    saves the context as ``turn_completion`` does, and answers."""
    import asyncio
    from collections import defaultdict

    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine._case_locks = defaultdict(asyncio.Lock)
    engine.deps = SimpleNamespace(
        llm_provider=None, sanitizer=sanitizer, redis_client=redis
    )
    engine.sent = []

    async def impl(
        case,
        user_message,
        attachments=None,
        intent_type=None,
        intent_data=None,
        *,
        plan,
        user_id=None,
        typed=False,
        redaction_ctx,
    ):
        prompt, reply, hold = script[user_message]
        engine.sent.append(await redaction_ctx.asanitize(prompt))
        await asyncio.sleep(hold)
        await redaction_ctx.save()
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": reply,
            "metadata": {"milestones_completed": [], "progress_made": False},
            "redaction_ctx": redaction_ctx,
        }

    engine._process_turn_impl = impl
    return engine


class TestConcurrentTurnsKeepTheRegistry:
    async def test_two_concurrent_turns_then_a_third_reverses_both(
        self, redaction_on, case
    ):
        """The review's probe: turns A and B load the registry before either
        saves (the service loads it ahead of the engine's per-case lock), and
        each registers a new value. Turn C's prompt carries both only as
        placeholders (LLM-authored state) and its reply echoes them; before
        the merge, the later save erased the other's entry and C's reply was
        persisted with a placeholder in it."""
        import asyncio

        redis = fakeredis.aioredis.FakeRedis()
        script = {
            "msg A": (f"db at {IP_A} refuses", f"Check {PH_A}.", 0.05),
            "msg B": (f"also {IP_B} times out", f"And {PH_B}.", 0.0),
            "msg C": (
                f"hypotheses: {PH_A} refuses; {PH_B} slow",
                f"Compare {PH_A} with {PH_B}.",
                0.0,
            ),
        }
        engine = _locking_engine(_IpSanitizer(), redis, script)
        repo = _CopyRepo()
        await repo.save(case)
        service = _service(engine, repo)

        def turn(query):
            return service.process_turn(
                case_id=case.case_id,
                user_id=case.user_id,
                payload=TurnPayload(query=query),
            )

        reply_a, reply_b = await asyncio.gather(turn("msg A"), turn("msg B"))
        reply_c = await turn("msg C")

        assert reply_a.agent_response == f"Check {IP_A}."
        assert reply_b.agent_response == f"And {IP_B}."
        assert reply_c.agent_response == f"Compare {IP_A} with {IP_B}."
        # The stored row of turn C (the two concurrent turns shared a number;
        # the later commit won, which is the OCC's business, not this test's).
        row = repo.store[case.case_id].messages[-1]
        assert row["role"] == "assistant"
        assert row["content"] == f"Compare {IP_A} with {IP_B}."
        assert IP_A not in "".join(engine.sent) and IP_B not in "".join(engine.sent)

    async def test_the_title_and_a_following_turn_keep_both_entries(self, redaction_on):
        """The title loads the registry; a turn loads it too, registers a new
        value and saves while the title's model call is in flight; the title's
        prompt registered a value the registry lacked, and saves after."""
        import asyncio

        sanitizer = _IpSanitizer()
        redis = fakeredis.aioredis.FakeRedis()
        case_id = "case_title_race"
        title_started, turn_saved = asyncio.Event(), asyncio.Event()

        class _Router:
            calls: list = []

            async def generate(self, **kwargs):
                self.calls.append(kwargs)
                title_started.set()
                await turn_saved.wait()
                return _response("Database Connection Refusals On Primary")

        router = _Router()
        title_case = SimpleNamespace(
            case_id=case_id,
            title="Case-261012-1",
            description="",
            inquiry=SimpleNamespace(
                proposed_problem_statement="db refuses connections",
                problem_statement_confirmed=True,
            ),
            evidence=[],
            uploaded_files=[],
        )
        case_service = SimpleNamespace(
            get_case_conversation_context=AsyncMock(
                return_value=(
                    "User: the database refuses connections from the app tier "
                    f"on {HOST} and the replica lags badly. "
                )
                * 20
            ),
            update_case=AsyncMock(return_value=True),
            get_case=AsyncMock(
                return_value=SimpleNamespace(
                    title="Database Connection Refusals On Primary"
                )
            ),
        )

        async def title():
            await title_generation._generate_and_persist_title(
                case=title_case,
                case_id=case_id,
                user_id="u",
                case_service=case_service,
                llm_provider=router,
                max_words=8,
                hint=None,
                correlation_id="c",
                sanitizer=sanitizer,
                redis_client=redis,
            )

        async def following_turn():
            await title_started.wait()  # loaded before the title saves
            ctx = await service_module.load_case_redaction(case_id, sanitizer, redis)
            await ctx.asanitize(f"new host {IP_D}")
            await ctx.save()
            turn_saved.set()

        await asyncio.gather(title(), following_turn())

        assert router.calls, "the title never reached its model call"
        stored = {
            p.decode()
            for p in (await redis.hgetall(f"redaction:registry:{case_id}")).values()
        }
        assert stored == {PH_HOST, PH_D}
