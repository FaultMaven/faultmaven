"""Runbook extraction reads the case it was asked about (#1661).

``extract_knowledge_from_case`` used to read the case through ``get_by_id`` and
``get_evidence``. No case repository has either method, so the
``AttributeError`` was swallowed by a broad ``except`` and every production
extraction asked the model to write a runbook from::

    Case Title: Unknown Case
    No messages included.
    No evidence included.

No test saw it because every double implemented the two methods production
lacks. So the tests here drive extraction over a REAL repository — the SQLite
one, on a real engine — and read back the prompt the provider received. The
provider is the only double: it is the model boundary, and what crosses it is
the thing under test.

Four properties, one class each:

1. The prompt carries the case's own title, description, messages and
   evidence, with the evidence rendered by the fields ``Evidence`` has.
2. The windows keep the END of the case. The message window used to be the
   OLDEST 50 rows (``get_messages`` pages from the start), which on a long
   case cut the resolution — the part a runbook is made of. The evidence window
   is chosen by recency, not by list order, because the two repositories and
   the engine do not agree on that order (#1609).
3. A case that cannot be read fails the extraction. It does not produce a
   runbook from nothing.
4. What reaches the model has passed the SAME redaction the investigation path
   applies to what it sends a model. This is the first time production sends
   a transcript and evidence to the extraction model.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.settings import get_settings
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.prompts.context_builder import (
    _evidence_recency_key,
)
from faultmaven.exceptions import ConfigurationException, NotFoundError
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.infrastructure.security.redaction import (
    DataSanitizer,
    RedactionUnavailableError,
)
from faultmaven.modules.case.contracts import MessageRowKind, append_message_row
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from faultmaven.modules.knowledge.domain.services import suggestion_service
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    EXTRACTION_EVIDENCE_WINDOW,
    EXTRACTION_MESSAGE_WINDOW,
    SuggestionService,
    _evidence_recency,
)
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    InMemorySuggestionRepository,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

ENTERPRISE = "ent_1661"
OWNER = "user_1661"
TITLE = "Checkout API 500s during the evening peak"
DESCRIPTION = "Roughly one checkout in five fails with a 500 from 19:40."
OPENING = "checkout is 500ing again, the app log says remaining connection slots"
RESOLUTION = "Added a finally that rolls back; idle-in-transaction is single digits."
SYMPTOM = "Repeated FATAL remaining-connection-slots entries across the peak."
CAUSE = "187 backends idle in transaction from the promo-code lookup path."

SOURCE_START = "--- SOURCE MATERIAL: CASE ---"


# ---------------------------------------------------------------------------
# The model boundary, and the only double
# ---------------------------------------------------------------------------


class RecordingProvider:
    """Records every prompt. Answers with queued bodies, then a valid runbook,
    so a test that does not care about the repair turn gets exactly one call."""

    def __init__(self, *bodies: str) -> None:
        self._bodies = list(bodies)
        self.prompts: list[str] = []

    async def generate(self, *, prompt: str, **_kw) -> SimpleNamespace:
        self.prompts.append(prompt)
        body = self._bodies.pop(0) if self._bodies else valid_runbook()
        return SimpleNamespace(content=body, is_truncated=False)


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_repository():
    """The SQLite case repository over a real (in-memory) SQLite engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


def _case(case_id: str = "case_aabb16611661") -> Case:
    return Case(
        case_id=case_id,
        user_id=OWNER,
        enterprise_id=ENTERPRISE,
        title=TITLE,
        description=DESCRIPTION,
        state=CaseState.INQUIRY,
    )


def _say(case: Case, turn: int, user: str, assistant: str | None = None) -> None:
    """One exchange, through the one constructor a row has (#1452)."""
    append_message_row(
        case, MessageRowKind.USER_TURN, user, turn_number=turn, author_id=OWNER
    )
    if assistant is not None:
        append_message_row(
            case, MessageRowKind.AGENT_ANSWER, assistant, turn_number=turn
        )


def _evidence(
    summary: str,
    *,
    turn: int = 1,
    category: EvidenceCategory = EvidenceCategory.SYMPTOM_EVIDENCE,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
    collected_at: datetime | None = None,
) -> Evidence:
    return Evidence(
        category=category,
        primary_purpose="diagnosis",
        summary=summary,
        source_type=source_type,
        source_file_id="file_aabb16611661",
        collected_by=OWNER,
        collected_at_turn=turn,
        collected_at=collected_at or datetime.now(timezone.utc),
    )


def _service(repository, provider, *, sanitizer=None) -> SuggestionService:
    return SuggestionService(
        case_repository=repository,
        knowledge_service=None,
        sanitizer=sanitizer,
        llm_provider=provider,
        suggestion_repository=InMemorySuggestionRepository(),
    )


async def _extract(service: SuggestionService, case_id: str = "case_aabb16611661"):
    return await service.extract_knowledge_from_case(
        case_id=case_id, enterprise_id=ENTERPRISE, extracted_by=OWNER
    )


def _source(prompt: str) -> str:
    """The case section of a prompt: everything the case contributed."""
    return prompt[prompt.index(SOURCE_START) :]


def _transcript_lines(source: str) -> list[str]:
    return [
        line
        for line in source.splitlines()
        if re.match(r"^\[(user|assistant)\]: ", line)
    ]


def _evidence_lines(source: str) -> list[str]:
    return [line for line in source.splitlines() if line.startswith("- [")]


# ---------------------------------------------------------------------------
# 1. The prompt carries the case
# ---------------------------------------------------------------------------


class TestThePromptCarriesTheCase:
    async def test_title_description_messages_and_evidence_reach_the_model(
        self, sqlite_repository
    ):
        """The defect itself. On the unfixed tree this prompt read
        ``Case Title: Unknown Case`` with no messages and no evidence."""
        case = _case()
        _say(case, 1, OPENING, "That is PostgreSQL refusing new connections.")
        _say(case, 2, RESOLUTION, "Good. Keep the session timeout as a backstop.")
        case.evidence.append(_evidence(SYMPTOM, turn=1))
        case.evidence.append(
            _evidence(
                CAUSE,
                turn=2,
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.METRICS,
            )
        )
        await sqlite_repository.save(case)

        provider = RecordingProvider()
        suggestion = await _extract(_service(sqlite_repository, provider))

        source = _source(provider.prompts[0])
        assert f"Case Title: {TITLE}" in source
        assert f"Description: {DESCRIPTION}" in source
        assert "Unknown Case" not in source
        assert f"[user]: {OPENING}" in source
        assert f"[user]: {RESOLUTION}" in source
        assert "[assistant]: That is PostgreSQL refusing new connections." in source
        assert "No messages included." not in source
        assert "No evidence included." not in source
        # Recorded on the suggestion, so the reviewer sees what was read.
        assert suggestion.source_case_title == TITLE
        assert suggestion.message_count == 4
        assert suggestion.evidence_count == 2

    async def test_evidence_renders_by_the_fields_evidence_has(self, sqlite_repository):
        """The loop read ``artifact_type`` and ``name``; ``Evidence`` has
        neither, so every line would have become ``- [unknown] : <summary>``."""
        case = _case()
        _say(case, 1, OPENING)
        case.evidence.append(
            _evidence(
                CAUSE,
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.METRICS,
            )
        )
        await sqlite_repository.save(case)

        provider = RecordingProvider()
        await _extract(_service(sqlite_repository, provider))

        lines = _evidence_lines(_source(provider.prompts[0]))
        assert lines == [f"- [causal_evidence | metrics] {CAUSE}"]
        assert "unknown" not in lines[0]

    async def test_the_include_flags_are_honoured(self, sqlite_repository):
        case = _case()
        _say(case, 1, OPENING)
        case.evidence.append(_evidence(SYMPTOM))
        await sqlite_repository.save(case)

        provider = RecordingProvider()
        suggestion = await _service(
            sqlite_repository, provider
        ).extract_knowledge_from_case(
            case_id=case.case_id,
            enterprise_id=ENTERPRISE,
            extracted_by=OWNER,
            include_messages=False,
            include_evidence=False,
        )

        source = _source(provider.prompts[0])
        assert f"Case Title: {TITLE}" in source
        assert OPENING not in source
        assert SYMPTOM not in source
        assert suggestion.message_count == 0
        assert suggestion.evidence_count == 0


# ---------------------------------------------------------------------------
# 2. The windows keep the end of the case
# ---------------------------------------------------------------------------


class TestTheWindowsKeepTheEndOfTheCase:
    async def test_the_message_window_is_the_last_rows(self, sqlite_repository):
        """A long case sends its LAST ``EXTRACTION_MESSAGE_WINDOW`` rows. The
        old slice sent the first 50, which is where the resolution is not."""
        case = _case()
        turns = EXTRACTION_MESSAGE_WINDOW  # two rows a turn: twice the window
        for turn in range(1, turns + 1):
            _say(
                case, turn, f"user row of turn {turn:03d}", f"answer of turn {turn:03d}"
            )
        _say(case, turns + 1, RESOLUTION)
        await sqlite_repository.save(case)

        provider = RecordingProvider()
        suggestion = await _extract(_service(sqlite_repository, provider))

        lines = _transcript_lines(_source(provider.prompts[0]))
        assert len(lines) == EXTRACTION_MESSAGE_WINDOW
        assert lines[-1] == f"[user]: {RESOLUTION}", "the end of the case was cut"
        assert (
            "[user]: user row of turn 001" not in lines
        ), "the window is the first rows"
        # In order: the window is a slice of the transcript, not a re-sort.
        assert lines[0] == f"[assistant]: answer of turn {turns - 24:03d}"
        assert suggestion.message_count == EXTRACTION_MESSAGE_WINDOW

    @pytest.mark.parametrize("backend", ["sqlite", "in_memory"])
    async def test_the_evidence_window_is_the_most_recent_rows(
        self, backend, sqlite_repository
    ):
        """Chosen by recency, not by list order. The SQL repositories load
        evidence newest-first and the in-memory one keeps it oldest-first
        (#1609), so ``[:N]`` keeps the newest on one and the oldest on the
        other, and ``[-N:]`` the reverse. Both backends run, so neither slice
        passes."""
        repository = (
            sqlite_repository if backend == "sqlite" else InMemoryCaseRepository()
        )
        case = _case()
        _say(case, 1, OPENING)
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        total = EXTRACTION_EVIDENCE_WINDOW + 5
        for n in range(1, total + 1):
            case.evidence.append(
                _evidence(
                    f"evidence row {n:03d}",
                    turn=n,
                    collected_at=start + timedelta(minutes=n),
                )
            )
        await repository.save(case)

        provider = RecordingProvider()
        suggestion = await _extract(_service(repository, provider))

        lines = _evidence_lines(_source(provider.prompts[0]))
        rendered = [
            int(re.search(r"evidence row (\d+)", line).group(1)) for line in lines
        ]
        kept = list(range(total - EXTRACTION_EVIDENCE_WINDOW + 1, total + 1))
        assert rendered == kept, f"{backend}: kept {rendered}"
        assert suggestion.evidence_count == EXTRACTION_EVIDENCE_WINDOW

    def test_recency_is_the_investigation_prompts_recency(self):
        """Two copies of one key: the investigation prompt's private
        ``_evidence_recency_key`` and extraction's ``_evidence_recency``
        (knowledge does not import the engine's prompt package). They must
        order the same rows the same way — turn first, then collection time
        within a turn."""
        start = datetime.now(timezone.utc)
        rows = [
            _evidence("a", turn=3, collected_at=start),
            _evidence("b", turn=1, collected_at=start + timedelta(minutes=9)),
            _evidence("c", turn=3, collected_at=start - timedelta(minutes=1)),
            _evidence("d", turn=2, collected_at=start),
        ]

        for ev in rows:
            assert _evidence_recency(ev) == _evidence_recency_key(ev)
        assert [ev.summary for ev in sorted(rows, key=_evidence_recency)] == [
            "b",
            "d",
            "c",
            "a",
        ]


# ---------------------------------------------------------------------------
# 3. A case that cannot be read fails the extraction
# ---------------------------------------------------------------------------


class _BrokenRepository:
    async def get(self, case_id: str):
        raise RuntimeError(f"database is gone while reading {case_id}")


class TestACaseThatCannotBeReadFailsTheExtraction:
    """It used to warn and carry on, and "carry on" meant spending up to four
    generations on a runbook made from nothing and filing it for review."""

    async def test_a_missing_case_raises_and_nothing_is_generated(
        self, sqlite_repository
    ):
        provider = RecordingProvider()
        service = _service(sqlite_repository, provider)

        with pytest.raises(NotFoundError, match="case_aabb00000000"):
            await _extract(service, case_id="case_aabb00000000")

        assert provider.prompts == []
        assert await service._repository.count_for_enterprise(ENTERPRISE) == 0

    async def test_a_failing_read_propagates_and_nothing_is_generated(self):
        provider = RecordingProvider()
        service = _service(_BrokenRepository(), provider)

        with pytest.raises(RuntimeError, match="case_aabb16611661"):
            await _extract(service)

        assert provider.prompts == []
        assert await service._repository.count_for_enterprise(ENTERPRISE) == 0

    async def test_no_case_repository_raises_and_nothing_is_generated(self):
        provider = RecordingProvider()
        service = _service(None, provider)

        with pytest.raises(ConfigurationException, match="case_aabb16611661"):
            await _extract(service)

        assert provider.prompts == []
        assert await service._repository.count_for_enterprise(ENTERPRISE) == 0


# ---------------------------------------------------------------------------
# 4. The extraction input passes the investigation path's redaction
# ---------------------------------------------------------------------------

PII_IP = "10.20.30.40"
PII_DB_URL = "postgresql://svc_checkout:hunter2pw@db-primary.internal"
PII_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PII_EMAIL = "jane.doe@contoso.example"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@pytest.fixture
def redaction_on(monkeypatch):
    """``SANITIZE_PII=true`` — the flag the investigation path keys on."""
    monkeypatch.setattr(get_settings().protection, "sanitize_pii", True)


def _with_presidio(sanitizer: DataSanitizer) -> DataSanitizer:
    """A sanitizer whose analyzer is up, with Presidio answered in-process.

    CI has no Presidio, and the regex half does not detect an email address,
    so without this nothing here would show that extraction runs the WHOLE
    detection path (regex, then Presidio) rather than a regex-only subset. The
    stand-in registers what it finds in the case registry exactly as
    ``_apply_presidio`` does, through the same placeholder mint.
    """

    def _presidio(text: str, registry: dict) -> str:
        def _swap(match: re.Match) -> str:
            type_map = registry.setdefault("EMAIL_ADDRESS", {})
            value = match.group(0)
            if value not in type_map:
                type_map[value] = sanitizer._hashed_placeholder("EMAIL_ADDRESS", value)
            return type_map[value]

        return EMAIL_RE.sub(_swap, text)

    sanitizer.analyzer_available = True
    sanitizer._apply_presidio = _presidio
    return sanitizer


def _investigation_path_redaction(case_id: str, sanitizer) -> CaseRedactionContext:
    """The redaction the milestone engine builds for a turn, built the way it
    builds it: its own ``_should_redact`` decides ``enabled``."""
    return CaseRedactionContext(
        case_id=case_id,
        sanitizer=sanitizer,
        redis_client=None,
        enabled=MilestoneEngine._should_redact(SimpleNamespace(sanitizer=sanitizer)),
    )


async def _pii_case(repository) -> Case:
    case = _case()
    _say(
        case,
        1,
        f"the replica at {PII_IP} refuses; app uses {PII_DB_URL}/checkout",
        f"Ask {PII_EMAIL} to check pg_stat_activity on the primary.",
    )
    case.evidence.append(_evidence(f"Config carries the key {PII_AWS_KEY} in clear."))
    await repository.save(case)
    return case


class TestTheExtractionInputIsRedactedAsTheInvestigationPathRedactsIt:
    async def test_no_pii_value_reaches_the_model_in_clear(
        self, sqlite_repository, redaction_on
    ):
        sanitizer = _with_presidio(DataSanitizer())
        case = await _pii_case(sqlite_repository)

        provider = RecordingProvider()
        await _extract(_service(sqlite_repository, provider, sanitizer=sanitizer))

        prompt = provider.prompts[0]
        engine = _investigation_path_redaction(case.case_id, sanitizer)
        assert engine.enabled, "control: the investigation path redacts here"
        for value in (PII_IP, PII_DB_URL, PII_AWS_KEY, PII_EMAIL):
            redacted = await engine.asanitize(value)
            assert redacted != value, f"control: the engine redacts {value!r}"
            assert value not in prompt, f"{value!r} reached the extraction model"
            # The SAME redaction: the investigation path's placeholder for the
            # value is what the extraction model sees in its place. A row that
            # was dropped rather than redacted would fail here.
            assert redacted in prompt, f"{redacted!r} missing for {value!r}"

    async def test_with_redaction_off_the_input_is_what_the_engine_would_send(
        self, sqlite_repository
    ):
        """``SANITIZE_PII`` unset (the standalone default): the engine sends its
        prompt as written, and so does extraction. The positive control for
        the test above — the values are in the case and do reach the prompt."""
        sanitizer = _with_presidio(DataSanitizer())
        case = await _pii_case(sqlite_repository)

        provider = RecordingProvider()
        await _extract(_service(sqlite_repository, provider, sanitizer=sanitizer))

        assert not _investigation_path_redaction(case.case_id, sanitizer).enabled
        for value in (PII_IP, PII_DB_URL, PII_AWS_KEY, PII_EMAIL):
            assert value in provider.prompts[0]

    async def test_the_repair_turn_is_redacted_too(
        self, sqlite_repository, redaction_on
    ):
        """Redaction sits at the call, not at the assembly of the case block:
        the repair prompt carries the model's own previous draft, and a value
        in it must not go back out in clear either."""
        sanitizer = DataSanitizer()
        await _pii_case(sqlite_repository)
        rejected_draft = f"## Problem\nThe replica at {PII_IP} refused.\n"

        provider = RecordingProvider(rejected_draft)
        await _extract(_service(sqlite_repository, provider, sanitizer=sanitizer))

        assert len(provider.prompts) == 2, "control: the repair turn ran"
        assert "REPAIR REQUIRED" in provider.prompts[1]
        assert "The replica at <IP_ADDRESS_" in provider.prompts[1]
        for prompt in provider.prompts:
            assert PII_IP not in prompt

    async def test_redaction_that_cannot_run_stops_the_send(
        self, sqlite_repository, redaction_on
    ):
        """Fail-closed, as on the investigation path: the sanitizer's refusal
        propagates. It is not caught into a skeleton draft, and nothing is sent
        or stored."""

        class _Refusing(DataSanitizer):
            def sanitize_text_with_registry(self, text, entity_registry):
                raise RedactionUnavailableError("Presidio analyzer failed")

        await _pii_case(sqlite_repository)
        provider = RecordingProvider()
        service = _service(sqlite_repository, provider, sanitizer=_Refusing())

        with pytest.raises(RedactionUnavailableError):
            await _extract(service)

        assert provider.prompts == []
        assert await service._repository.count_for_enterprise(ENTERPRISE) == 0

    @pytest.mark.parametrize("sanitize_pii", [True, False])
    @pytest.mark.parametrize("has_sanitizer", [True, False])
    async def test_it_redacts_exactly_when_the_investigation_path_does(
        self, monkeypatch, sanitize_pii, has_sanitizer
    ):
        """One decision, two call sites: the engine's ``_should_redact`` (in a
        module this change does not touch) and extraction's. The truth table
        pins them together so neither can move alone."""
        monkeypatch.setattr(get_settings().protection, "sanitize_pii", sanitize_pii)
        sanitizer = DataSanitizer() if has_sanitizer else None
        service = _service(
            InMemoryCaseRepository(), RecordingProvider(), sanitizer=sanitizer
        )

        extraction = service._model_boundary_redaction("case_aabb16611661")
        engine = _investigation_path_redaction("case_aabb16611661", sanitizer)

        assert extraction.enabled is engine.enabled
        assert extraction.sanitizer is sanitizer

    def test_the_provider_is_reached_only_through_the_redacting_call(self):
        """Structural, because the tests above can only exercise the calls that
        exist. ``_generate_once`` is where the redaction is applied, so it must
        be the ONLY place the service touches its provider. A second call site
        — ``_generate_title`` says "in production, use LLM", and would send the
        case title — would reach the model without it, and nothing behavioural
        here would know. So: every read of the provider outside ``__init__``
        sits inside ``_generate_once``, whichever ``self`` attribute ``__init__``
        stored it under (an alias is the same provider by another name).

        Not seen: a read through ``getattr(self, "<name>")`` or through an
        object other than ``self``. Neither shape occurs in this module.
        """
        tree = ast.parse(Path(suggestion_service.__file__).read_text())
        service_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SuggestionService"
        )
        methods = [
            node
            for node in service_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        init = next(m for m in methods if m.name == "__init__")

        def self_attr(node: ast.AST) -> str | None:
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                return node.attr
            return None

        # Every attribute __init__ binds to the ``llm_provider`` argument, or to
        # an attribute already holding it.
        handles = {"_llm_provider"}
        for node in ast.walk(init):
            if isinstance(node, ast.Assign) and (
                (isinstance(node.value, ast.Name) and node.value.id == "llm_provider")
                or self_attr(node.value) in handles
            ):
                handles |= {self_attr(t) for t in node.targets if self_attr(t)}

        def reads_provider(node: ast.AST) -> bool:
            return (
                self_attr(node) in handles
                and isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
            )

        sites = {
            method.name: count
            for method in methods
            if method is not init
            and (count := sum(1 for n in ast.walk(method) if reads_provider(n)))
        }

        # Positive control: the scan sees the reads that do exist — the
        # ``if not self._llm_provider`` guard and the ``generate`` call.
        assert sites.get("_generate_once", 0) >= 2, sites
        assert set(sites) == {"_generate_once"}, (handles, sites)
