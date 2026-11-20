"""A log line names case content by its id and length, never the content (#2890).

Principles §7, "What a log line may hold", rule 3: "No case content, at any
level, DEBUG included. Case content is evidence text, a question or search
query, a case or runbook title, a prompt, or a model's reply. A line names the
content's identifier and length instead." Stage S4 of #2886.

Each case drives one site's function with :data:`MARKER` in the content,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``), so the message and the ``extra`` fields
are both on the line. The marker is absent from every rendered line, and the
content's length (and its id, where the line names one) is present.
"""

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from faultmaven.api.exception_handlers import request_validation_exception_handler
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    ConfidenceRepair,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.milestone_inference import (
    _apply_symptom_retraction,
)
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _account_confidence,
    _fix_enum_violations,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _flatten_follow_ups,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, render_fenced
from faultmaven.core.investigation.schemas import (
    MilestoneJustifications,
    SuggestedFollowUp,
)
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.logging.config import build_log_formatter
from faultmaven.models.api import KnowledgeBaseDocument, SourceMetadata
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.find_entity_tool import FindEntityTool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.auth.api.session import search_sessions
from faultmaven.modules.auth.contracts import DevUser, UserDTO
from faultmaven.modules.case.api import title_generation
from faultmaven.modules.case.api.routes.cases import generate_case_title
from faultmaven.modules.case.contracts import EvidenceStance, ProblemStatus
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.knowledge.domain.models.conversion import CaseConversionRequest
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    _triage_confidence,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService
from tests.case_builders import investigating_case, make_case
from tests.model_content import disabled_redaction, vetted
from tests.unit.infrastructure.llm.providers.test_local_tool_calls import _mock_session
from tests.unit.modules.agent.conftest import (
    RecordingCaseRepository,
    create_sample_case,
)
from tests.unit.modules.agent.test_out_of_band_turns_1329 import (
    ENTERPRISE,
    InMemoryTurnLedger,
    _service,
    _turn,
)
from tests.unit.modules.agent.tools.test_search_file_tool import (
    _make_evidence,
    _wire_case_lookup,
)
from tests.unit.modules.knowledge.test_error_text_not_echoed import _client
from tests.unit.modules.knowledge.test_lexical_search_contract_1288 import (
    USER,
    search_service,
)
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene
from tests.unit.modules.preprocessing.test_preprocessing_page_capture_passthrough import (
    _make_classification_mock,
)
from tests.utils import admit_every_parent, regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.security]

#: The content every case plants. Plain letters and digits, so neither the
#: redactor nor a truncation can be what keeps it off the line.
MARKER = "zqxmarker2890"

#: A content string of a known length, carrying the marker.
CONTENT = f"disk full on {MARKER} node"

#: A title carrying the marker, as a user or a model would write one.
TITLE = f"Disk full on {MARKER}"


@pytest.fixture
def lines(caplog):
    """Every record the test emits, at DEBUG, as the chain's JSON lines."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="faultmaven")
    formatter = build_log_formatter("json", False, io.StringIO())

    def render() -> list[str]:
        return [formatter.format(record) for record in caplog.records]

    return render


def _named(rendered: list[str], event: str, *present: str) -> dict:
    """The marker is on no line; the one line whose event holds ``event``
    carries every ``present`` text. Returns that line's fields."""
    assert rendered, "nothing was logged"
    for line in rendered:
        assert MARKER not in line, line
    hits = [line for line in rendered if event in json.loads(line)["event"]]
    assert hits, f"no line holds {event!r}: {rendered}"
    for text in present:
        assert any(text in line for line in hits), (text, hits)
    return json.loads(hits[0])


# ── intent_resolver.py ────────────────────────────────────────────────────


def test_classifier_reply_is_named_by_length(lines):
    reply = f"choice {MARKER} 12"
    assert IntentResolver(llm_router=None)._parse_response(reply, []) is None
    _named(lines(), "unparseable response", f"text_len={len(reply.lower())}")


# ── hypothesis_manager.py ─────────────────────────────────────────────────


def test_hypothesis_lines_name_the_hypothesis_not_its_statement(lines):
    manager = HypothesisManager()
    h = manager.create_hypothesis(CONTENT, "config", 0.4, current_turn=1)
    manager.link_evidence(h, "ev_1", EvidenceStance.SUPPORTS, turn=1)
    manager.link_evidence(
        h, "ev_1", EvidenceStance.REFUTES, turn=2, stance_confidence=0.9
    )
    stagnant = manager.create_hypothesis(CONTENT, "config", 0.4, current_turn=1)
    stagnant.iterations_without_progress = 99
    manager.check_state_transition(stagnant, turn=3)
    manager.refute_hypothesis(h, 2, ["ev_1"], reason=f"user said {MARKER}")
    rendered = lines()
    _named(rendered, "Created hypothesis", h.hypothesis_id, f"len={len(CONTENT)}")
    linked = _named(rendered, "Linked supports evidence", "ev_1")
    assert linked["statement_len"] == len(CONTENT)
    assert linked["hypothesis_id"] == h.hypothesis_id
    revised = _named(rendered, "Revised refutes evidence link", "ev_1")
    assert revised["statement_len"] == len(CONTENT)
    _named(rendered, "INCONCLUSIVE", stagnant.hypothesis_id, f"len={len(CONTENT)}")
    _named(rendered, "REFUTED", h.hypothesis_id, f"reason_len={len(MARKER) + 10}")


def test_a_refutation_without_a_reason_still_logs(lines):
    """The length of an absent reason is 0: the line cannot raise after the
    hypothesis has already been refuted."""
    manager = HypothesisManager()
    h = manager.create_hypothesis(CONTENT, "config", 0.4, current_turn=1)
    manager.refute_hypothesis(h, 2, [], reason=None)
    _named(lines(), "REFUTED", h.hypothesis_id, "reason_len=0")


def _user() -> DevUser:
    return DevUser(
        user_id="user-1",
        username="user-1",
        email="user-1@example.com",
        display_name="user-1",
        created_at=datetime.now(timezone.utc),
        organization_id="org-1",
    )


# ── document_qa_tool.py ───────────────────────────────────────────────────


async def test_kb_question_is_named_by_scope_and_length(lines):
    weak = UnifiedKBConfig().relevance_threshold - 0.1
    chunk = {"content": "x", "score": weak, "metadata": {"parent_document_id": "rb"}}
    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=[chunk])
    store.search = AsyncMock(return_value=[chunk])
    await DocumentQATool(store, MagicMock(), UnifiedKBConfig()).answer_with_citations(
        CONTENT,
        "scope-7",
        filters={"scope": "global"},
        redaction_ctx=disabled_redaction(),
        admit=admit_every_parent,
    )
    _named(lines(), "Query:", f"question_len={len(CONTENT)}", "scope_id: scope-7")


# ── knowledge_service.py ──────────────────────────────────────────────────


def _kb_service(store) -> KnowledgeService:
    return KnowledgeService(
        sanitizer=MagicMock(asanitize=AsyncMock(side_effect=lambda q: q)),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=MagicMock(),
    )


async def test_kb_search_query_is_named_by_length(lines):
    store = MagicMock(hybrid_search=AsyncMock(return_value=[]))
    await _kb_service(store).search_knowledge(CONTENT, limit=5)
    rendered = lines()
    _named(rendered, "Searching knowledge base", f"query_len={len(CONTENT)}")
    _named(rendered, "Found 0 results", f"query_len={len(CONTENT)}")


async def test_indexed_document_is_named_by_id_and_title_length(lines):
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = MagicMock(add_documents=AsyncMock())
    document = KnowledgeBaseDocument(
        document_id="doc-1",
        title=CONTENT,
        content="# Steps\n\nCordon, then drain.",
        document_type="runbook",
        scope="global",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    await service._index_document_in_vector_store(
        document,
        prechunked=[("Cordon, then drain.", [0.1] * 1024)],
        enterprise_id="ent-1",
        generation="gen-1",
    )
    fields = _named(lines(), "Indexed document into vector store")
    assert fields["document_id"] == "doc-1"
    assert fields["title_len"] == len(CONTENT)


async def test_semantic_search_is_named_by_user_and_query_length(lines):
    service = _kb_service(MagicMock(search=AsyncMock(return_value=[])))
    service._share_repo = None
    await service.search_documents(CONTENT, user=USER)
    _named(lines(), "Semantic search", "user_id=user-1", f"query_len={len(CONTENT)}")


async def test_fulltext_search_is_named_by_user_and_query_length(lines, search_service):
    await search_service.fulltext_search_documents(query=CONTENT, user=USER)
    _named(lines(), "Full-text search", "user_id=user-1", f"query_len={len(CONTENT)}")


# ── modules/knowledge/api/routes.py ───────────────────────────────────────


def test_fulltext_search_route_is_named_by_user_and_query_length(lines):
    service = MagicMock()
    service.fulltext_search_documents = AsyncMock(return_value={"total_results": 0})
    response = _client(service, user=_user()).post(
        "/knowledge/documents/search", json={"query": CONTENT}
    )
    assert response.status_code == 200, response.text
    _named(lines(), "Full-text search", "user_id=user-1", f"query_len={len(CONTENT)}")


# ── knowledge_vector_store.py ─────────────────────────────────────────────


async def test_failed_keyword_search_is_named_by_collection_and_length(lines):
    store = KnowledgeVectorStore.__new__(KnowledgeVectorStore)
    store._single_keyword_search = AsyncMock(side_effect=RuntimeError("store busy"))
    keyword = f"{MARKER}x"
    await store._keyword_constrained_search("kb_docs", [0.1], [keyword], k=3)
    _named(
        lines(),
        "Keyword-constrained search",
        "kb_docs",
        f"keyword_len={len(keyword)}",
    )


# ── infrastructure/llm/providers/registry.py ──────────────────────────────


async def test_low_confidence_reply_is_named_by_provider_and_length(lines):
    registry = ProviderRegistry.__new__(ProviderRegistry)
    registry._initialized = True
    registry._routing_initialized = True
    registry._sticky_provider = None
    registry.logger = logging.getLogger(
        "faultmaven.infrastructure.llm.providers.registry"
    )
    registry._providers = {}
    registry._provider_states = {"fireworks": ProviderState(name="fireworks")}
    registry._provider_classes = {}
    registry._fallback_chain = ["fireworks"]

    async def _generate(*args, **kwargs):
        return LLMResponse(
            content=CONTENT,
            confidence=0.1,
            provider="fireworks",
            model="test-model",
            tokens_used=0,
            response_time_ms=0,
        )

    registry._providers["fireworks"] = MagicMock(_generate=_generate)
    await registry.route_request(prompt=vetted("question"))
    _named(
        lines(),
        "Low confidence response content",
        "fireworks",
        f"content_len={len(CONTENT)}",
    )


# ── infrastructure/llm/providers/local_provider.py ────────────────────────


async def test_local_provider_names_prompt_and_reply_by_length(lines):
    provider = LocalProvider(
        ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:5000",
            models=["functionary-7b-v2"],
            default_model="functionary-7b-v2",
            timeout=30,
        )
    )
    reply = f"  the reply {MARKER}  "
    data = {
        "choices": [{"message": {"role": "assistant", "content": reply}}],
        "usage": {"total_tokens": 4},
    }
    with patch("aiohttp.ClientSession", return_value=_mock_session(data)):
        await send_to_model(provider, prompt=vetted(CONTENT))
    rendered = lines()
    _named(
        rendered,
        "Request payload",
        "functionary-7b-v2",
        f"messages=1, content_len={len(CONTENT)}, prompt_len={len(CONTENT)}",
    )
    _named(rendered, "Response data", "response_len=")
    _named(rendered, "Raw content", f"content_len={len(reply)}")
    _named(rendered, "Validated content", f"content_len={len(reply.strip())}")


async def test_local_provider_names_sent_messages_by_count_and_length(lines):
    """A conversation is sent as its messages, not the prompt: the line counts
    and measures what was sent, and names no prompt length."""
    provider = LocalProvider(
        ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:5000",
            models=["functionary-7b-v2"],
            default_model="functionary-7b-v2",
            timeout=30,
        )
    )
    sent = [
        {"role": "system", "content": "You are terse."},
        {  # multimodal: the text parts are what is measured
            "role": "user",
            "content": [
                {"type": "text", "text": CONTENT},
                {"type": "image_url", "image_url": {"url": "data:image/png;x"}},
            ],
        },
    ]
    data = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    with patch("aiohttp.ClientSession", return_value=_mock_session(data)):
        await provider._call_openai_compatible_api(
            "unused", "functionary-7b-v2", 100, 0.0, messages=sent
        )
    line = _named(lines(), "Request payload", "messages=2")
    assert f"content_len={len('You are terse.') + len(CONTENT)}" in line["event"]
    assert "prompt_len" not in line["event"]


# ── modules/auth/api/session.py ───────────────────────────────────────────


async def test_session_search_query_is_named_by_user_and_length(lines):
    service = MagicMock(search_sessions=AsyncMock(return_value=[]))
    await search_sessions(
        search_params={"query": CONTENT},
        session_service=service,
        current_user=_user(),
    )
    _named(lines(), "Search returned 0 sessions", "user-1", f"query_len={len(CONTENT)}")


# ── document_preprocessor.py ──────────────────────────────────────────────


def test_triage_confidence_repair_is_named_by_type_and_length(lines):
    _triage_confidence(MARKER)
    fields = _named(
        lines(),
        "Content triage confidence repaired",
        f"<str, raw_len={len(MARKER)}>",
    )
    assert fields["raw_type"] == "str"
    assert fields["raw_len"] == len(MARKER)


# ── api/exception_handlers.py ─────────────────────────────────────────────


async def test_validation_error_is_named_by_loc_type_and_input_length(lines):
    oversized = f"{MARKER} " + "x" * 4000  # echoed as a placeholder (#1048)
    exc = RequestValidationError(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "query"),
                "msg": "String should have at most 3 characters",
                "input": CONTENT,
                "ctx": {"max_length": 3},
            },
            {  # a validator's message can quote the value: not logged
                "type": "value_error",
                "loc": ("body", "title"),
                "msg": f"Value error, {MARKER} is not a title",
                "input": TITLE,
                "ctx": {"error": ValueError(f"{MARKER} is not a title")},
            },
            {  # the client's own key, not one the schema declared
                "type": "extra_forbidden",
                "loc": ("body", f"{MARKER}_key"),
                "msg": "Extra inputs are not permitted",
                "input": 1,
            },
            {
                "type": "string_too_long",
                "loc": ("body", "note"),
                "msg": "String should have at most 3 characters",
                "input": oversized,
                "ctx": {"max_length": 3},
            },
            {
                "type": "string_type",
                "loc": ("body", "name"),
                "msg": "Input should be a valid string",
                "input": None,
            },
        ]
    )
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/x"))
    response = await request_validation_exception_handler(request, exc)
    assert response.status_code == 422
    assert CONTENT in response.body.decode(), "the response still echoes it"
    fields = _named(lines(), "Validation error on POST /x", "string_too_long")
    too_long = "String should have at most 3 characters"
    assert fields["validation_errors"] == [
        {
            "loc": ["body", "query"],
            "type": "string_too_long",
            "msg": too_long,
            "ctx": {"max_length": 3},
            "input_len": len(CONTENT),
        },
        {"loc": ["body", "title"], "type": "value_error", "input_len": len(TITLE)},
        {
            "loc": ["body", f"<key_len={len(MARKER) + 4}>"],
            "type": "extra_forbidden",
            "msg": "Extra inputs are not permitted",
            "input_type": "int",
        },
        {
            "loc": ["body", "note"],
            "type": "string_too_long",
            "msg": too_long,
            "ctx": {"max_length": 3},
            "input_len": len(oversized),
        },
        {
            "loc": ["body", "name"],
            "type": "string_type",
            "msg": "Input should be a valid string",
            "input_type": "NoneType",
        },
    ]


async def test_pydantic_messages_that_quote_the_input_are_not_logged(lines):
    """``union_tag_invalid`` quotes the input's tag, ``uuid_parsing`` one of
    its characters: their ``msg`` and ``ctx`` stay off the line."""
    from typing import Literal, Union
    from uuid import UUID

    from pydantic import BaseModel, Field, ValidationError

    class A(BaseModel):
        k: Literal["a"]

    class B(BaseModel):
        k: Literal["b"]

    class Body(BaseModel):
        x: Union[A, B] = Field(discriminator="k")
        u: UUID

    with pytest.raises(ValidationError) as raised:
        Body.model_validate({"x": {"k": MARKER}, "u": "q-not-a-uuid"})
    errors = [{**e, "loc": ("body", *e["loc"])} for e in raised.value.errors()]
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/x"))
    await request_validation_exception_handler(request, RequestValidationError(errors))
    fields = _named(lines(), "Validation error on POST", "union_tag_invalid")
    entries = {e["type"]: e for e in fields["validation_errors"]}
    assert set(entries) == {"union_tag_invalid", "uuid_parsing"}
    for entry in entries.values():
        assert "msg" not in entry and "ctx" not in entry, entry
    assert "`q`" not in json.dumps(fields)


# ── case_service.py: create_case ──────────────────────────────────────────


async def test_created_case_is_named_by_id_and_title_length(lines):
    repo = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda case: case)
    repo.list = AsyncMock(return_value=([], 0))
    service = CaseService(case_repository=repo, session_store=AsyncMock())
    case = await service.create_case(title=TITLE, owner_id="user_123")
    _named(lines(), "Created case", case.case_id, f"title_len={len(TITLE)}")


# ── routes/cases.py: generate_case_title ──────────────────────────────────


async def test_title_route_names_hint_and_title_by_length(lines):
    case = make_case(title=TITLE, user_id="user_123", enterprise_id="org_test")
    service = AsyncMock()
    service.require_case = AsyncMock(return_value=case)
    service.get_case = AsyncMock(return_value=case)
    service.get_case_conversation_context = AsyncMock(return_value="")
    request = MagicMock()
    request.app.state.sanitizer = regex_only_sanitizer()
    request.app.state.redis_client = None
    hint = f"about {MARKER}"
    with pytest.raises(ValidationException):  # too thin to name: no LLM call
        await generate_case_title(
            case_id=case.case_id,
            request=request,
            response=MagicMock(headers={}),
            request_body={"hint": hint},
            force=False,
            case_service=service,
            current_user=UserDTO(
                user_id="user_123",
                username="u",
                email="u@example.com",
                display_name="U",
                is_active=True,
            ),
        )
    rendered = lines()
    params = _named(rendered, "Effective parameters", f"hint_len={len(hint)}")
    assert params["case_id"] == case.case_id and params["hint_len"] == len(hint)
    got = _named(rendered, "Case retrieved", f"title_len={len(TITLE)}")
    assert got["case_id"] == case.case_id
    assert got["existing_title_len"] == len(TITLE)


# ── title_generation.py ───────────────────────────────────────────────────


def _titleable_case(title="Case-260101-1"):
    case = make_case(title=title, user_id="user_123", enterprise_id="org_test")
    case.inquiry.proposed_problem_statement = (
        "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
    )
    return case


def _title_service(case, *, writes=True, persist=True):
    service = AsyncMock()
    service.get_case = AsyncMock(return_value=case)
    service.get_case_conversation_context = AsyncMock(return_value="")

    async def _update(case_id, updates, user_id=None):
        if writes and "title" in updates:
            case.title = updates["title"]
        return persist

    service.update_case = AsyncMock(side_effect=_update)
    return service


@pytest.fixture
def generated(monkeypatch):
    """The generator answers :data:`TITLE` (no model involved)."""

    async def _title(*args, **kwargs):
        return TITLE, "llm"

    monkeypatch.setattr(title_generation, "_generate_title_with_llm", _title)


async def _persist(case, service):
    return await title_generation._generate_and_persist_title(
        case=case,
        case_id=case.case_id,
        user_id="user_123",
        case_service=service,
        llm_provider=None,
        max_words=8,
        hint=None,
        sanitizer=regex_only_sanitizer(),
        redis_client=None,
    )


async def test_persisted_title_is_named_by_length(lines, generated):
    case = _titleable_case()
    await _persist(case, _title_service(case))
    fields = _named(lines(), "Title persistence verified", case.case_id)
    assert fields["title_len"] == len(TITLE)


async def test_failed_title_write_is_named_by_length(lines, generated):
    case = _titleable_case()
    with pytest.raises(HTTPException):
        await _persist(case, _title_service(case, persist=False))
    fields = _named(lines(), "Failed to persist generated title", case.case_id)
    assert fields["generated_title_len"] == len(TITLE)


async def test_unverified_title_is_named_by_lengths(lines, generated):
    standing = f"Old {MARKER} title"
    case = _titleable_case(title=standing)
    with pytest.raises(HTTPException):
        await _persist(case, _title_service(case, writes=False))
    fields = _named(
        lines(),
        "verification failed",
        case.case_id,
        f"expected_len={len(TITLE)}",
        f"actual_len={len(standing)}",
    )
    assert fields["expected_title_len"] == len(TITLE)
    assert fields["actual_title_len"] == len(standing)


async def test_auto_title_is_named_by_length(lines, generated):
    case = _titleable_case()
    await title_generation._auto_title_case_if_default(
        case_id=case.case_id,
        user_id="user_123",
        case_service=_title_service(case),
        llm_provider=None,
        sanitizer=regex_only_sanitizer(),
        redis_client=None,
    )
    assert case.title == TITLE, "premise: the auto-title landed"
    fields = _named(lines(), "Auto-titled case", case.case_id)
    assert fields["title_len"] == len(TITLE)


# ── runbook_creation.py: _run_runbook_conversion ──────────────────────────


async def test_runbook_draft_is_named_by_id_and_title_length(lines):
    draft = MagicMock(runbook_id="rb_0123456789ab", title=TITLE, quality_score=0.9)
    conversion = MagicMock()
    conversion.convert_from_case = AsyncMock(return_value=MagicMock(drafts=[draft]))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)  # no notice to write: ends there
    llm = MagicMock()
    llm.route = AsyncMock()
    engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
    request = CaseConversionRequest(
        case_id="case_aabb11223344", title="Pool", description="d", scope="global"
    )
    committed = asyncio.get_running_loop().create_future()
    committed.set_result(None)
    await engine.runbooks._run_runbook_conversion(
        conversion, request, "u1", "o1", committed=committed
    )
    fields = _named(
        lines(), "Runbook draft created", "rb_0123456789ab", f"title_len={len(TITLE)}"
    )
    assert fields["case_id"] == "case_aabb11223344"


# ── investigation_service/service.py: _dispatch_turn ──────────────────────


@pytest.fixture
def bound():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def test_resolved_intent_is_named_by_case_and_query_length(lines, bound):
    """Driven through ``process_turn`` with the out-of-band suite's doubles: the
    resolver matches typed text to an offered choice."""
    engine = MagicMock(spec=MilestoneEngine)

    async def spy(*, case, **_):
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": "engine answer",
            "metadata": {"milestones_completed": [], "progress_made": False},
        }

    engine.process_turn = AsyncMock(side_effect=spy)
    repo = RecordingCaseRepository()
    case = create_sample_case()
    case.user_id = "user_123"
    service = _service(engine, repo, InMemoryTurnLedger())
    resolved = {"type": "conversation"}
    service.intent_resolver.resolve = AsyncMock(return_value=resolved)
    case.last_suggestions = [
        {
            "label": "Return to remediation",
            "payload": "Return to remediation",
            "action_type": "DECIDE",
            "intent": resolved,
            "offered_turn": case.current_turn,
        }
    ]
    query = f"return to the {MARKER} remediation"
    await _turn(service, repo, case, query=query)
    _named(
        lines(),
        "Intent resolved from suggestions",
        case.case_id,
        f"query_len={len(query)}",
    )


# ── preprocessing_service.py: classify_and_extract ────────────────────────


async def test_invalid_page_capture_is_named_by_length(lines):
    classifier = MagicMock()
    classifier.classify.return_value = _make_classification_mock()
    service = PreprocessingService(classifier=classifier, logs_extractor=CrimeScene())
    content = "\n".join(f"plain line {i} {MARKER}" for i in range(250))
    await service.classify_and_extract(
        content=content,
        filename="captured.html",
        source_metadata=SourceMetadata(source_type="page_capture"),
        principal="u1",
    )
    fields = _named(lines(), "page_capture shape invalid")
    assert fields["content_len"] == len(content)


# ── search_file_tool.py ───────────────────────────────────────────────────


@pytest.mark.parametrize("output_format", ["excerpts", "count"])
async def test_file_search_is_named_by_evidence_and_query_length(lines, output_format):
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(
        return_value=f"line 1\nline 2 {MARKER} timeout\nline 3\n".encode()
    )
    tool = SearchFileTool(storage_service=storage, context_lines=5, max_results=3)
    case = MagicMock()
    case.case_id = "case_123"
    case.evidence = [_make_evidence()]
    _wire_case_lookup(case)
    context = ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id="user_1",
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )
    result = await tool.execute_with_context(
        params={
            "evidence_id": "ev_abc",
            "query": MARKER,
            "output_format": output_format,
        },
        context=context,
    )
    assert result.success is True
    _named(
        lines(),
        f"format={output_format}",
        "ev_abc",
        f"query_len={len(MARKER)}",
    )


# ── find_entity_tool.py ───────────────────────────────────────────────────


async def test_failed_entity_lookup_is_named_by_case_and_value_length(lines):
    repo = MagicMock()
    # The exception's own text is S3's (#2889); this one carries no content.
    repo.find_entity = AsyncMock(side_effect=RuntimeError("store down"))
    context = ToolContext(
        session_id="sess_1",
        case_id="case_abc",
        enterprise_id="org_1",
        user_id="user_1",
        redaction_ctx=disabled_redaction(),
    )
    result = await FindEntityTool(case_repository=repo).execute_with_context(
        params={"entity_value": TITLE}, context=context
    )
    assert result.success is False
    _named(lines(), "find_entity failed", "case_abc", f"value_len={len(TITLE)}")


def _case(**kw):
    return investigating_case(
        case_id="case_000000002890",
        current_turn=5,
        statement="checkout 500s",
        symptom="checkout 500s",
        problem_status=ProblemStatus.VERIFIED,
        **kw,
    )


# ── prompts/fence.py ──────────────────────────────────────────────────────


def test_an_absorbed_blob_is_named_by_length(lines):
    hostile = f'line one\n<uploaded_file label="{MARKER}.log" file_backed="true"'

    def render(f: PromptFence) -> str:
        # Hand-rolled: raw bodies bypassing ``element`` (test_fence.py), five
        # of them, so the lengths' volume bound shows.
        return "".join(
            f.open("file_extract")
            + "\n"
            + f.data(hostile)
            + "\n"
            + f.close("file_extract")
            for _ in range(5)
        )

    render_fenced(render, token_source=lambda: "aaaaaaaa")
    line = _named(lines(), "prompt_fence_absorbed_delimiter")
    assert line["count"] > 3
    assert len(line["blob_lens"]) == 3 and all(line["blob_lens"])


# ── milestone_engine/turn_records.py ──────────────────────────────────────


def test_a_bare_payload_card_is_named_by_label_and_payload_length(lines):
    label = f"Check {MARKER} pool config"
    # The line fires only on a payload that reads as a bare gate reply, which no
    # marker survives (``card_reads_as_bare_reply``): the payload's own text is
    # the content that must stay off the line.
    payload = "\ufeffProceed"
    card = SuggestedFollowUp(label=label, action_type="DECIDE", payload=payload)
    _flatten_follow_ups([card], {})
    line = _named(
        lines(),
        "LLM DECIDE card",
        f"label_len={len(label)}",
        f"send_len={len(payload)}",
    )
    assert line["card_label_len"] == len(label)
    assert "Proceed" not in json.dumps(line)


# ── milestone_engine/structured_output.py ─────────────────────────────────


def test_a_hallucinated_enum_is_named_by_length(lines):
    valid = "connection_pool_exhausted_on_primary_database"
    schema = {"properties": {"status": {"enum": [valid, "inactive"]}}}
    near = f"connection_pool_{MARKER}_exhausted_on_primary_database"
    far = f"{MARKER} nothing like it"
    assert _fix_enum_violations({"status": near}, schema) == {"status": valid}
    assert _fix_enum_violations({"status": far}, schema) == {"status": valid}
    rendered = lines()
    _named(rendered, "Auto-correcting hallucinated enum", f"value_len={len(near)}")
    _named(rendered, "No close match", f"value_len={len(far)}")


def test_a_confidence_repair_is_named_by_type_and_length(lines):
    raw = f"{MARKER} percent"
    repair = ConfidenceRepair(
        schema="HypothesisToAdd",
        field="likelihood",
        action=ConfidenceAction.DROPPED,
        raw=raw,
    )
    _account_confidence(SimpleNamespace(), [repair], None)
    line = _named(lines(), "structured_output_confidence_repaired")
    assert line["repairs"] == [
        f"HypothesisToAdd.likelihood: <str, raw_len={len(raw)}> is not a confidence"
        " in [0, 1]; field dropped, stored value kept"
    ]


# ── milestone_engine/milestone_inference.py ───────────────────────────────


def test_a_retraction_rationale_is_named_by_length(lines):
    case = _case()
    rationale = f"Misread the dashboard: {MARKER} was staging."
    response = SimpleNamespace(
        evidence_trail=SimpleNamespace(
            milestone_justifications=MilestoneJustifications(symptom_verified=rationale)
        )
    )
    meta: dict = {}
    assert _apply_symptom_retraction(
        case, SimpleNamespace(symptom_verified=False), response, meta
    )
    _named(lines(), "RETRACTED", case.case_id, f"rationale_len={len(rationale)}")


# ── evidence_need_linking.py ──────────────────────────────────────────────


class _FollowUp:
    """The attributes the linker reads (test_evidence_need_linking.py)."""

    def __init__(self, body):
        self.action_type, self.body, self.label = "EVIDENCE", body, ""
        self.evidence_need_id = None


def test_a_capped_ask_is_named_by_length(lines):
    from faultmaven.core.investigation.evidence_need_linking import (
        _MAX_BACKFILLED_PER_TURN,
        link_evidence_suggestions_to_needs,
    )

    asks = [
        "kubectl pod restart counts",
        "postgres slow query log",
        "nginx upstream latency histogram",
        "kafka consumer lag metrics",
        "redis eviction statistics",
        "systemd journal boot errors",
    ][:_MAX_BACKFILLED_PER_TURN]
    capped = f"{MARKER} terraform plan diff"
    case = SimpleNamespace(
        case_id="case_000000002890", evidence_needs=[], hypotheses={}
    )
    follow_ups = [_FollowUp(a) for a in [*asks, capped]]
    link_evidence_suggestions_to_needs(case, follow_ups, {}, 5, lambda r, c, p: r)
    _named(
        lines(), "Backfill cap reached", "case_000000002890", f"ask_len={len(capped)}"
    )


# ── milestone_engine/evidence_need_updates.py ─────────────────────────────


def test_a_rejected_causal_need_is_named_by_length(lines):
    from faultmaven.core.investigation.milestone_engine.evidence_need_updates import (
        _apply_evidence_need_updates,
    )
    from faultmaven.modules.case.contracts import NeedPriority, NeedPurpose

    case = _case()
    update = SimpleNamespace(
        need_id=None,
        purpose=NeedPurpose.CAUSAL_VERIFICATION,
        request_text=CONTENT,
        rationale="confirm cause",
        priority=NeedPriority.MEDIUM,
        state=None,
        motivating_hypothesis_ids=["hyp_dangling"],
        fulfilling_evidence_ids=[],
        superseded_reason=None,
    )
    _apply_evidence_need_updates(case, [update], {}, case.current_turn)
    _named(
        lines(),
        "Rejected causal-purpose evidence_need create",
        case.case_id,
        f"request_text_len={len(CONTENT)}",
    )


# ── milestone_engine/kb_prefetch.py ───────────────────────────────────────


async def test_prefetched_runbooks_are_named_by_id_and_title_length(lines, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from tests.unit.core.investigation.test_kb_push_policy_1360_1361 import (
        _case as kb_case,
    )
    from tests.unit.core.investigation.test_kb_push_policy_1360_1361 import _engine
    from tests.utils import reset_settings_singleton

    monkeypatch.setenv("KB_PREFETCH_ENABLED", "true")
    reset_settings_singleton()
    title = f"Disk full on {MARKER} runbook"
    hits = [
        SimpleNamespace(
            title=title,
            snippet="Check the volume",
            score=0.8,
            document_type="runbook",
            parent_document_id="rb_disk_full",
        )
    ]
    service = MagicMock()
    service.search_knowledge = AsyncMock(return_value=hits)
    case = kb_case()
    try:
        await _engine(service).kb_prefetcher.prefetch_kb_context(
            case, "disk full", "symptom"
        )
    finally:
        reset_settings_singleton()
    line = _named(
        lines(), "KB pre-fetch", "rb_disk_full", f"title_len={len(title)}", case.case_id
    )
    assert line["kb_runbook_ids"] == ["rb_disk_full"]
    assert line["kb_runbook_title_lens"] == [len(title)]


# ── milestone_engine/stage_gates.py ───────────────────────────────────────


def test_an_rca_infeasible_close_is_named_by_proposal_and_length(lines):
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _apply_stage_gate_side_effects,
    )
    from tests.unit.core.investigation.test_rca_infeasible_closure import (
        _make_case as rca_case,
    )

    rationale = f"third-party {MARKER} outage"
    case = rca_case(rca_infeasible=True, rationale=rationale)
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "it worked", {})
    proposal_id = case.offer_record.open.proposal_id
    _named(
        lines(),
        "Proposed CLOSED transition",
        case.case_id,
        f"proposal_id={proposal_id}",
        f"rationale_len={len(rationale)}",
    )


# ── milestone_engine/generation.py ────────────────────────────────────────


async def test_a_tool_call_is_named_by_id_and_argument_length(lines):
    import json
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.models.interfaces import ToolResult
    from tests.model_content import disabled_redaction, vetted
    from tests.unit.core.investigation.test_milestone_engine_tool_loop import (
        SampleResponse,
        _make_engine,
        _make_mock_registry,
        _make_schema_response,
        _make_tool_call_response,
    )

    arguments = {"query": f"ssh {MARKER} failed"}
    provider = AsyncMock()
    provider.route = AsyncMock(
        side_effect=[
            _make_tool_call_response("search_file", arguments, call_id="call_2890"),
            _make_schema_response({"agent_response": "ok", "next_action": "go"}),
        ]
    )
    registry = _make_mock_registry()
    registry.execute_tool.return_value = ToolResult(success=True, data="Line 1")
    engine = _make_engine(mock_provider=provider, mock_registry=registry)
    tools = [{"type": "function", "function": {"name": "search_file"}}]
    await engine.generator._tool_augmented_generate(
        prompt=vetted("Investigate SSH issues"),
        schema_model=SampleResponse,
        investigation_tools=tools,
        tool_context=MagicMock(),
        redaction_ctx=disabled_redaction(),
    )
    _named(
        lines(),
        "LLM called tool",
        "call_2890",
        f"args_len={len(json.dumps(arguments))}",
    )


# ── milestone_engine/response_application.py ──────────────────────────────


def _inquiry_engine(replies):
    """An engine over the scripted model replies (test_inquiry_transition.py)."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
    from tests.unit.core.investigation.test_inquiry_transition import MockLLMProvider

    llm = MockLLMProvider()
    llm.route = AsyncMock(side_effect=[json.dumps(r) for r in replies])
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock()
    return MilestoneEngine(llm, repo, investigation_tools=MagicMock())


def _inquiry_case(statement=None):
    from faultmaven.modules.case.contracts import Case, CaseState, InquiryData

    return Case(
        case_id="case_000000002890",
        title="Test Inquiry",
        state=CaseState.INQUIRY,
        user_id="user_123",
        enterprise_id="org_123",
        description="",
        inquiry=InquiryData(
            thread_id="thread_123", proposed_problem_statement=statement
        ),
    )


async def test_a_same_turn_statement_and_the_message_are_named_by_length(lines):
    statement = f"API returning 503 errors on {MARKER}"
    engine = _inquiry_engine(
        [
            {
                "agent_response": "Starting investigation into API 503 errors.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                    },
                    "proposed_problem_statement": statement,
                    "user_confirmed_investigation": True,
                },
            }
        ]
    )
    case = _inquiry_case()
    message = f"My API on {MARKER} returns 503s, please investigate."
    await engine.process_turn(case, message, redaction_ctx=None)
    rendered = lines()
    _named(rendered, "user_message_len", case.case_id, f"={len(message)}")
    deferred = _named(rendered, "Same-turn-confirmation guard", case.case_id)
    assert deferred["statement_len"] == len(statement)


async def test_a_confirmed_statement_is_named_by_length(lines):
    from faultmaven.core.investigation.schemas import InquiryResponse

    statement = f"API returning 503 errors on {MARKER}"
    case = _inquiry_case(statement)
    updates = InquiryResponse.InquiryStateUpdate(user_confirmed_investigation=True)
    await _inquiry_engine([]).responses._apply_inquiry_updates(
        case, updates, {}, user_message="yes"
    )
    assert case.inquiry.problem_statement_confirmed
    _named(
        lines(),
        "User confirmed problem statement",
        case.case_id,
        f"statement_len={len(statement)}",
    )


async def test_investigation_updates_name_their_content_by_id_and_length(lines):
    from faultmaven.core.investigation.schemas import (
        BlockerType,
        EvidenceToAdd,
        HypothesisCategory,
        MissingCriticalData,
    )
    from faultmaven.modules.case.contracts import (
        EvidenceCategory,
        EvidenceSourceType,
        UploadedFile,
    )
    from tests.unit.core.investigation.test_hypothesis_dedup import (
        _DSU,
        _add_hyp,
        _engine,
        _h2a,
        _meta,
    )
    from tests.unit.core.investigation.test_hypothesis_dedup import _case as dedup_case

    case = dedup_case()
    case.uploaded_files.append(
        UploadedFile(
            file_id="file_0123456789ab",
            filename="pods.log",
            size_bytes=512,
            uploaded_at_turn=4,
        )
    )
    standing = _add_hyp(
        case,
        f"connection pool exhausted under load {MARKER}",
        HypothesisCategory.DATABASE,
    )
    duplicate = f"under load the connection pool exhausted {MARKER}"
    blocker = f"the {MARKER} log is truncated"
    summary = f"pods crashlooping on {MARKER}"
    updates = _DSU(
        missing_critical_data=MissingCriticalData(
            blocker_type=BlockerType.DATA_INCOMPLETE,
            description=blocker,
            what_was_expected="a full log",
            what_was_found="a partial log",
            impact="no timeline",
        ),
        evidence_to_add=[
            EvidenceToAdd(
                summary=summary,
                extract="OOMKilled exit 137",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_0123456789ab",
            )
        ],
        hypotheses_to_add=[_h2a(duplicate)],
    )
    meta = _meta()
    await _engine().responses._apply_investigation_updates(case, updates, meta)
    assert meta["evidence_added"], "the evidence row was minted"
    rendered = lines()
    _named(rendered, "data blocker", case.case_id, f"description_len={len(blocker)}")
    _named(rendered, "Evidence creation check", case.case_id, "count=1")
    _named(
        rendered,
        "Created evidence",
        meta["evidence_added"][0],
        f"summary_len={len(summary)}",
    )
    _named(
        rendered,
        "Deduped hypothesis",
        standing.hypothesis_id,
        f"statement_len={len(duplicate)}",
    )


async def test_a_failed_reasoning_validation_names_its_errors_and_ref_length(lines):
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
    from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
    from tests.unit.core.investigation.test_hypothesis_dedup import _case as dedup_case

    # A turn reference the model wrote is quoted into the validation error.
    response = InvestigationResponse_Diagnosis.model_validate(
        {
            "agent_response": "The symptom is confirmed.",
            "evidence_trail": {
                "milestone_justifications": {"symptom_verified": "seen in the log"},
                "evidence_analyzed": [f"turn_{MARKER}", f"turn_99_{MARKER}"],
            },
            "state_updates": {"milestones": {"symptom_verified": True}},
        }
    )
    case = dedup_case(symptom_verified=False)
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    await engine.responses.process_response_structured(case, "it fails", response)
    _named(
        lines(),
        "Reasoning validation failed",
        case.case_id,
        "offending ['symptom_verified']",
        f"Invalid turn reference format: <ref_len={len(MARKER) + 5}>. Expected",
        f"Invalid turn reference <ref_len={len(MARKER) + 8}>: turn number must be",
        "Cannot complete milestones when no actionable evidence",
    )
