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
        sanitizer, redis_client = object(), object()

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
    """``sanitize_text_with_registry`` over the one entity."""

    def sanitize_text_with_registry(self, text, registry):
        if RAW in text:
            registry.setdefault("EMAIL_ADDRESS", {})[RAW] = PSEUDONYM
        return text.replace(RAW, PSEUDONYM)


class _DictRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _context() -> CaseRedactionContext:
    return CaseRedactionContext(
        case_id="case_1956",
        sanitizer=_FakeSanitizer(),
        redis_client=_DictRedis(),
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
        assert ctx.redis_client.store, "the registry the turn grew was not saved"

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
