"""CRLF-authored documents are treated exactly like their LF twins (#1403).

``$`` under ``re.MULTILINE`` matches at end-of-string or immediately before a
``\\n``. On a CRLF line the ``\\r`` sits between the heading text and the ``\\n``
and ``[ \\t]*`` does not consume it, so ``^## Causes$`` never anchors. Measured
on the shipped corpus before the fix: **0 of 91** runbooks validated under CRLF
against 91 of 91 under LF, mean quality score 90.5 -> 75.8, 546 "Missing
required section" errors on documents carrying every section.

**Each test pins ONE layer.** The fix normalises at four boundaries so that no
single forgotten call site can reintroduce the bug, and the hazard with layered
guards is that reverting any one layer leaves the others covering for it — every
test passes, and the layer is unguarded. So the tests are built to be
individually mutation-killable:

* the route test spies on ``upload_document`` and asserts what it RECEIVES, so
  the service-layer normalisation cannot satisfy it;
* the service tests call the service methods DIRECTLY, so the route decode
  cannot satisfy them;
* the validator tests call the pure functions directly, so nothing upstream can.

Each test names the mutation that must kill it. Verified by reverting each
layer in turn: every one fails exactly the tests below it and no others.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.api.routes import (
    get_knowledge_service,
)
from faultmaven.modules.knowledge.api.routes import (
    router as knowledge_router,
)
from faultmaven.modules.knowledge.domain.services.document_parser import DocumentParser
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    QualityScorer,
    RunbookValidator,
)
from faultmaven.utils.line_endings import decode_text, normalize_line_endings
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

CR = chr(13)


def _runbook_corpus() -> list[pathlib.Path]:
    root = (
        pathlib.Path(__file__).resolve().parents[4]
        / "resources/knowledge/pack/runbooks"
    )
    books = sorted(root.rglob("*.md"))
    assert (
        len(books) > 50
    ), f"corpus missing at {root} — every guard below would be vacuous"
    return books


def _crlf(text: str) -> str:
    """The CRLF twin of an LF document, whatever the source happened to use."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


# --------------------------------------------------------------------------
# The helper itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a\r\nb", "a\nb"),  # CRLF
        ("a\rb", "a\nb"),  # lone CR — a line ending per CommonMark 2.1
        ("a\r\r\nb", "a\n\nb"),  # the shape `.replace` cannot do in one pass
        ("a\nb", "a\nb"),  # already LF: the identity
        ("", ""),
    ],
)
def test_normalize_covers_every_line_ending_convention(raw, expected):
    assert normalize_line_endings(raw) == expected


def test_normalize_is_idempotent():
    """``"\\r\\r\\n".replace("\\r\\n", "\\n")`` is ``"\\r\\n"`` — still a CRLF. The
    narrow spelling is not idempotent, which is one of the three reasons the
    regex is ``\\r\\n?``. Mutation: swap the regex for the ``.replace`` form."""
    sample = "a\r\r\nb\rc\r\nd\n"
    once = normalize_line_endings(sample)
    assert normalize_line_endings(once) == once
    assert CR not in once


def test_normalize_is_the_identity_on_the_shipped_corpus():
    """The corpus is LF-only, so the fix must be a provable no-op on it. This is
    the whole-corpus half of the 'LF behaviour unchanged' bar."""
    for path in _runbook_corpus():
        text = path.read_text(encoding="utf-8")
        # `is`, not `==`: the disjunction this replaces (`X is text or
        # X == text`) reduced to the equality arm and asserted nothing about
        # identity. Note what this does and does NOT pin -- identity survives
        # even without the `"\r" not in text` fast path, because CPython's
        # `str.replace` returns the original object when nothing matches. So
        # this guards "LF text is never rebuilt", not the fast path itself,
        # which is a performance property and is left to the benchmark file.
        assert normalize_line_endings(text) is text, path.name


def test_decode_text_agrees_with_decode_then_normalize():
    """The fused decode is an optimisation, so it has to be provably equivalent
    to the obvious two-pass form — including where a multi-byte character or a
    CRLF pair straddles an internal decode-buffer boundary, which is the one way
    an incremental decoder can differ from ``bytes.decode()``."""
    for pad in (8190, 8191, 8192, 8193, 65535, 65536):
        for ch in ("e", "é", "中", "🔥"):
            raw = (b"a" * pad) + ch.encode("utf-8") + b"\r\n## Causes\r\nx"
            assert decode_text(raw) == normalize_line_endings(raw.decode("utf-8"))


def test_decode_text_preserves_a_bom_and_still_raises_on_bad_utf8():
    """Two behaviours the upload route depends on. The BOM must survive because
    ``document_preprocessor._LEADING_NOISE`` is what strips it and #1375 was
    reopened by a BOM once already; the error must still raise because the route
    catches it to fall back to latin-1."""
    assert decode_text("﻿---\r\n".encode("utf-8"))[0] == "﻿"

    with pytest.raises(UnicodeDecodeError):
        decode_text(b"## Causes\r\n\xff\xfe")

    assert decode_text(b"\xff\xfe ok\r\n", "latin-1").endswith("ok\n")


# --------------------------------------------------------------------------
# Layer 4 — the gate and the scorer (the surface #1403 names)
# --------------------------------------------------------------------------


def test_every_shipped_runbook_validates_identically_under_crlf():
    """The load-bearing guard, and the one the issue is about.

    CRLF must reach PARITY with LF, not merely improve — errors, warnings and
    score, per runbook. The shipped corpus is LF-only, so a corpus comparison
    structurally cannot see a CRLF regression; the CRLF twin is synthesised here
    for exactly that reason.

    Mutation: drop ``normalize_line_endings`` from ``validate_content`` — 91
    runbooks each report 6 missing sections.
    """
    validator = RunbookValidator()
    for path in _runbook_corpus():
        lf = path.read_text(encoding="utf-8")
        got, want = validator.validate_content(_crlf(lf)), validator.validate_content(
            lf
        )
        assert got.passed == want.passed, path.name
        assert sorted(got.errors) == sorted(want.errors), path.name
        assert sorted(got.warnings) == sorted(want.warnings), path.name


def test_every_shipped_runbook_scores_identically_under_crlf():
    """The score is persisted to ``conversion_drafts.quality_score`` and drives
    the ``< QUALITY_WARNING_THRESHOLD`` warning, so a CRLF document scoring 15
    points low is a durable wrong answer, not just a failed gate. Scored through
    ``QualityScorer`` directly because it is a different class on a different
    route from the validator — the distinction that made an earlier guard in
    this area vacuous.

    Mutation: drop the normalisation from ``score_content``.
    """
    scorer = QualityScorer()
    for path in _runbook_corpus():
        lf = path.read_text(encoding="utf-8")
        got, want = scorer.score_content(_crlf(lf)), scorer.score_content(lf)
        assert (got.overall, got.grade) == (want.overall, want.grade), path.name


def test_the_causes_subsection_gate_still_fires_under_crlf():
    """``_validate_structure`` has a SECOND ``[ \\t]*$`` matcher: the one that
    decides whether to check that ``## Causes`` holds at least one ``### Cause``.
    Under CRLF it never matched, so that gate was skipped entirely — masked at
    the time by the missing-section error firing anyway. A fix that repaired
    only the required-section loop would leave a live gate disarmed, and no
    corpus test would notice because every shipped runbook HAS its causes."""
    empty_causes = (
        "---\nid: x\ntitle: t\ndomain: d\nservice: s\n"
        "symptom_class: [a]\nseverity: high\nstatus: draft\nverified_by: x\n---\n"
        "## Symptom Recognition\nb\n## Applicability\nb\n## Diagnostic Steps\nb\n"
        "## Causes\nno subsections here\n## Prevention\nb\n## Sources\nb\n"
    )
    errors = RunbookValidator().validate_content(_crlf(empty_causes)).errors
    assert any("at least one ### Cause" in e for e in errors), errors


# --------------------------------------------------------------------------
# Layer 1 — the upload route's decode
# --------------------------------------------------------------------------


def _client(service):
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.state.team_service = None

    async def _service():
        return service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            enterprise_id="ent-1",
            is_platform_admin=lambda: True,
        )

    app.dependency_overrides[get_knowledge_service] = _service
    app.dependency_overrides[require_authentication] = _user
    return TestClient(app)


def test_the_upload_route_hands_the_service_lf():
    """Pins the ROUTE's decode, not the service's normalisation.

    ``upload_document`` is a mock here, so its own (real) normalisation never
    runs — the only thing that can make this pass is the route decoding with
    universal newlines. That is what makes this test die to a revert of layer 1
    alone, instead of being covered for by layer 3.

    Mutation: put ``content.decode("utf-8", errors="strict")`` back.
    """
    service = SimpleNamespace(
        upload_document=AsyncMock(
            return_value={"document_id": "kb-1", "status": "uploaded"}
        )
    )
    body = "---\r\nid: x\r\n---\r\n\r\n## Causes\r\ntext\r\n".encode("utf-8")

    response = _client(service).post(
        "/api/v1/knowledge/documents",
        data={"title": "Redis OOM", "document_type": "runbook", "scope": "global"},
        files={"file": ("redis-oom.md", body, "text/markdown")},
    )

    assert response.status_code == 201, response.text
    delivered = service.upload_document.call_args.kwargs["content"]
    assert CR not in delivered, repr(delivered)
    assert "## Causes\ntext\n" in delivered


# --------------------------------------------------------------------------
# Layer 2 — text extraction (the formats that do NOT come back via read_text)
# --------------------------------------------------------------------------


def test_the_parser_strips_a_cr_smuggled_through_an_html_entity(tmp_path):
    """``Path.read_text`` is universal-newlines, which is why the txt/markdown/
    html READS never carried a CR — but BeautifulSoup decodes ``&#13;`` to a
    literal one, so an LF file can still yield CR-bearing text. pypdf does the
    same on an unescaped CR in a literal string (it does not implement PDF 32000
    7.3.4.2); HTML is used here because it needs no binary fixture.

    It matters because ``cleanup_text``'s first rule is ``(?m)^(?:Page \\d+...)$``
    and page-footer stripping is exactly what these formats need.

    Mutation: return ``text`` instead of ``normalize_line_endings(text)``.
    """
    page = tmp_path / "doc.html"
    page.write_text("<h1>Title</h1><p>alpha&#13;bravo</p>\n", encoding="utf-8")

    extracted = DocumentParser().parse(page, "text/html")

    assert CR not in extracted, repr(extracted)
    assert "alpha\nbravo" in extracted


# --------------------------------------------------------------------------
# Layer 3 — the service choke points that own a WRITE
#
# These are what make the fix true of STORAGE rather than only of matching.
# Layer 4 normalises what the gate SEES; if the service then persisted the raw
# string, the file, the `knowledge_items.content` row, `size_bytes` and the
# ChromaDB chunks would all keep the `\r` — and the authoring gate's claim to
# measure "exactly as the chunker sees it" would be quietly false.
#
# Driven through the real service on in-memory SQLite with only the ChromaDB
# call mocked, so a revert of layer 3 is not covered for by layers 1 or 4.
# --------------------------------------------------------------------------

#: The standalone tenant the services resolve to via
#: `writable_enterprise_id(None)`. Read from the constant both sides use --
#: a hand-copied literal would drift into a foreign-key failure that says
#: nothing about line endings.
_DEFAULT_ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID

#: A distinct id for the BILLING row. The fixture this was adapted from used
#: the enterprise constant as an organization primary key; nothing here needs
#: them equal, and separate ids keep the two tiers legible (ADR-017: the
#: enterprise isolates, the organization bills).
_STANDALONE_ORG_ID = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture(scope="function")
async def _engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="function")
async def _session_factory(_engine):
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=_DEFAULT_ENTERPRISE_ID,
                name="Default Enterprise",
                slug="default",
            )
        )
        session.add(
            OrganizationModel(
                organization_id=_STANDALONE_ORG_ID,
                enterprise_id=_DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


def _service(session_factory) -> KnowledgeService:
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=5)
    return service


@pytest.mark.asyncio
async def test_upload_document_stores_lf_on_disk(
    _session_factory, tmp_path, monkeypatch
):
    """What is written must be what was validated.

    This is also the guard for the suggestion-APPROVAL path: it reaches this
    method as ``upload_document(content=suggestion.suggested_content)``, so a
    CRLF suggestion becomes a CRLF runbook file without it.

    Mutation: drop ``content = normalize_line_endings(content)`` from
    ``upload_document`` — the gate still passes (layer 4 normalises what it
    SEES) and a CRLF file lands on disk, which is exactly the silent split
    between verdict and storage this pins.
    """
    monkeypatch.chdir(tmp_path)

    await _service(_session_factory).upload_document(
        content=_crlf(valid_runbook("Redis Notes For The Cache Tier")),
        title="Redis Notes",
        document_type="runbook",
        scope="personal",
        owner_id="user-42",
    )

    written = list((tmp_path / "data" / "knowledge").rglob("*.md"))
    assert len(written) == 1, written
    on_disk = written[0].read_bytes().decode("utf-8")
    assert CR not in on_disk, repr(on_disk[:200])


@pytest.mark.asyncio
async def test_update_document_metadata_reindexes_lf(
    _session_factory, tmp_path, monkeypatch
):
    """``PUT /knowledge/documents/{id}`` is the one content write with NO
    runbook quality gate — any authenticated user, an untyped ``dict`` body, and
    a ``content`` key re-chunks the document straight into ChromaDB. So layer 4
    cannot cover it (it is never called) and there is no request model for a
    validator to hang off. Measured across the corpus, CRLF here moved chunk
    boundaries and put a ``\\r`` into 1284 of 1297 chunks.

    Asserts on what is handed to the INDEXER, which is the thing that gets
    embedded. Mutation: drop the normalisation from ``update_document_metadata``.
    """
    monkeypatch.chdir(tmp_path)
    service = _service(_session_factory)

    created = await service.upload_document(
        content=valid_runbook("Redis Notes For The Cache Tier"),
        title="Redis Notes",
        document_type="runbook",
        scope="personal",
        owner_id="user-42",
    )
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda s: s)
    service._index_document_in_vector_store.reset_mock()

    await service.update_document_metadata(
        document_id=created["document_id"],
        content=_crlf(valid_runbook("Redis Notes For The Cache Tier")) + "\r\nmore\r\n",
    )

    indexed = service._index_document_in_vector_store.call_args.args[0]
    assert CR not in indexed.content, repr(indexed.content[:200])


@pytest.mark.asyncio
async def test_update_suggestion_stores_lf():
    """``PUT /knowledge/suggestions/{id}`` takes an UNTYPED ``dict`` body, so
    there is no request model a Pydantic validator could normalise on and this
    service method is the boundary.

    It matters because of where the content goes: ``suggested_content`` is
    re-validated by the review loop and then handed to ``upload_document``
    verbatim on approval, so an edit pasted from a Windows editor showed the
    reviewer six "Missing required section" errors about a document that had
    all six.

    Mutation: pass ``content`` straight through instead of normalising it.
    """
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )

    service = SuggestionService(suggestion_repository=AsyncMock())
    suggestion = MagicMock()
    suggestion.suggested_title = "T"
    suggestion.suggested_content = "old"
    service.get_suggestion_visible = AsyncMock(return_value=suggestion)
    service._scan_and_record = AsyncMock()

    await service.update_suggestion(
        "sug-1", content="## Causes\r\n### Cause A: x\r\n", enterprise_id="ent-1"
    )

    delivered = suggestion.update_content.call_args.kwargs["content"]
    assert CR not in delivered, repr(delivered)


@pytest.mark.asyncio
async def test_update_draft_writes_lf_to_disk(_session_factory, tmp_path, monkeypatch):
    """``PUT /knowledge/conversions/{id}/drafts/{draft_id}`` is a JSON body
    field, so nothing upstream has decoded it through ``Path.read_text``.

    This method persists BEFORE it validates, so the normalisation has to
    happen ahead of the write or the file and the recorded verdict disagree —
    which is what this asserts by reading the bytes back off disk.

    Mutation: move or drop the normalisation in ``update_draft``.
    """
    from faultmaven.infrastructure.persistence.models import (
        ConversionDraftModel,
        ConversionJobModel,
        UploadedFileModel,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )

    monkeypatch.chdir(tmp_path)
    rel = "data/knowledge/global/rb.md"
    (tmp_path / "data" / "knowledge" / "global").mkdir(parents=True)

    async with _session_factory() as session:
        session.add(
            UploadedFileModel(
                file_id="file_1",
                enterprise_id=_DEFAULT_ENTERPRISE_ID,
                filename="src.md",
                size_bytes=1,
                content_type="text/markdown",
                uploaded_by="u1",
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id="conv_1",
                enterprise_id=_DEFAULT_ENTERPRISE_ID,
                user_id="u1",
                status="completed",
                scope="global",
                source_file_id="file_1",
            )
        )
        session.add(
            ConversionDraftModel(
                id="draft_1",
                enterprise_id=_DEFAULT_ENTERPRISE_ID,
                conversion_id="conv_1",
                runbook_id="rb-1",
                title="T",
                file_path=rel,
                status="draft",
                validation_passed=True,
            )
        )
        await session.commit()

    service = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory,
        knowledge_service=None,
    )
    await service.update_draft(
        conversion_id="conv_1",
        draft_id="draft_1",
        user_id="u1",
        content=_crlf(valid_runbook("Redis Notes For The Cache Tier")),
        is_platform_admin=True,
    )

    on_disk = (tmp_path / rel).read_bytes().decode("utf-8")
    assert CR not in on_disk, repr(on_disk[:200])


@pytest.mark.asyncio
async def test_create_runbook_from_template_writes_lf(
    _session_factory, tmp_path, monkeypatch
):
    """``POST /knowledge/runbooks/create`` does not carry a document at all — it
    carries five free-text JSON fields that are interpolated into an LF markdown
    template here. A CRLF client therefore produces a MIXED-ending document, and
    ``causes`` is the field that decides the outcome: it has to supply the
    ``### Cause N:`` headings and ``**Statement:**`` sub-fields, every one of
    which is matched line-anchored.

    Asserts on the bytes written, because this method persists before it scores.

    Mutation: drop any of the five ``normalize_line_endings`` calls — this was
    added after a mutation run showed the site had NO guard while the other four
    layer-3 sites did.
    """
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )

    monkeypatch.chdir(tmp_path)
    service = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory,
        knowledge_service=None,
    )
    service._ensure_team_publish_allowed = AsyncMock()

    result = await service.create_runbook_from_template(
        # CR in `title` deliberately: it is one of the SEVEN fields the first
        # shape of this fix did not normalise (with domain, service_name,
        # symptom_class, severity, tags, difficulty), and it reaches BOTH the
        # frontmatter and the H1. A payload using only the five named free-text
        # fields re-certifies the partial fix.
        title="Redis Runs Out\r\nOf Memory",
        domain="database",
        service_name="redis",
        symptom_class=["resource_exhaustion"],
        severity="high",
        scope="global",
        tags=["redis"],
        difficulty="intermediate",
        symptom_recognition="Clients see OOM errors.\r\nLatency climbs.\r\n",
        applicability="Redis 6 and later.\r\n",
        diagnostic_steps="### Step 1: Check memory\r\nRun `INFO memory`.\r\n",
        causes=(
            "### Cause A: maxmemory reached\r\n"
            "**Statement:** the instance hit its maxmemory ceiling\r\n"
            "**Indicators:**\r\n- root: [Step 1] used_memory at maxmemory\r\n"
            "**Interventions:**\r\n- **remediation** (root): raise maxmemory\r\n"
        ),
        prevention="Alert at 80% of maxmemory.\r\n",
        user_id="u1",
        enterprise_id=_DEFAULT_ENTERPRISE_ID,
    )

    written = list((tmp_path / "data" / "knowledge").rglob("*.md"))
    assert len(written) == 1, written
    on_disk = written[0].read_bytes().decode("utf-8")
    assert CR not in on_disk, repr(on_disk[:300])
    assert "### Cause A: maxmemory reached\n" in on_disk
    assert "Redis Runs Out\nOf Memory" in on_disk

    # The RETURNED draft, not just the file. `write_runbook_file` normalises as
    # a backstop, so the on-disk assertion above can no longer tell whether this
    # method normalised -- but `content`, `content_preview` and `size_bytes` are
    # built from the in-memory string and handed straight back to the client, so
    # they can. Without this the service-level call is unguarded, which a
    # mutation run showed.
    draft = result["draft"]
    assert CR not in draft.content, repr(draft.content[:300])
    assert CR not in draft.content_preview, repr(draft.content_preview)


# --------------------------------------------------------------------------
# Found in review of this PR
# --------------------------------------------------------------------------


def test_a_bom_does_not_hide_the_frontmatter():
    """ "UTF-8 with BOM" is the default save of several Windows editors, so the
    real Windows artefact is BOM **and** CRLF — and fixing only the CRLF half
    left that file refused.

    A separate, PRE-EXISTING defect with a different mechanism: the frontmatter
    pattern is ``re.match``-anchored at offset 0, and both readers that produce
    text here preserve the BOM (only ``utf-8-sig`` strips one). Measured before
    the fix: ``passed=False``, ``['No YAML frontmatter found']``, score 94.0 ->
    80.5, a 422 from ``POST /knowledge/documents`` on a runbook carrying every
    section. BOM+LF failed identically, which is what shows it is not a
    line-ending bug.

    Mutation: drop ``{_BOM}`` from ``FRONTMATTER_RE``.
    """
    lf = _runbook_corpus()[0].read_text(encoding="utf-8")
    validator, scorer = RunbookValidator(), QualityScorer()

    for label, raw in (
        ("BOM+LF", ("﻿" + lf).encode("utf-8")),
        ("BOM+CRLF", ("﻿" + _crlf(lf)).encode("utf-8")),
    ):
        text = decode_text(raw)
        assert text[0] == "﻿", f"{label}: the BOM must be PRESERVED, not stripped"
        assert validator.validate_content(text).passed, label
        assert scorer.score_content(text).overall == scorer.score_content(lf).overall


def test_write_runbook_file_writes_lf_bytes(tmp_path):
    """Enumerating the writing services missed one — the LLM conversion draft at
    ``_convert_single_failure_mode`` wrote model output verbatim while the
    validate/score calls on the next line judged the normalised twin. An
    enumeration is only ever as good as the enumeration, so the guarantee lives
    at the choke point every runbook write already goes through.

    Asserted on the RAW BYTES, not the decoded text. Text mode defaults to
    ``newline=None``, which translates ``\n`` back to ``os.linesep`` on write,
    and ``read_text`` would translate it straight back and hide that — so on a
    non-LF host the whole fix is undone at the last step and a decoded
    assertion still passes.

    Mutation: the normalisation half of the write line. The ``newline="\n"``
    half is NOT observable on an LF host -- ``os.linesep`` is already ``"\n"``
    and cannot be faked for ``write_text`` -- so it is asserted by construction
    rather than by this test, and a mutation of it correctly kills nothing here.
    """
    from faultmaven.utils.runbook_id import write_runbook_file

    root = tmp_path / "data" / "knowledge"
    root.mkdir(parents=True)
    written = write_runbook_file(
        root / "global" / "rb.md",
        "## Causes\r\n### Cause A: x\r\nbody\rmore\n",
        source="test",
        root=root,
    )

    raw = written.read_bytes()
    assert b"\r" not in raw, repr(raw)
    assert raw == b"## Causes\n### Cause A: x\nbody\nmore\n"


def test_normalize_leaves_a_non_string_alone():
    """``PUT /knowledge/documents/{id}`` declares an untyped ``dict`` body, so
    ``content`` may be any JSON value. ``RedactionService.sanitize`` accepts
    ``int``/``float``/``bool`` and stringifies the rest, so raising here would
    turn a value the endpoint used to handle into a 500.

    The asymmetry is why it needs a test rather than a convention: ``"\\r" not
    in {...}`` is a KEY test, so a dict or list passed silently while an int
    raised ``TypeError``.

    Mutation: drop the ``isinstance`` half of the guard.
    """
    for value in (5, True, 3.5, None, {"a": 1}, ["x"], b"\r\n"):
        assert normalize_line_endings(value) is value


def test_the_two_pass_replace_agrees_with_the_regex_it_replaced():
    """The helper uses two ``str.replace`` passes rather than
    ``re.sub(r"\\r\\n?", ...)`` because the regex costs 2012 ms against 55 ms on
    10 MB of ``\\r``, and several call sites run on the event loop. Equivalence
    is the thing that makes that swap safe, so it is asserted rather than
    assumed — over the shapes that distinguish the two, including the
    ``\\r\\r\\n`` case that breaks the SINGLE-pass form.
    """
    import re

    reference = re.compile(r"\r\n?")
    for sample in (
        "a\r\nb",
        "a\rb",
        "a\r\r\nb",
        "a\n\rb",
        "\r",
        "\r\n\r\n",
        "\r\r\r",
        "no carriage returns here",
        "",
    ):
        assert normalize_line_endings(sample) == reference.sub("\n", sample), repr(
            sample
        )
