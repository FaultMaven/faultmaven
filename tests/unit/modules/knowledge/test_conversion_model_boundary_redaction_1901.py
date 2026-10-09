"""#1901 — conversion redacts what it sends a model, as an investigation turn does.

Case→runbook conversion sent the case (title, problem, root cause, solutions,
evidence) to the model with no redaction of its own. The only pass on the path
was ``LLMRouter._sanitize_if_needed`` over the router's private
``DataSanitizer()`` — a property of the default router, which
``LLM_ROUTER_CLASS`` can substitute. The document path's three model calls
(triage, analysis, per-failure-mode conversion) had the same shape.

Every model call the conversion service makes now redacts its outbound messages
at the engine layer, with the DI sanitizer, under ``should_redact`` — the
decision the investigation path makes. Pinned here with a recording router (no
live model call):

1. **Redacts exactly when the investigation path does**, for the case path and
   each document call: with ``SANITIZE_PII`` on and a sanitizer configured, no
   PII value reaches the router and the investigation path's placeholder does;
   otherwise the text goes out as written.
2. **The truncation retry resends the redacted text.**
3. **Never reversed.** The runbook is de-identified knowledge: the
   placeholders the model wrote are what is persisted, as on the extraction
   path.
4. **Fail closed.** A redaction that is required and cannot run stops the
   conversion with its class intact and nothing sent — at each call site, on
   the parallel and the sequential failure-mode branch.
5. **Structurally**, every router call in the conversion code is handed
   messages that went through a model-boundary redaction.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.security.case_redaction import (
    CaseRedactionContext,
    should_redact,
)
from faultmaven.infrastructure.security.redaction import (
    DataSanitizer,
    RedactionUnavailableError,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    CaseConversionRequest,
    ConversionStatus,
)
from faultmaven.modules.knowledge.domain.services import document_preprocessor
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    service as conversion_service_module,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.prompts import (
    PARALLEL_THRESHOLD,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from tests.runbook_samples import valid_runbook
from tests.utils import sanitize_pii_pinned

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

CASE_ID = "case_aa0000001901"
PII_IP = "10.20.30.40"
PII_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PRODUCED_TITLE = "PostgreSQL Replica Connection Refusal"

#: Which call a recorded request is, by the temperature each site sends.
STAGES = {0.1: "triage", 0.2: "analysis", 0.3: "conversion"}

#: A marker each call site's user message carries, so a sanitizer can refuse
#: at exactly one of them.
STAGE_MARKERS = {
    "triage": "Classify this document excerpt",
    "analysis": "Analyze this document",
    "conversion": "Convert the following source material",
}

_PLACEHOLDER_OR_IP = re.compile(r"<IP_ADDRESS_[0-9a-f]+>|" + re.escape(PII_IP))

# Technical enough for the heuristic gate, long enough for the length gate, not
# a runbook — and the IP early, inside the triage sample. Stage 3's own regex
# pass does not cover an IP, so this is the engine layer's to redact.
_DOCUMENT = (
    "# Payments replica notes\n\n"
    f"The replica at {PII_IP} refuses connections after failover. "
    + (
        "The payments API returns an error when the upstream times out. "
        "Operators run kubectl rollout restart deployment/payments to recover. "
    )
    * 4
)


def _response(content: str, stop: StopReason = StopReason.STOP) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="recording",
        model="test-model",
        tokens_used=100,
        response_time_ms=1,
        stop_reason=stop,
    )


def _analysis_json(modes: int) -> str:
    return json.dumps(
        {
            "is_actionable": True,
            "failure_modes": [
                {
                    "id": f"pg-replica-refusal-{i}",
                    "title": f"PostgreSQL Replica Refusal Variant {i}",
                    "domain": "database",
                    "service": "postgresql",
                    # Distinct per mode, or the job collapses them into one
                    # (``_partition_failure_modes``, key 1).
                    "symptom_class": [f"connection_refused_{i}"],
                    "severity": "high",
                    "symptoms_summary": "Replica refuses connections",
                    "resolution_summary": "Restart the replica",
                }
                for i in range(modes)
            ],
            "source_assessment": {
                "content_type": "troubleshooting_guide",
                "actionability_rating": "high",
                "missing_information": [],
            },
        }
    )


class RecordingRouter:
    """Records every request; answers each by its stage.

    The conversion answer echoes the IP token it was sent (the raw value or its
    placeholder) into the runbook it writes, as a model writes what it reads —
    which is what lets the test see whether anything reverses it on the way to
    the draft.
    """

    def __init__(
        self,
        modes: int = 1,
        truncate_first_conversion: bool = False,
        fail_conversion: int | None = None,
    ):
        self.calls: list[tuple[str, list[dict]]] = []
        self._modes = modes
        self._truncate_next_conversion = truncate_first_conversion
        #: The 1-based generation call that fails AT THE ROUTER (a provider
        #: error), which is per-mode and must stay so.
        self._fail_conversion = fail_conversion

    async def route(self, **kwargs) -> LLMResponse:
        assert "prompt" not in kwargs, "every conversion call is chat-shaped"
        stage = STAGES[kwargs["temperature"]]
        messages = kwargs["messages"]
        self.calls.append((stage, messages))
        if stage == "triage":
            return _response(
                '{"is_actionable": true, "confidence": 0.9, "reason": "guide"}'
            )
        if stage == "analysis":
            return _response(_analysis_json(self._modes))
        if self._fail_conversion == len(self.sent_text("conversion")) // 2:
            raise RuntimeError("provider unavailable")
        if self._truncate_next_conversion:
            self._truncate_next_conversion = False
            return _response("---\ntitle: cut", StopReason.MAX_TOKENS)
        user = next(m["content"] for m in messages if m["role"] == "user")
        seen = _PLACEHOLDER_OR_IP.search(user)
        echo = f"\nThe replica at {seen.group(0)} refused.\n" if seen else ""
        return _response(valid_runbook(PRODUCED_TITLE) + echo)

    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]

    def sent_text(self, stage: str | None = None) -> list[str]:
        return [
            m["content"]
            for s, messages in self.calls
            if stage is None or s == stage
            for m in messages
        ]


class _RefusingNthGeneration(DataSanitizer):
    """Presidio fails on the Nth failure mode's generation request only.

    Counts the redactions of a generation user message (one per mode); every
    other text is redacted normally. With the old shape — each mode redacted
    just before it was sent — modes 1..N-1 had already gone out by the time
    this refused.
    """

    def __init__(self, n: int):
        super().__init__()
        self._n = n
        self._seen = 0

    def sanitize_text_with_registry(self, text, entity_registry):
        if STAGE_MARKERS["conversion"] in text:
            self._seen += 1
            if self._seen == self._n:
                raise RedactionUnavailableError("Presidio analyzer failed")
        return super().sanitize_text_with_registry(text, entity_registry)


class _RefusingAt(DataSanitizer):
    """Presidio is down under fail-closed — at one call site, or at all."""

    def __init__(self, stage: str | None = None):
        super().__init__()
        self._marker = STAGE_MARKERS[stage] if stage else ""

    def sanitize_text_with_registry(self, text, entity_registry):
        if self._marker in text:
            raise RedactionUnavailableError("Presidio analyzer failed")
        return super().sanitize_text_with_registry(text, entity_registry)


@pytest.fixture(params=[False, True], ids=["sanitize_pii-off", "sanitize_pii-on"])
def redaction_arm(request):
    """Both values of the flag the investigation path keys on, pinned."""
    with sanitize_pii_pinned(request.param):
        yield request.param


@pytest.fixture
def redaction_on():
    with sanitize_pii_pinned(True):
        yield


@pytest.fixture(scope="module")
def sanitizer():
    return DataSanitizer()


_SETTINGS = SimpleNamespace(
    llm=SimpleNamespace(
        get_knowledge_model=lambda: "test-model",
        get_classifier_model=lambda: "classifier",
        explicit_role_provider=lambda role: None,
    )
)


def _service(router, sanitizer) -> ConversionService:
    return ConversionService(
        llm_router=router,
        settings=_SETTINGS,
        db_session_factory=None,
        knowledge_service=None,
        sanitizer=sanitizer,
    )


def _investigation_path(scope_id: str, sanitizer) -> CaseRedactionContext:
    """The context the milestone engine builds for a turn, built as it builds
    it (``turn_generation._generate_turn_response``)."""
    return CaseRedactionContext(
        case_id=scope_id, sanitizer=sanitizer, enabled=should_redact(sanitizer)
    )


def _case_request() -> CaseConversionRequest:
    return CaseConversionRequest(
        case_id=CASE_ID,
        title="Replica refuses connections",
        description=f"the replica at {PII_IP} refuses connections",
        root_cause=f"the app authenticates with {PII_AWS_KEY}, revoked at failover",
        evidence_summary=f"pg logs on {PII_IP}: FATAL: password authentication failed",
    )


async def _convert_case(tmp_path, router, sanitizer):
    service = _service(router, sanitizer)
    with patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp_path / "knowledge"),
    ):
        return await service.convert_from_case(
            _case_request(), user_id="u_1901", enterprise_id=None
        )


async def _convert_document(tmp_path, router, sanitizer):
    source = tmp_path / "replica-notes.md"
    source.write_text(_DOCUMENT)
    service = _service(router, sanitizer)
    with patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp_path / "knowledge"),
    ):
        return await service.convert_document(
            file_path=source,
            content_type="text/markdown",
            original_filename="replica-notes.md",
            scope="personal",
            user_id="u_1901",
            enterprise_id=None,
        )


# ---------------------------------------------------------------------------
# 1. Redacts exactly when the investigation path does
# ---------------------------------------------------------------------------


class TestTheCasePath:
    @pytest.mark.parametrize("has_sanitizer", [True, False])
    async def test_it_redacts_exactly_when_the_investigation_path_does(
        self, tmp_path, redaction_arm, has_sanitizer, sanitizer
    ):
        handed = sanitizer if has_sanitizer else None
        router = RecordingRouter()

        response = await _convert_case(tmp_path, router, handed)

        assert response.status == ConversionStatus.COMPLETED
        assert router.stages() == ["conversion"], "control: the model was called"
        sent = router.sent_text()
        engine = _investigation_path(CASE_ID, handed)
        assert engine.enabled is (redaction_arm and has_sanitizer)
        for value in (PII_IP, PII_AWS_KEY):
            if engine.enabled:
                placeholder = await engine.asanitize(value)
                assert placeholder != value, f"control: the engine redacts {value}"
                assert not any(value in text for text in sent), value
                # The SAME redaction: the investigation path's placeholder is
                # what the conversion model sees in the value's place.
                assert any(placeholder in text for text in sent), value
            else:
                assert any(value in text for text in sent), value

    async def test_the_truncation_retry_resends_the_redacted_text(
        self, tmp_path, redaction_on, sanitizer
    ):
        router = RecordingRouter(truncate_first_conversion=True)

        await _convert_case(tmp_path, router, sanitizer)

        assert router.stages() == ["conversion", "conversion"], "control: retried"
        first, retry = (messages for _, messages in router.calls)
        assert retry == first
        assert not any(PII_IP in text for text in router.sent_text())

    async def test_the_draft_keeps_the_placeholders_the_model_wrote(
        self, tmp_path, redaction_on, sanitizer
    ):
        """Never reversed: ``CaseRedactionContext`` puts real values back only
        when a caller asks (``reverse``), and only the investigation path does,
        for the reply it shows the user. A runbook is meant to be de-identified;
        what the model wrote is what is persisted — as on the extraction path,
        and as the default router's own pass already produced under
        ``SANITIZE_PII``."""
        router = RecordingRouter()

        response = await _convert_case(tmp_path, router, sanitizer)

        placeholder = await _investigation_path(CASE_ID, sanitizer).asanitize(PII_IP)
        (draft,) = response.drafts
        assert f"The replica at {placeholder} refused." in draft.content
        assert PII_IP not in draft.content
        on_disk = Path(draft.file_path).read_text()
        assert placeholder in on_disk and PII_IP not in on_disk


class TestTheDocumentPath:
    @pytest.mark.parametrize("has_sanitizer", [True, False])
    async def test_each_call_redacts_exactly_when_the_investigation_path_does(
        self, tmp_path, redaction_arm, has_sanitizer, sanitizer
    ):
        """The ruling: the model-boundary rule is uniform. With ``SANITIZE_PII``
        on, the default router already redacts this text, so a default
        deployment sees no change; the engine layer makes it hold under a
        substituted router too. Keyed on the conversion's id — a document has
        no case — and placeholders are a keyed function of the value, so the
        id does not change them."""
        handed = sanitizer if has_sanitizer else None
        router = RecordingRouter()

        response = await _convert_document(tmp_path, router, handed)

        assert response.status == ConversionStatus.COMPLETED
        assert router.stages() == ["triage", "analysis", "conversion"], "control"
        engine = _investigation_path(response.conversion_id, handed)
        assert engine.enabled is (redaction_arm and has_sanitizer)
        placeholder = await engine.asanitize(PII_IP)
        for stage in ("triage", "analysis", "conversion"):
            sent = router.sent_text(stage)
            if engine.enabled:
                assert placeholder != PII_IP, "control: the engine redacts an IP"
                assert not any(PII_IP in text for text in sent), stage
                assert any(placeholder in text for text in sent), stage
            else:
                assert any(PII_IP in text for text in sent), stage


# ---------------------------------------------------------------------------
# 4. Fail closed
# ---------------------------------------------------------------------------


class TestARedactionThatCannotRunStopsTheSend:
    async def test_on_the_case_path(self, tmp_path, redaction_arm):
        """Not laundered into a ``ConversionError`` by the conversion's broad
        ``except``: the class propagates and the model is never called. With
        the flag off the redaction never runs, so it cannot refuse — the
        engine does not call it either."""
        router = RecordingRouter()

        if redaction_arm:
            with pytest.raises(RedactionUnavailableError):
                await _convert_case(tmp_path, router, _RefusingAt())
            assert router.calls == []
        else:
            response = await _convert_case(tmp_path, router, _RefusingAt())
            assert response.status == ConversionStatus.COMPLETED
            assert any(PII_IP in text for text in router.sent_text())

    @pytest.mark.parametrize(
        "modes", [1, PARALLEL_THRESHOLD], ids=["parallel", "sequential"]
    )
    @pytest.mark.parametrize("stage", ["triage", "analysis", "conversion"])
    async def test_at_each_document_call(self, tmp_path, redaction_on, stage, modes):
        """Triage fails OPEN on its own errors, and the parallel branch turns
        each failure mode's exception into that mode's error: neither may
        absorb a refusal to send. Nothing reaches the router from the refusing
        call or after it."""
        router = RecordingRouter(modes=modes)

        with pytest.raises(RedactionUnavailableError):
            await _convert_document(tmp_path, router, _RefusingAt(stage))

        order = ["triage", "analysis", "conversion"]
        assert router.stages() == order[: order.index(stage)]

    @pytest.mark.parametrize(
        ("modes", "n"),
        [(2, 2), (5, 3), (PARALLEL_THRESHOLD, 2), (PARALLEL_THRESHOLD, 3)],
        ids=["parallel-2of2", "parallel-3of5", "sequential-2of6", "sequential-3of6"],
    )
    async def test_a_refusal_on_a_later_mode_sends_and_writes_nothing(
        self, tmp_path, redaction_on, modes, n
    ):
        """Every mode is redacted before any is sent. A refusal on the Nth
        mode fails the whole conversion — not after modes 1..N-1 went out and
        left draft files on disk with no job row to own them, for a later
        ``/scan`` to adopt."""
        router = RecordingRouter(modes=modes)

        with pytest.raises(RedactionUnavailableError):
            await _convert_document(tmp_path, router, _RefusingNthGeneration(n))

        assert router.stages() == ["triage", "analysis"], "no generation was sent"
        knowledge = tmp_path / "knowledge"
        written = list(knowledge.rglob("*.md")) if knowledge.exists() else []
        assert written == [], written

    @pytest.mark.parametrize(
        "modes", [2, PARALLEL_THRESHOLD], ids=["parallel", "sequential"]
    )
    async def test_a_router_failure_stays_one_modes_error(
        self, tmp_path, redaction_on, sanitizer, modes
    ):
        """The other side of the line: a failure AT THE ROUTER, after the
        redaction, is one failure mode's ``ConversionError`` and the rest still
        convert — PARTIAL, as before #1901."""
        router = RecordingRouter(modes=modes, fail_conversion=2)

        response = await _convert_document(tmp_path, router, sanitizer)

        assert response.status == ConversionStatus.PARTIAL
        assert len(response.drafts) == modes - 1
        assert router.stages().count("conversion") == modes


# ---------------------------------------------------------------------------
# 5. Structural: every router call is handed redacted messages
# ---------------------------------------------------------------------------

#: The conversion code: the service package, and the preprocessor it owns
#: (``ConversionService`` is its only constructor).
_CONVERSION_MODULES = sorted(
    Path(conversion_service_module.__file__).parent.glob("*.py")
) + [Path(document_preprocessor.__file__)]

_ROUTER_HANDLES = {"llm_router", "_llm_router"}


def _is_router(node: ast.AST) -> bool:
    """``llm_router`` or ``self._llm_router``."""
    if isinstance(node, ast.Name):
        return node.id in _ROUTER_HANDLES
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _ROUTER_HANDLES
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _params_annotated(func: ast.AST, annotation: str) -> set[str]:
    return {
        a.arg
        for a in func.args.args + func.args.kwonlyargs
        if getattr(a.annotation, "id", None) == annotation
    }


def _redacted_names(func: ast.AST) -> set[str]:
    """Names ``func`` binds to redacted messages: to
    ``await <redaction>.asanitize_messages(...)``, where ``<redaction>`` is
    ``model_boundary_redaction(...)`` or a parameter annotated
    ``CaseRedactionContext``; or to ``<prepared>.redacted_messages``, where
    ``<prepared>`` is a parameter annotated ``_PreparedConversion`` (whose one
    construction is checked to hold redacted messages)."""
    params = _params_annotated(func, "CaseRedactionContext")
    prepared = _params_annotated(func, "_PreparedConversion")
    names = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "redacted_messages"
            and isinstance(value.value, ast.Name)
            and value.value.id in prepared
        ):
            names |= targets
            continue
        if not isinstance(value, ast.Await):
            continue
        call = value.value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "asanitize_messages"
        ):
            continue
        source = call.func.value
        if (
            isinstance(source, ast.Call)
            and getattr(source.func, "id", None) == "model_boundary_redaction"
        ) or (isinstance(source, ast.Name) and source.id in params):
            names |= targets
    return names


def test_every_router_call_is_handed_redacted_messages():
    """Enumerates every call ON a router handle in the conversion code — not
    a substring of the source — and requires each to be a ``route`` whose
    ``messages`` is a name its enclosing top-level function bound to a
    model-boundary redaction — directly, or through the ``_PreparedConversion``
    the send step is handed, whose one construction is checked the same way. A new call site, or one that builds its messages
    inline, fails here with its location.

    Not seen: a router reached through a name other than ``llm_router`` /
    ``self._llm_router``. Neither module stores it under another.
    """
    sites: list[tuple[str, bool]] = []
    preparations: list[tuple[str, bool]] = []
    for path in _CONVERSION_MODULES:
        tree = ast.parse(path.read_text())
        scopes = [
            (f"{cls.name}.{fn.name}", fn)
            for cls in tree.body
            if isinstance(cls, ast.ClassDef)
            for fn in cls.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        ] + [
            (fn.name, fn)
            for fn in tree.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for name, func in scopes:
            redacted = _redacted_names(func)
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "_PreparedConversion"
                ):
                    held = next(
                        (
                            k.value
                            for k in node.keywords
                            if k.arg == "redacted_messages"
                        ),
                        None,
                    )
                    preparations.append(
                        (
                            f"{path.name}:{name}",
                            isinstance(held, ast.Name) and held.id in redacted,
                        )
                    )
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and _is_router(node.func.value)
                ):
                    continue
                messages = next(
                    (k.value for k in node.keywords if k.arg == "messages"), None
                )
                sites.append(
                    (
                        f"{path.name}:{name}",
                        node.func.attr == "route"
                        and not any(k.arg == "prompt" for k in node.keywords)
                        and isinstance(messages, ast.Name)
                        and messages.id in redacted,
                    )
                )

    # Positive control: the three model calls conversion makes.
    assert sorted(site for site, _ in sites) == [
        "document_preprocessor.py:DocumentPreprocessor._run_content_triage",
        "pipeline.py:_analyze_document",
        "service.py:ConversionService._convert_single_failure_mode",
    ], sites
    assert all(ok for _, ok in sites), sites
    # The send step takes a ``_PreparedConversion``; its one construction holds
    # messages that came back from the redaction.
    assert preparations == [
        ("service.py:ConversionService._prepare_conversion", True)
    ], preparations
