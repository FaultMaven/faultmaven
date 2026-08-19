"""A causes record must be recoverable from the chunks stored beside it (fm#1103).

The KB cause seeder's join is derived, not stored. Retrieval hands it a chunk;
it decides which of the parent runbook's ``metadata["causes"]`` records that hit
names by parsing ``### Cause X:`` out of the chunk's own text and matching the
letter against ``cause_letter``. Nothing wrote that correspondence down, so
nothing checked it — a cause whose letter appears in no chunk is unseedable
permanently, silently, and no matter how often the runbook is retrieved.

#1092 is what made that reachable: the old author-order fallback seeded a matched
runbook's first N causes regardless of any join, which masked a broken one. With
the fallback gone (deliberately — it was the defect), the join is load-bearing.

The shipped pack is pinned against it by a corpus test. The other two write
surfaces are not:

* a verified case->runbook conversion, whose record comes from ``extract_causes``
  and whose chunks come from the runtime ``ContentChunker``;
* an out-of-tree pack (``KB_PACK_DIR``), whose chunks AND record both come from a
  kb-toolkit build no test in this repo can see.

and a third re-pairs them after the fact: a boot-time re-index, which re-chunks
with the runtime chunker under a record the pack wrote.

The retrieval-side counter added in #1101 sees this only after a case has already
lost its seeds, only once the runbook is retrieved, and only for the
heading-present-but-wrong-letter shape — a missing heading yields no letter at
all and never trips it. These tests pin the write-time check that closes both
shapes at the one moment both sides are in hand.
"""

import glob
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.models import KnowledgeBaseDocument
from faultmaven.modules.knowledge.domain.services.content_chunker import ContentChunker
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    _row_causes,
    _unrecoverable_cause_letters,
)
from faultmaven.modules.knowledge.domain.services.runbook_cause_extractor import (
    extract_causes,
)

pytestmark = [pytest.mark.unit]

_COUNTER = (
    "faultmaven.core.investigation.lifecycle_metrics."
    "kb_cause_unseedable_at_ingest_total"
)
_EMBED_GUARD = "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise"


def _cause(letter: str, name: str = "Something broke") -> dict:
    return {"cause_letter": letter, "cause_name": name}


def _chunk(letter: str) -> str:
    return f"### Cause {letter}: Something broke\n**Statement:** it did"


# ---------------------------------------------------------------------------
# The predicate: which declared letters can no chunk recover
# ---------------------------------------------------------------------------


def test_every_declared_letter_present_is_clean():
    chunks = ["## Causes\n\n" + _chunk("A"), _chunk("B")]
    assert _unrecoverable_cause_letters(chunks, [_cause("A"), _cause("B")]) == []


def test_a_letter_no_chunk_carries_is_reported():
    """The shape the retrieval-side counter is structurally blind to: the heading
    is missing everywhere, so retrieval produces no letter to disagree with."""
    chunks = ["## Causes\n\n" + _chunk("A"), _chunk("B")]
    missing = _unrecoverable_cause_letters(
        chunks, [_cause("A"), _cause("B"), _cause("C")]
    )
    assert missing == ["C"]


def test_a_drifted_heading_form_reports_every_letter():
    """A producer whose heading form left the shared grammar (``#### Cause A:``)
    recovers nothing — the record looks well-formed, the chunks join to nothing."""
    chunks = ["#### Cause A: Something broke", "Cause B - Something else"]
    assert _unrecoverable_cause_letters(chunks, [_cause("A"), _cause("B")]) == [
        "A",
        "B",
    ]


def test_missing_letters_keep_record_order_and_dedupe():
    causes = [_cause("D"), _cause("B"), _cause("D"), _cause("A")]
    assert _unrecoverable_cause_letters([_chunk("B")], causes) == ["D", "A"]


def test_a_chunk_heading_absent_from_the_record_is_not_this_check():
    """The opposite direction is the seeder's own alarm
    (``kb_cause_seed_letter_mismatch_total``) and is not double-counted here: a
    stray heading costs no cause its seedability."""
    chunks = [_chunk("A"), _chunk("Z")]
    assert _unrecoverable_cause_letters(chunks, [_cause("A")]) == []


@pytest.mark.parametrize("causes", [None, [], [{}], [{"cause_letter": ""}], ["A"]])
def test_records_declaring_no_usable_letter_report_nothing(causes):
    """Deliberate scope. A letterless or malformed entry is also unseedable, but
    it is a broken record rather than a record/chunk disagreement — the extractor
    and the runbook validator own it, and folding it in would fire this alarm for
    a defect its message cannot describe."""
    assert _unrecoverable_cause_letters([], causes) == []


def test_a_record_with_no_chunks_at_all_reports_every_letter():
    assert _unrecoverable_cause_letters([], [_cause("A"), _cause("B")]) == ["A", "B"]


# ---------------------------------------------------------------------------
# Reading the record off a raw row (JsonBlob is str on SQLite, dict on JSONB)
# ---------------------------------------------------------------------------


def test_row_causes_reads_the_json_string_shape():
    raw = json.dumps({"causes": [_cause("A")]})
    assert _row_causes(raw) == [_cause("A")]


def test_row_causes_reads_the_decoded_dict_shape():
    """Handling only the string shape loses the record on every JSONB
    deployment — the failure ``kb_init._decode_metadata`` was written for."""
    assert _row_causes({"causes": [_cause("A")]}) == [_cause("A")]


@pytest.mark.parametrize(
    "raw", [None, "", "{}", {}, "not json", "[]", json.dumps({"causes": "A"})]
)
def test_row_causes_returns_none_for_anything_without_a_causes_list(raw):
    assert _row_causes(raw) is None


# ---------------------------------------------------------------------------
# The check fires where both sides are in hand — the indexing seam
# ---------------------------------------------------------------------------


def _service() -> KnowledgeService:
    service = KnowledgeService.__new__(KnowledgeService)
    vector_store = MagicMock()
    vector_store.delete_documents_by_parent_id = AsyncMock()
    vector_store.add_documents = AsyncMock()
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})
    return service


def _document(content: str) -> KnowledgeBaseDocument:
    return KnowledgeBaseDocument(
        document_id="doc-1",
        title="Draining a node",
        content=content,
        document_type="runbook",
        tags=[],
        source_url=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


_RUNBOOK = (
    "# Node drain stalls\n\n"
    "## Causes\n\n"
    "### Cause A: Pod disruption budget blocks eviction\n"
    "**Statement:** the PDB has no spare replica\n\n"
    "### Cause B: Terminating pod ignores SIGTERM\n"
    "**Statement:** the process traps and never exits\n"
)


async def _index(service, document, **kwargs):
    async def _embed(texts, **_):
        return [[0.0, 1.0] for _ in texts]

    with patch(_EMBED_GUARD, new=AsyncMock(side_effect=_embed)):
        return await service._index_document_in_vector_store(document, **kwargs)


@pytest.mark.asyncio
async def test_runtime_chunked_record_that_outruns_its_chunks_is_counted():
    service = _service()
    with patch(_COUNTER) as counter:
        await _index(
            service,
            _document(_RUNBOOK),
            causes=[_cause("A"), _cause("B"), _cause("C")],
        )
    counter.labels.assert_called_once_with(chunker="runtime")
    assert counter.labels.return_value.inc.call_count == 1


@pytest.mark.asyncio
async def test_pack_chunks_are_labeled_as_the_pack_chunker():
    """The vendored pack is pinned by a corpus test, so a fire on this label means
    an out-of-tree ``KB_PACK_DIR`` built by a drifted kb-toolkit — a different
    producer to chase than a conversion, which is why the label exists."""
    service = _service()
    with patch(_COUNTER) as counter:
        await _index(
            service,
            _document(_RUNBOOK),
            prechunked=[("## Causes\n\n" + _chunk("A"), [0.0, 1.0])],
            causes=[_cause("A"), _cause("B")],
        )
    counter.labels.assert_called_once_with(chunker="pack")


@pytest.mark.asyncio
async def test_an_agreeing_document_counts_nothing():
    service = _service()
    with patch(_COUNTER) as counter:
        await _index(service, _document(_RUNBOOK), causes=[_cause("A"), _cause("B")])
    assert counter.labels.call_count == 0


@pytest.mark.asyncio
async def test_a_document_without_a_causes_record_counts_nothing():
    """The overwhelmingly common case — flat prose, an uploaded document, any
    non-runbook. The check must cost nothing and say nothing for them."""
    service = _service()
    with patch(_COUNTER) as counter:
        await _index(service, _document("# Notes\n\nSome prose about nodes."))
    assert counter.labels.call_count == 0


@pytest.mark.asyncio
async def test_the_document_is_still_indexed_when_the_check_fires():
    """Observed, never enforced. Refusing the write would turn a recall loss into
    a failed ingest — on the pack path, a failed KB bootstrap."""
    service = _service()
    with patch(_COUNTER):
        chunks_created = await _index(
            service, _document(_RUNBOOK), causes=[_cause("A"), _cause("C")]
        )
    assert chunks_created > 0
    assert service._vector_store.add_documents.await_count == 1


@pytest.mark.asyncio
async def test_a_broken_check_cannot_fail_the_write_it_observes():
    service = _service()
    with patch(
        "faultmaven.modules.knowledge.domain.services.knowledge_service."
        "_unrecoverable_cause_letters",
        side_effect=RuntimeError("boom"),
    ):
        chunks_created = await _index(
            service, _document(_RUNBOOK), causes=[_cause("A")]
        )
    assert chunks_created > 0


@pytest.mark.asyncio
async def test_the_warning_names_the_document_and_the_missing_letters(caplog):
    service = _service()
    with patch(_COUNTER):
        with caplog.at_level("WARNING"):
            await _index(
                service, _document(_RUNBOOK), causes=[_cause("A"), _cause("C")]
            )
    warning = "\n".join(
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )
    assert "doc-1" in warning
    assert "C" in warning


# ---------------------------------------------------------------------------
# Both write surfaces reach the seam with their record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_runbook_hands_its_record_to_the_check():
    """The acquisition point for both the pack path (``kb_init``) and a verified
    conversion (``verify_draft``) — the record exists only as an argument here,
    so nothing downstream could recover it."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = MagicMock()
    service._db_session_factory = MagicMock()
    service._create_team_share = AsyncMock()
    service._index_document_in_vector_store = AsyncMock(return_value=3)
    causes = [_cause("A")]

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory.return_value = session

    with patch(
        "faultmaven.modules.knowledge.infrastructure.persistence."
        "knowledge_item_repository.DatabaseKnowledgeItemRepository"
    ) as repo:
        repo.return_value.create = AsyncMock()
        await service.ingest_runbook(
            document_id="doc-1",
            title="Node drain stalls",
            content=_RUNBOOK,
            organization_id=None,
            causes=causes,
        )

    assert service._index_document_in_vector_store.await_args.kwargs["causes"] is causes


@pytest.mark.asyncio
async def test_reindex_pairs_the_rows_record_with_freshly_derived_chunks():
    """A boot repair re-chunks with the RUNTIME chunker while the row keeps the
    record it was ingested with. For a pack runbook that is the one place our
    chunker and kb-toolkit's record meet, so it is checked like an ingest."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = MagicMock()
    service._index_document_in_vector_store = AsyncMock(return_value=2)

    row = MagicMock()
    row.item_id = "doc-1"
    row.title = "Node drain stalls"
    row.content = _RUNBOOK
    row.item_type = "runbook"
    row.tags = []
    row.source_url = None
    row.scope = "global"
    row.owner_id = None
    row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.knowledge_metadata = json.dumps({"causes": [_cause("A"), _cause("B")]})

    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    service._db_session_factory = MagicMock(return_value=session)

    await service.reindex_missing_vectors("doc-1")

    assert service._index_document_in_vector_store.await_args.kwargs["causes"] == [
        _cause("A"),
        _cause("B"),
    ]


@pytest.mark.asyncio
async def test_editing_a_published_runbook_rechecks_its_standing_record():
    """An edit re-chunks the content and leaves ``metadata["causes"]`` untouched,
    so rewriting a Causes section is the everyday way for the two to stop
    agreeing — the most reachable of the surfaces, and the one a corpus test can
    never see."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = MagicMock()
    service._index_document_in_vector_store = AsyncMock(return_value=2)
    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)

    item = MagicMock()
    item.item_id = "doc-1"
    item.title = "Node drain stalls"
    item.content = _RUNBOOK
    item.item_type = MagicMock(value="runbook")
    item.tags = []
    item.source_url = None
    item.scope = MagicMock(value="global")
    item.owner_id = None
    item.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item.is_published = True
    item.metadata = {"causes": [_cause("A"), _cause("B"), _cause("C")]}

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=item)
    repo.update = AsyncMock(return_value=item)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    with patch(
        "faultmaven.modules.knowledge.infrastructure.persistence."
        "knowledge_item_repository.DatabaseKnowledgeItemRepository",
        return_value=repo,
    ):
        with patch.object(
            KnowledgeService, "_document_dto", MagicMock(return_value={})
        ):
            await service.update_document_metadata(
                document_id="doc-1", content=_RUNBOOK + "\n<!-- edited -->\n"
            )

    assert service._index_document_in_vector_store.await_args.kwargs["causes"] == [
        _cause("A"),
        _cause("B"),
        _cause("C"),
    ]


# ---------------------------------------------------------------------------
# Corpus: the RUNTIME chunker over the shipped runbooks
# ---------------------------------------------------------------------------


def test_runtime_chunker_recovers_every_cause_letter_of_every_shipped_runbook():
    """The corpus guard's missing half.

    ``test_shipped_pack_chunks_recover_every_cause_letter`` pins the pack's
    BUILD-TIME chunks. Every other write surface chunks with ``ContentChunker``
    in this process, and nothing pinned that pairing — so a change to the chunker
    (or to the extractor it must agree with) could make authored runbooks
    unseedable while the pack corpus stayed green.

    Runs the authored-runbook pipeline end to end over the same 91 markdown
    sources: ``extract_causes`` for the record, ``ContentChunker`` for the
    chunks, and the seeder's own recovery predicate to join them.
    """
    root = Path(__file__).resolve().parents[4]
    sources = sorted(
        glob.glob(str(root / "resources/knowledge/runbooks/**/*.md"), recursive=True)
    )
    if not sources:  # pragma: no cover - runbooks always vendored
        pytest.skip("shipped runbooks not vendored in this checkout")

    chunker = ContentChunker()
    checked = 0
    for source in sources:
        content = Path(source).read_text(encoding="utf-8")
        causes = extract_causes(content)
        if not causes:
            continue
        checked += 1
        missing = _unrecoverable_cause_letters(chunker.split(content), causes)
        assert not missing, (
            f"{Path(source).name}: the runtime chunker leaves cause(s) "
            f"{missing} with no chunk carrying their heading — unseedable"
        )
    assert checked > 0, "no shipped runbook carried an extractable causes record"
