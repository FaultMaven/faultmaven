"""A model-content refusal reaches the boundary of the work that made it (#1998 P3).

The model boundary refuses (``RawModelContentRefused``) a call whose content the
redaction module did not mint, before any network I/O. A refusal is a
programming error, never a reason to fall back: it must reach the request or
job boundary of the work that made it. A broad ``except`` on the way that turned
it into a fallback — "classifier failed, default to conversation", an
extractive title, a tool error fed back to the model, a partial conversion —
would ship the defect silently. This file is the guard:

1. **The census.** Every function in ``faultmaven/`` (and ``scripts/``,
   ``alembic/``) that names the seam (``route_model_call``) or the dispatch
   (``send_to_model``) — a call, a reference, a string naming one, docstrings
   aside — is a send-site function, read from the source (the same lexical
   walk as rule S of ``test_model_dispatch_lexical_1998.py``, with its
   docstring rule). :data:`CENSUS` keys on those functions and lists the
   caller paths that reach each one; the census test fails when the two sets
   differ, so a new send site cannot land without a case.
2. **The propagation table.** Each caller path is driven from its boundary
   entry — the turn (``InvestigationService.prepare_turn``), a route function,
   a detached job — with the REAL seam made to refuse the census function's
   own sends (``dispatch.model_call_violations`` patched, and only for calls
   whose caller is that function; ``router.py``'s own binding is left alone,
   so a routed call still reaches the dispatch). The refusal must arrive at the
   boundary: raised out of it; or, through a typed wrap
   (``ServiceException(...) from e``), as a failure whose ``__cause__`` chain
   holds it; or, for detached work, in the job's terminal record. Positive
   control: the refusal that arrives was raised by the real seam or dispatch,
   called from the census function, and names its target. Negative control:
   the same path, unpatched, with the minted input production builds,
   succeeds.
3. **The lexical complement** (R4): inside each census function, a broad
   handler (``except Exception``, ``BaseException``, ``TypeError``, bare)
   must come after an ``except RawModelContentRefused: raise`` in the same
   handler list. A handler in a CALLER is the propagation table's to catch.

One path is pinned as a known gap (strict ``xfail``), by ruling: the turn
route's post-commit auto-title (``_auto_title_case_if_default``) still
swallows a refusal, because re-raising it there answers a 500 for a turn that
already committed, and the turn's ``Idempotency-Key`` is optional, so an
unkeyed client retry would run the turn twice. Its ``xfail`` flips to a
failure the day that handler re-raises.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import (
    RawModelContentRefused,
    ServiceException,
    walk_cause_chain,
)
from faultmaven.infrastructure.llm import dispatch
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    StopReason,
    ToolCall,
)
from faultmaven.infrastructure.security.model_content import RAW, ContentViolation
from tests.model_content import FakeProvider, disabled_redaction, fake_response
from tests.unit.architecture.test_model_dispatch_lexical_1998 import (
    _docstrings,
    _files,
    _names,
    _parse,
)
from tests.utils import sanitize_pii_pinned, sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.architecture, pytest.mark.security]

_DISPATCH = "faultmaven/infrastructure/llm/dispatch.py"
#: The names a send site spells (spelled in pieces: rule A3 of the dispatch
#: lexical test reads the dispatcher's name as a string).
_SEAM = "route_model" + "_call"
_DISPATCHER = "send_to" + "_model"
_SEND_NAMES = frozenset({_SEAM, _DISPATCHER})
#: The handler types a refusal (a ``TypeError``) would be caught by.
_BROAD = frozenset({"Exception", "BaseException", "TypeError"})
_REFUSAL = "RawModelContentRefused"


@pytest.fixture(autouse=True)
def _no_network():
    """No connection leaves this file's tests, by any path."""
    with sockets_refused():
        yield


# ---------------------------------------------------------------------------
# 1. The census: every send-site function, read from the source
# ---------------------------------------------------------------------------


def _refers(node: ast.AST, docstrings: set) -> bool:
    """Whether ``node`` names the seam or the dispatch."""
    if isinstance(node, ast.Name):
        return node.id in _SEND_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _SEND_NAMES
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node in docstrings and len(node.value.split()) > 1:
            return False
        return bool(_names(node.value, _SEND_NAMES))
    return False


def send_sites(source: str, path: str) -> Dict[str, ast.AST]:
    """``{"path::Qual.name": def node}`` for every function in ``source`` that
    names the seam or the dispatch; ``path::<module>`` for a module-level
    reference. Imports bind the names and are not references."""
    tree = _parse(source)
    docstrings = _docstrings(tree)
    found: Dict[str, ast.AST] = {}

    def visit(node: ast.AST, chain: Tuple[str, ...], owner: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child if not isinstance(child, ast.ClassDef) else owner
                visit(child, chain + (child.name,), inner)
                continue
            if _refers(child, docstrings):
                key = f"{path}::{'.'.join(chain) or '<module>'}"
                found.setdefault(key, owner)
            visit(child, chain, owner)

    visit(tree, (), tree)
    return found


def _scan() -> Dict[str, ast.AST]:
    found: Dict[str, ast.AST] = {}
    for rel, path in _files():
        if rel == _DISPATCH:
            continue
        found.update(send_sites(path.read_text(encoding="utf-8"), rel))
    return found


# ---------------------------------------------------------------------------
# 3. The lexical complement: a census function's own broad handlers
# ---------------------------------------------------------------------------


def _handler_names(handler: ast.ExceptHandler) -> set:
    if handler.type is None:
        return {"<bare>"}
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {
        e.attr if isinstance(e, ast.Attribute) else getattr(e, "id", "?") for e in elts
    }


def _is_refusal_reraise(handler: ast.ExceptHandler) -> bool:
    return (
        _handler_names(handler) == {_REFUSAL}
        and len(handler.body) == 1
        and isinstance(handler.body[0], ast.Raise)
        and handler.body[0].exc is None
    )


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """Every node of ``function``'s own body, nested defs and lambdas aside."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def unguarded_handlers(function: ast.AST) -> List[int]:
    """Lines of the broad handlers in ``function``'s own ``try`` statements
    that no earlier ``except RawModelContentRefused: raise`` in the same list
    guards."""
    found: List[int] = []
    for node in _own_nodes(function):
        handlers = getattr(node, "handlers", None)
        if not isinstance(node, ast.Try) and not (
            sys.version_info >= (3, 11) and isinstance(node, ast.TryStar)
        ):
            continue
        guarded = False
        for handler in handlers:
            if _is_refusal_reraise(handler):
                guarded = True
            elif not guarded and _handler_names(handler) & (_BROAD | {"<bare>"}):
                found.append(handler.lineno)
    return found


# ---------------------------------------------------------------------------
# The selective refusal and the controls
# ---------------------------------------------------------------------------


def _qualname(code) -> str:
    return code.co_qualname.replace(".<locals>", "")


@contextlib.contextmanager
def seam_refuses_at(site: str) -> Iterator[None]:
    """The REAL seam and dispatch refuse the sends ``site`` makes, and only
    those: ``dispatch.model_call_violations`` reports a raw prompt when its
    caller (``route_model_call`` or ``send_to_model``) was called from the
    census function. Every other send on the path is checked as shipped."""
    path, qualname = site.split("::")
    real = dispatch.model_call_violations

    def selective(prompt: Any, messages: Any, kwargs: Dict[str, Any]):
        caller = sys._getframe(2)
        if (
            caller.f_code.co_filename.endswith(path)
            and _qualname(caller.f_code) == qualname
        ):
            return [ContentViolation("prompt", RAW, "prompt")]
        return real(prompt, messages, kwargs)

    with patch.object(dispatch, "model_call_violations", selective):
        yield


def assert_from_the_real_seam(
    refusal: BaseException, site: str, sent_by: str = _SEAM
) -> None:
    """The positive control (R5): the refusal was raised by the real seam or
    dispatch (``sent_by``), called from the census function, and names its
    target: ``router`` for the seam, the provider's label for the dispatch."""
    assert isinstance(refusal, RawModelContentRefused), refusal
    frames = traceback.extract_tb(refusal.__traceback__)
    path, qualname = site.split("::")
    assert len(frames) >= 3, frames
    refuse, sender, caller = frames[-1], frames[-2], frames[-3]
    assert (refuse.filename.endswith(_DISPATCH), refuse.name) == (True, "refuse")
    assert sender.filename.endswith(_DISPATCH)
    assert sender.name == sent_by, (sender.name, sent_by)
    assert caller.filename.endswith(path), (caller.filename, path)
    assert caller.name == qualname.rsplit(".", 1)[-1], (caller.name, qualname)
    target = str(refusal).split(":", 1)[0]
    assert (target == "router") is (sent_by == _SEAM), str(refusal)
    assert str(refusal).endswith("not minted by the redaction module: prompt raw")


def _refusal_in(exc: BaseException) -> BaseException:
    """The refusal a typed wrap carries on its ``__cause__`` chain."""
    found = [c for c in walk_cause_chain(exc) if isinstance(c, RawModelContentRefused)]
    assert found, f"no refusal on the cause chain of {exc!r}"
    return found[0]


@dataclass(frozen=True)
class Chain:
    """One caller path of a census function, driven from its boundary.

    ``run(refuse)`` drives the work. Refusing, it returns the refusal as the
    boundary has it (raised out of it, on a typed wrap's chain, or in a job's
    terminal record); not refusing, it asserts the boundary's success and
    returns ``None``.
    """

    site: str
    path: str
    run: Callable[[bool], Awaitable[Optional[BaseException]]]
    #: The send the census function makes on this path: the seam, or the
    #: dispatch (a dedicated provider, the connection check, the registry).
    sender: str = _SEAM


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------

USER = "user_1998p3"
ENTERPRISE = "ent_1998p3"


def _llm(content: str = "ok", **fields: Any) -> LLMResponse:
    return fake_response(content, **fields)


class _TitleSanitizer:
    """What the title's disclosure gate (#1967) runs: nothing to redact."""

    def sanitize_text_with_registry(self, text, registry):
        return text

    def apply_regex_redaction(self, text, registry):
        return text


def _router_double(content: str) -> SimpleNamespace:
    """A router double: ``route`` answers ``content``."""
    return SimpleNamespace(route=AsyncMock(return_value=_llm(content)))


def _real_router(provider: BaseLLMProvider, monkeypatch_context) -> Any:
    """A real ``LLMRouter`` over a real registry holding ``provider``."""
    from faultmaven.infrastructure.llm.providers.registry import (
        ProviderRegistry,
        ProviderState,
    )
    from faultmaven.infrastructure.llm.router import LLMRouter

    name = provider.provider_name
    registry = ProviderRegistry(settings=None)
    registry._providers = {name: provider}
    registry._fallback_chain = [name]
    registry._provider_states = {name: ProviderState(name=name)}
    registry._initialized = True
    monkeypatch_context.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    monkeypatch_context.setattr(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    )
    return LLMRouter()


# ---------------------------------------------------------------------------
# Route chains: the case title, the connection check, the routed registry
# ---------------------------------------------------------------------------

_TITLE = "Login Failures On The Auth Service"


def _titled_case() -> SimpleNamespace:
    return SimpleNamespace(
        case_id="case_1998p3title",
        title="Case-261016-1",
        description="",
        is_terminal=False,
        inquiry=SimpleNamespace(
            proposed_problem_statement="Login failures on the auth service",
            problem_statement_confirmed=True,
        ),
        evidence=[],
        uploaded_files=[],
    )


def _title_case_service(case: SimpleNamespace) -> SimpleNamespace:
    """A case service that stores a title as written, so the route's
    read-back after the write sees it."""

    async def update_case(case_id, updates, user_id):
        case.title = updates["title"]
        return True

    return SimpleNamespace(
        get_case=AsyncMock(return_value=case),
        get_case_conversation_context=AsyncMock(
            return_value="User: login failures on the auth service " * 40
        ),
        update_case=AsyncMock(side_effect=update_case),
    )


async def _title_route(refuse: bool, llm_provider: Any) -> Optional[BaseException]:
    """``POST /cases/{id}/title``: the route function itself."""
    from starlette.requests import Request
    from starlette.responses import Response

    from faultmaven.modules.auth.contracts import UserDTO
    from faultmaven.modules.case.api import title_generation
    from faultmaven.modules.case.api.routes.cases import generate_case_title

    case = _titled_case()
    case_service = _title_case_service(case)
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_provider=llm_provider,
            sanitizer=_TitleSanitizer(),
            redis_client=object(),
        )
    )
    request = Request({"type": "http", "app": app, "headers": []})
    user = UserDTO(
        user_id=USER,
        username="u",
        email="u@example.com",
        display_name="U",
        is_active=True,
    )
    call = generate_case_title(
        case.case_id,
        request,
        Response(),
        request_body=None,
        force=False,
        case_service=case_service,
        current_user=user,
    )
    with patch.object(
        title_generation,
        "load_case_redaction",
        new=AsyncMock(return_value=disabled_redaction(case.case_id)),
    ):
        if refuse:
            with pytest.raises(RawModelContentRefused) as raised:
                await call
            case_service.update_case.assert_not_called()
            return raised.value
        response = await call
    assert response.title == _TITLE
    return None


async def _title_route_with_a_router_double(refuse: bool):
    return await _title_route(refuse, _router_double(_TITLE))


async def _title_route_through_the_real_router(refuse: bool):
    with pytest.MonkeyPatch.context() as scoped:
        router = _real_router(FakeProvider([_llm(_TITLE)], name="fake"), scoped)
        with sanitize_pii_pinned(False):
            return await _title_route(refuse, router)


async def _auto_title_after_a_turn(refuse: bool) -> Optional[BaseException]:
    """The turn route's post-commit auto-title (R2 stop): the boundary is the
    turn route, after the commit. A refusal that escapes the auto-title is
    returned; today none escapes (the handler logs a WARNING and swallows it),
    so the refusal case fails, as its strict ``xfail`` expects, and the day
    the handler re-raises it passes and the ``xfail`` turns red."""
    from faultmaven.modules.case.api import title_generation

    case = _titled_case()
    case_service = _title_case_service(case)
    try:
        with patch.object(
            title_generation,
            "load_case_redaction",
            new=AsyncMock(return_value=disabled_redaction(case.case_id)),
        ):
            await title_generation._auto_title_case_if_default(
                case_id=case.case_id,
                user_id=USER,
                case_service=case_service,
                llm_provider=_router_double(_TITLE),
                sanitizer=_TitleSanitizer(),
                redis_client=object(),
            )
    except RawModelContentRefused as escaped:
        return escaped
    if refuse:
        return None
    case_service.update_case.assert_awaited()
    assert case.title == _TITLE
    return None


async def _connection_check(refuse: bool) -> Optional[BaseException]:
    """``POST /admin/config/llm/test-connection``: the route function itself."""
    from faultmaven.api.models import LLMConnectionTestRequest
    from faultmaven.api.routes.admin_config import check_llm_connection
    from faultmaven.infrastructure.llm.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings=None)
    provider = FakeProvider([_llm("hello")], name="openai")
    registry._providers = {"openai": provider}
    registry._initialized = True
    call = check_llm_connection(
        LLMConnectionTestRequest(provider="openai"),
        current_user=SimpleNamespace(user_id=USER),
        llm_provider=SimpleNamespace(registry=registry),
    )
    if refuse:
        with pytest.raises(RawModelContentRefused) as raised:
            await call
        assert provider.calls == []
        return raised.value
    response = await call
    assert response.connected is True
    return None


# ---------------------------------------------------------------------------
# Conversion chains: the document route (three sends), the detached case job
# ---------------------------------------------------------------------------


def _conversion_service(router: Any) -> Any:
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from tests.unit.modules.knowledge.test_conversion_model_boundary_redaction_1901 import (  # noqa: E501
        _service,
    )

    return _service(router, DataSanitizer())


@contextlib.contextmanager
def _conversion_data_dir(tmp: Path) -> Iterator[None]:
    from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
        ConversionService,
    )

    with patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp / "knowledge"),
    ):
        yield


async def _convert_route(refuse: bool, modes: int) -> Optional[BaseException]:
    """``POST /knowledge/convert``: the route function itself."""
    import tempfile

    from starlette.datastructures import Headers, UploadFile

    from faultmaven.modules.knowledge.api.conversion_routes import convert_document
    from tests.unit.modules.knowledge.test_conversion_model_boundary_redaction_1901 import (  # noqa: E501
        _DOCUMENT,
        RecordingRouter,
    )

    router = RecordingRouter(modes=modes)
    body = _DOCUMENT.encode()
    upload = UploadFile(
        file=io.BytesIO(body),
        filename="replica-notes.md",
        size=len(body),
        headers=Headers({"content-type": "text/markdown"}),
    )
    user = SimpleNamespace(
        user_id=USER, enterprise_id=None, is_platform_admin=lambda: False
    )
    with (
        tempfile.TemporaryDirectory() as tmp,
        _conversion_data_dir(Path(tmp)),
        sanitize_pii_pinned(False),
    ):
        call = convert_document(
            file=upload,
            scope="personal",
            team_id=None,
            service=_conversion_service(router),
            current_user=user,
        )
        if refuse:
            with pytest.raises(RawModelContentRefused) as raised:
                await call
            return raised.value
        result = await call
    assert result["status"] == "completed", result
    assert len(result["drafts"]) == modes
    return None


async def _case_conversion_job(refuse: bool) -> Optional[BaseException]:
    """The detached case→runbook job a resolved turn starts: its terminal
    record is the notice it writes to the transcript and the ERROR line that
    names the class."""
    import asyncio
    import tempfile

    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
    from tests.unit.modules.knowledge.test_conversion_model_boundary_redaction_1901 import (  # noqa: E501
        RecordingRouter,
        _case_request,
    )

    case = SimpleNamespace(
        case_id=_case_request().case_id, messages=[], current_turn=4, message_count=0
    )
    repo = MagicMock()
    repo.get = AsyncMock(return_value=case)
    repo.save = AsyncMock()
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    gate = asyncio.get_running_loop().create_future()
    gate.set_result(None)
    records: List[logging.LogRecord] = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = records.append  # type: ignore[method-assign]
    job_log = logging.getLogger(
        "faultmaven.core.investigation.milestone_engine.runbook_creation"
    )
    job_log.addHandler(handler)
    try:
        with (
            tempfile.TemporaryDirectory() as tmp,
            _conversion_data_dir(Path(tmp)),
            sanitize_pii_pinned(False),
            patch(
                "faultmaven.core.investigation.milestone_engine.runbook_creation."
                "append_message_row",
                side_effect=lambda c, kind, content, **_: c.messages.append(
                    {"kind": kind, "content": content}
                ),
            ),
        ):
            await engine.runbooks._run_runbook_conversion(
                _conversion_service(RecordingRouter()),
                _case_request(),
                USER,
                ENTERPRISE,
                committed=gate,
            )
    finally:
        job_log.removeHandler(handler)
    [notice] = case.messages
    if refuse:
        assert notice["content"].startswith("Runbook generation failed"), notice
        [record] = [r for r in records if r.exc_info]
        return record.exc_info[1]
    assert "is ready" in notice["content"], notice
    return None


# ---------------------------------------------------------------------------
# Turn chains: the real service, engine, generator and tools, a scripted model
# ---------------------------------------------------------------------------

#: The user's message: an aside the triage has to read (#1328's shape).
_QUERY = (
    "Forget the server for a second. Can you write a haiku about my cat, and "
    "tell me the capital of Australia?"
)
_FILE_ID = "file_fa0e00001998"
_KB_TOOL, _EVIDENCE_TOOL, _DEEP_TOOL = "kb_qa", "case_evidence_search", "deep_analysis"


class _TurnModel:
    """The model behind every send a turn makes, answering each by its kind.

    ``verdict`` is the out-of-band triage's ("1": incident work, "2": an
    aside); ``tool`` is the tool the tool loop's first call asks for. The
    kinds are told apart by what each site sends, as no two share a shape.
    """

    def __init__(self, verdict: str = "1", tool: Optional[str] = None) -> None:
        self.verdict = verdict
        self.tool = tool

    def answer(self, kwargs: Dict[str, Any]) -> LLMResponse:
        from faultmaven.modules.agent.domain.services.out_of_band import (
            ANSWER_MAX_TOKENS,
            TRIAGE_MAX_TOKENS,
        )

        messages = kwargs.get("messages") or []
        text = " ".join(str(m.get("content") or "") for m in messages)
        if kwargs.get("tools"):
            return self._tool_loop(kwargs, messages)
        if "The assistant just offered these choices" in text:
            return _llm("none")
        if kwargs.get("max_tokens") == TRIAGE_MAX_TOKENS:
            return _llm(self.verdict)
        if kwargs.get("max_tokens") == ANSWER_MAX_TOKENS:
            return _llm("Canberra. Tell me what is going wrong when ready.")
        if messages and messages[0].get("role") == "system":
            return _llm("Restart the replica after failover.")  # KB synthesis
        if messages:
            return _llm("The auth failures repeat every minute.")  # tier 2
        return _llm('{"agent_response": "Noted.", "state_updates": {}}')

    def _tool_loop(self, kwargs, messages) -> LLMResponse:
        names = [t["function"]["name"] for t in kwargs["tools"]]
        if (
            self.tool
            and self.tool in names
            and not any(m.get("role") == "tool" for m in messages)
        ):
            args = {
                _KB_TOOL: {"question": "what fixes replica refusals?"},
                _EVIDENCE_TOOL: {"question": "when did auth fail?"},
                _DEEP_TOOL: {"evidence_id": _FILE_ID, "query": "auth failure"},
            }[self.tool]
            return _llm(
                "",
                tool_calls=[
                    ToolCall(
                        id="call_1998p3",
                        type="function",
                        function={"name": self.tool, "arguments": json.dumps(args)},
                    )
                ],
            )
        [schema] = [n for n in names if n.startswith("InvestigationResponse")]
        return _llm(
            "",
            tool_calls=[
                ToolCall(
                    id="call_schema",
                    type="function",
                    function={
                        "name": schema,
                        "arguments": json.dumps(
                            {"agent_response": "Noted.", "state_updates": {}}
                        ),
                    },
                )
            ],
        )


class _TurnRouter:
    """The router the turn's sends reach (the seam has passed them)."""

    provider_name = "openai"

    def __init__(self, model: _TurnModel) -> None:
        self.model = model
        self.config = SimpleNamespace(default_model="gpt-test")

    async def route(self, **kwargs):
        return self.model.answer(kwargs)

    def supports_tool_calling(self, model=None) -> bool:
        return True

    def get_structured_output_strategy(self, schema):
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            create_strategy_for_capability,
        )

        return create_strategy_for_capability(
            StructuredOutputCapability.BEST_EFFORT, schema
        )


class _DAProvider(BaseLLMProvider):
    """A dedicated DA provider (``DA_PROVIDER``): the tool loop sends to it
    directly, through ``send_to_model``."""

    LABEL = "dedicated"

    def __init__(self, model: _TurnModel) -> None:
        super().__init__(
            ProviderConfig(
                name="dedicated",
                api_key="test-key-not-real",
                models=["da-model"],
                default_model="da-model",
            )
        )
        self.model = model

    @property
    def provider_name(self) -> str:
        return "dedicated"

    def is_available(self) -> bool:
        return True

    def get_supported_models(self) -> List[str]:
        return ["da-model"]

    def supports_tool_calling(self, model=None) -> bool:
        return True

    async def _generate(self, prompt=None, model=None, max_tokens=1000, **kwargs):
        return self.model.answer({"max_tokens": max_tokens, **kwargs})


class _Chunks:
    """A vector store with one strong chunk."""

    async def search(self, **_):
        return [
            {
                "content": "After failover the replica refuses connections.",
                "metadata": {"source": "replica.md", "title": "Replica refusal"},
                "score": 0.95,
            }
        ]


class _Repository:
    """A case repository as a database behaves: a save stores a snapshot."""

    def __init__(self, case) -> None:
        self.rows = {case.case_id: case.model_copy(deep=True)}

    async def get(self, case_id: str, *args, **kwargs):
        row = self.rows.get(case_id)
        return row.model_copy(deep=True) if row is not None else None

    async def save(self, case, *, reports=(), receipt=None):
        self.rows[case.case_id] = case.model_copy(deep=True)
        return case


def _investigating_case(*, suggestions: bool, material: bool):
    from faultmaven.modules.case.contracts import CaseState, InquiryData, UploadedFile
    from faultmaven.modules.case.domain.models.case import Case
    from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

    now = datetime.now(timezone.utc)
    case = Case(
        case_id="case_1998a3000001",
        title="Nightly OOM kills of postgres",
        description="postgres is OOM-killed nightly",
        user_id=USER,
        enterprise_id=ENTERPRISE,
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=now,
            proposed_problem_statement="Nightly OOM kills",
        ),
        current_turn=1,
        turn_history=[
            TurnProgress(
                turn_number=1,
                timestamp=now,
                progress_made=False,
                outcome=TurnOutcome.DATA_REQUESTED,
                user_message_summary="postgres OOM-killed nightly",
                agent_response_summary="Could you share dmesg?",
            )
        ],
        messages=[
            {"turn_number": 1, "role": "user", "content": "postgres OOM-killed"},
            {"turn_number": 1, "role": "assistant", "content": "Share dmesg?"},
        ],
    )
    if suggestions:
        case.last_suggestions = [
            {
                "label": "Share the dmesg output",
                "payload": "Here is the dmesg output",
                "intent": {"type": "conversation"},
                "offered_turn": 1,
            }
        ]
    if material:
        upload = UploadedFile(
            file_id=_FILE_ID,
            filename="auth.log",
            size_bytes=64,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_at=now,
            uploaded_by=USER,
            data_type="logs",
            storage_ref="ent/case/auth.log",
        )
        upload.structural_index = "auth failures, one a minute " * 4
        case.uploaded_files.append(upload)
    return case


async def _turn(
    refuse: bool,
    *,
    verdict: str = "1",
    tool: Optional[str] = None,
    suggestions: bool = False,
    material: bool = False,
    dedicated: bool = False,
) -> Optional[BaseException]:
    """One turn through ``InvestigationService.prepare_turn`` — the real
    service, engine, generator, tool registry and tools; only the model and
    the case store are doubles. A refusal reaches it as the turn's typed wrap
    (``ServiceException`` from the refusal); a minted turn is prepared."""
    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
    from faultmaven.core.investigation.schemas import TurnPayload
    from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service
    from faultmaven.modules.agent.domain.services.investigation_service import (
        service as service_module,
    )
    from faultmaven.modules.agent.domain.services.investigation_service.service import (  # noqa: E501
        InvestigationService,
    )
    from faultmaven.modules.agent.tools.base import AgentToolRegistry
    from faultmaven.modules.agent.tools.case_evidence_qa import AnswerFromCaseEvidence
    from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
    from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
    from faultmaven.modules.agent.tools.kb_tool_adapter import (
        CaseEvidenceQAAdapter,
        KBToolAdapter,
    )

    model = _TurnModel(verdict=verdict, tool=tool)
    router = _TurnRouter(model)
    tools = AgentToolRegistry()
    tools.register(
        KBToolAdapter(AnswerFromKB(vector_store=_Chunks(), llm_router=router))
    )
    tools.register(
        CaseEvidenceQAAdapter(
            AnswerFromCaseEvidence(vector_store=_Chunks(), llm_router=router)
        )
    )
    storage = SimpleNamespace(
        retrieve_file=AsyncMock(return_value=b"ok\nauth failure user=x\nok\n")
    )
    tools.register(DeepAnalysisTool(LocalTier2Service(router, storage)))
    case = _investigating_case(suggestions=suggestions, material=material)
    repository = _Repository(case)
    engine = MilestoneEngine(
        llm_provider=router,
        repository=repository,
        investigation_tools=tools,
        da_provider=_DAProvider(model) if dedicated else None,
        da_model="da-model" if dedicated else None,
    )
    service = InvestigationService(engine, repository)
    loader = AsyncMock(return_value=disabled_redaction(case.case_id))
    call = service.prepare_turn(
        case_id=case.case_id, user_id=USER, payload=TurnPayload(query=_QUERY)
    )
    with patch.object(service_module, "load_case_redaction", new=loader):
        if refuse:
            with pytest.raises(ServiceException) as raised:
                await call
            return _refusal_in(raised.value)
        prepared = await call
    assert prepared.response.agent_response, prepared.response
    return None


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_INTENT = "faultmaven/core/investigation/intent_resolver.py::IntentResolver._classify"
_TRIAGE = (
    "faultmaven/modules/agent/domain/services/out_of_band.py::"
    "OutOfBandTriage._classify"
)
_ASIDE = "faultmaven/modules/agent/domain/services/out_of_band.py::_one_answer_attempt"
_SINGLE_SHOT = (
    "faultmaven/core/investigation/milestone_engine/generation.py::"
    "StructuredOutputGenerator._generate_structured_output_inner.llm_operation"
)
_TOOL_LOOP = (
    "faultmaven/core/investigation/milestone_engine/generation.py::"
    "StructuredOutputGenerator._tool_augmented_generate._tool_loop_call"
)
_SYNTHESIS = (
    "faultmaven/modules/agent/tools/document_qa_tool.py::"
    "DocumentQATool.answer_question._synthesize"
)
_TIER2 = (
    "faultmaven/core/preprocessing/tier2/local_service.py::"
    "LocalTier2Service.analyze._analyze"
)
_TITLE_SITE = (
    "faultmaven/modules/case/api/title_generation.py::_generate_title_with_llm"
)
_ANALYSIS = (
    "faultmaven/modules/knowledge/domain/services/conversion_service/pipeline.py::"
    "_analyze_document._analyze"
)
_CONVERT = (
    "faultmaven/modules/knowledge/domain/services/conversion_service/service.py::"
    "ConversionService._convert_single_failure_mode._convert"
)
_DOC_TRIAGE = (
    "faultmaven/modules/knowledge/domain/services/document_preprocessor.py::"
    "DocumentPreprocessor._run_content_triage"
)
_CONNECTION_CHECK = (
    "faultmaven/infrastructure/llm/providers/registry.py::"
    "ProviderRegistry.send_connection_check"
)
_ROUTE_REQUEST = (
    "faultmaven/infrastructure/llm/providers/registry.py::"
    "ProviderRegistry.route_request"
)


def _chains() -> List[Chain]:
    from faultmaven.modules.knowledge.domain.services.conversion_service.prompts import (  # noqa: E501
        PARALLEL_THRESHOLD,
    )

    chains = [
        Chain(_INTENT, "turn", lambda r: _turn(r, suggestions=True)),
        Chain(_TRIAGE, "turn", lambda r: _turn(r)),
        Chain(_ASIDE, "turn: out-of-band answer", lambda r: _turn(r, verdict="2")),
        Chain(_SINGLE_SHOT, "turn: single-shot generation", lambda r: _turn(r)),
        Chain(
            _TOOL_LOOP,
            "turn: tool loop, router",
            lambda r: _turn(r, material=True),
        ),
        Chain(
            _TOOL_LOOP,
            "turn: tool loop, dedicated DA provider",
            lambda r: _turn(r, material=True, dedicated=True),
            sender=_DISPATCHER,
        ),
        Chain(
            _SYNTHESIS,
            "turn: tool loop -> kb_qa",
            lambda r: _turn(r, material=True, tool=_KB_TOOL),
        ),
        Chain(
            _SYNTHESIS,
            "turn: tool loop -> case_evidence_search",
            lambda r: _turn(r, material=True, tool=_EVIDENCE_TOOL),
        ),
        Chain(
            _TIER2,
            "turn: tool loop -> deep_analysis",
            lambda r: _turn(r, material=True, tool=_DEEP_TOOL),
        ),
        Chain(_TITLE_SITE, "route: POST /title", _title_route_with_a_router_double),
        Chain(
            _ROUTE_REQUEST,
            "route: POST /title, real router",
            _title_route_through_the_real_router,
            sender=_DISPATCHER,
        ),
        Chain(
            _CONNECTION_CHECK,
            "route: POST test-connection",
            _connection_check,
            sender=_DISPATCHER,
        ),
        Chain(_DOC_TRIAGE, "route: POST /convert", lambda r: _convert_route(r, 1)),
        Chain(_ANALYSIS, "route: POST /convert", lambda r: _convert_route(r, 1)),
        Chain(
            _CONVERT,
            "route: POST /convert, parallel modes (gather)",
            lambda r: _convert_route(r, 2),
        ),
        Chain(
            _CONVERT,
            "route: POST /convert, sequential modes",
            lambda r: _convert_route(r, PARALLEL_THRESHOLD),
        ),
        Chain(_CONVERT, "job: case conversion (detached)", _case_conversion_job),
    ]
    return chains


def _id(chain: Chain) -> str:
    return f"{chain.site.split('::')[1]} <- {chain.path}"


#: The R2 stop, a known gap pinned until its handler re-raises.
_AUTO_TITLE = Chain(
    _TITLE_SITE, "turn route: post-commit auto-title", _auto_title_after_a_turn
)
_R2_STOP = pytest.mark.xfail(
    strict=True,
    reason=(
        "R2 stop: re-raising answers a 500 for a committed turn, and an "
        "unkeyed retry (Idempotency-Key is optional) would run it twice"
    ),
)


CHAINS = _chains() + [_AUTO_TITLE]
#: The refusal cases; the R2 stop is the one expected to fail.
REFUSALS = [
    pytest.param(c, id=_id(c), marks=[_R2_STOP] if c is _AUTO_TITLE else [])
    for c in CHAINS
]
#: The negative controls: every path, minted, succeeds — the R2 stop's too.
CONTROLS = [pytest.param(c, id=_id(c)) for c in CHAINS]

#: The census: every send-site function and the caller paths that reach it.
CENSUS: Dict[str, Tuple[str, ...]] = {}
for _chain in CHAINS:
    CENSUS[_chain.site] = CENSUS.get(_chain.site, ()) + (_chain.path,)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_census_is_every_send_site_function():
    found = set(_scan())
    assert len(found) >= 13, f"scanned only {sorted(found)}"
    assert found == set(CENSUS), (
        "the send-site functions changed: every function that names "
        f"{_SEAM} or {_DISPATCHER} needs a propagation case here.\n"
        f"  new: {sorted(found - set(CENSUS))}\n"
        f"  gone: {sorted(set(CENSUS) - found)}"
    )


def test_every_census_function_guards_its_own_broad_handlers():
    sites = _scan()
    unguarded = {
        site: lines
        for site, function in sites.items()
        if (lines := unguarded_handlers(function))
    }
    assert not unguarded, (
        "a broad handler in a send-site function catches a refusal before "
        "`except RawModelContentRefused: raise` (#1998 P3):\n  "
        + "\n  ".join(f"{site}: lines {lines}" for site, lines in unguarded.items())
    )


@pytest.mark.parametrize("chain", REFUSALS)
async def test_a_refusal_reaches_the_boundary(chain):
    with seam_refuses_at(chain.site):
        refusal = await chain.run(True)
    assert refusal is not None, f"{chain.path}: the refusal never reached it"
    assert_from_the_real_seam(refusal, chain.site, chain.sender)


@pytest.mark.parametrize("chain", CONTROLS)
async def test_the_same_path_minted_succeeds(chain):
    """The negative control: unpatched, the input production mints passes the
    seam and the dispatch, and the boundary succeeds."""
    assert await chain.run(False) is None


# ---------------------------------------------------------------------------
# The census and the lexical rule, on planted sources
# ---------------------------------------------------------------------------

#: name -> (source, the census key it must yield in ``faultmaven/planted.py``).
PLANTED_SITES = {
    "a call": (
        "async def f(router, p):\n    return await route_model_call(router, prompt=p)\n",
        "f",
    ),
    "a method of a class": (
        "class C:\n    async def m(self, p):\n"
        "        return await dispatch.send_to_model(self.p, prompt=p)\n",
        "C.m",
    ),
    "a closure": (
        "def outer():\n    async def inner(r):\n"
        "        return await route_model_call(r)\n    return inner\n",
        "outer.inner",
    ),
    "an alias": ("def f():\n    send = send_to_model\n    return send\n", "f"),
    "a reflection by name": (
        "def f(d):\n    return getattr(d, 'route_model_call')\n",
        "f",
    ),
    "a module-level reference": ("send = route_model_call\n", "<module>"),
}

#: What names the seam without being a send site.
NOT_SITES = {
    "an import": "from faultmaven.infrastructure.llm.dispatch import route_model_call\n",
    "a docstring example": (
        'def f():\n    """Example:\n\n    >>> await route_model_call(router, prompt=p)\n'
        '    """\n'
    ),
    "a longer identifier": "def f():\n    return route_model_calls\n",
}


@pytest.mark.parametrize("name", sorted(PLANTED_SITES))
def test_a_planted_send_site_joins_the_census(name):
    """The census-set test's teeth: a new send-site function yields a key the
    table does not hold."""
    source, qualname = PLANTED_SITES[name]
    found = send_sites(source, "faultmaven/planted.py")
    assert set(found) == {f"faultmaven/planted.py::{qualname}"}, found
    assert not set(found) & set(CENSUS)


@pytest.mark.parametrize("name", sorted(NOT_SITES))
def test_what_is_not_a_send_site(name):
    assert send_sites(NOT_SITES[name], "faultmaven/planted.py") == {}


def _only_function(source: str) -> ast.AST:
    [function] = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "site"
    ]
    return function


_GUARD = "    except RawModelContentRefused:\n        raise\n"

#: name -> a send-site function whose own handlers let a refusal be caught.
UNGUARDED = {
    "except Exception": (
        "def site():\n    try:\n        send()\n    except Exception:\n"
        "        return None\n"
    ),
    "a bare except": "def site():\n    try:\n        send()\n    except:\n        pass\n",
    "except TypeError": (
        "def site():\n    try:\n        send()\n    except TypeError:\n        pass\n"
    ),
    "except BaseException": (
        "def site():\n    try:\n        send()\n    except BaseException:\n"
        "        pass\n"
    ),
    "a tuple holding Exception": (
        "def site():\n    try:\n        send()\n"
        "    except (ValueError, Exception):\n        pass\n"
    ),
    "the guard after the broad handler": (
        "def site():\n    try:\n        send()\n    except Exception:\n"
        "        pass\n" + _GUARD
    ),
    "a guard that does more than re-raise": (
        "def site():\n    try:\n        send()\n"
        "    except RawModelContentRefused:\n        log()\n        raise\n"
        "    except Exception:\n        pass\n"
    ),
    "a handler away from the send": (
        "def site():\n    try:\n        settings()\n    except Exception:\n"
        "        pass\n    send()\n"
    ),
}

#: name -> a send-site function the rule leaves alone.
GUARDED = {
    "the guard first": (
        "def site():\n    try:\n        send()\n" + _GUARD + "    except Exception:\n"
        "        pass\n"
    ),
    "the guard earlier in the list": (
        "def site():\n    try:\n        send()\n"
        + _GUARD
        + "    except ValueError:\n        pass\n    except Exception:\n        pass\n"
    ),
    "only narrow handlers": (
        "def site():\n    try:\n        send()\n    except ValueError:\n        pass\n"
    ),
    "a nested function's handler is its own": (
        "def site():\n    def helper():\n        try:\n            x()\n"
        "        except Exception:\n            pass\n    send()\n"
    ),
}


@pytest.mark.parametrize("name", sorted(UNGUARDED))
def test_the_lexical_rule_flags_an_unguarded_broad_handler(name):
    assert unguarded_handlers(_only_function(UNGUARDED[name])), name


@pytest.mark.parametrize("name", sorted(GUARDED))
def test_the_lexical_rule_allows_a_guarded_or_narrow_handler(name):
    assert unguarded_handlers(_only_function(GUARDED[name])) == [], name
