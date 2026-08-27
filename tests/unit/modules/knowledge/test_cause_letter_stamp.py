"""The seeder's join key is stored at index time, not re-derived at read time (fm#1108).

Retrieval used to name which of a runbook's ``metadata["causes"]`` records a hit
matched by re-running ``CAUSE_HEADING_RE`` over the hit's chunk text. That made
the join a function of the grammar **in force when the read happened** — and
``runbook_grammar`` is a manual mirror of kb-toolkit's that is *expected* to
change, with a cross-repo CI job requiring it. Nothing re-ingested on a grammar
change, so a code-only edit silently re-interpreted chunks already in the store
while their SQL record stayed as the old grammar had extracted it. All four
guards from #1101/#1103/#1106 stay quiet through that: the corpus tests only see
the curated corpus, the retrieval-side counter needs a letter to disagree with,
and both write-time counters run before the change happens.

Stamping the letters at index time pins the join to the moment both sides were
written. A later grammar change cannot reach back; it only changes future
ingests, where the write-time check runs — and it now forces a re-stamp of the
pack, because the grammar is part of the stamp identity.
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.models import KnowledgeBaseDocument
from faultmaven.models.vector_metadata import VectorMetadata
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    _carried_cause_letters,
    _read_stamped_cause_letters,
    _unrecorded_chunk_letters,
)

pytestmark = [pytest.mark.unit]

_EMBED_GUARD = "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise"
_UNSTAMPED = (
    "faultmaven.core.investigation.lifecycle_metrics.kb_cause_letters_unstamped_total"
)

_RUNBOOK = (
    "# Node drain stalls\n\n"
    "## Causes\n\n"
    "### Cause A: Pod disruption budget blocks eviction\n"
    "**Statement:** the PDB has no spare replica\n\n"
    "### Cause B: Terminating pod ignores SIGTERM\n"
    "**Statement:** the process traps and never exits\n"
)


def _cause(letter: str) -> dict:
    return {"cause_letter": letter, "cause_name": "Something broke"}


# ---------------------------------------------------------------------------
# The stamp is written, on every chunk, with "none" distinguishable from "never"
# ---------------------------------------------------------------------------


def test_an_empty_stamp_is_stored_but_an_absent_one_is_not():
    """The whole migration turns on this. ``""`` means "stamped, no cause heading
    in this chunk"; a missing key means "written before fm#1108". Dropping the
    empty string the way every other falsy field is dropped would collapse the
    two and make the fallback counter meaningless."""
    assert VectorMetadata(cause_letters="").to_chroma_metadata()["cause_letters"] == ""
    assert "cause_letters" not in VectorMetadata().to_chroma_metadata()
    assert (
        VectorMetadata(cause_letters="A,B").to_chroma_metadata()["cause_letters"]
        == "A,B"
    )


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
        title="Node drain stalls",
        content=content,
        document_type="runbook",
        tags=[],
        source_url=None,
        # Required since #1166 — this fixture is not about the tier;
        # "global" keeps it exercising exactly what it did before.
        scope="global",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


async def _index(service, document, **kwargs):
    async def _embed(texts, **_):
        return [[0.0, 1.0] for _ in texts]

    with patch(_EMBED_GUARD, new=AsyncMock(side_effect=_embed)):
        return await service._index_document_in_vector_store(document, **kwargs)


def _written_metadata(service):
    (doc_dicts,) = service._vector_store.add_documents.await_args.args
    return [d["metadata"] for d in doc_dicts]


@pytest.mark.asyncio
async def test_every_chunk_is_stamped_and_cause_chunks_carry_their_letters():
    service = _service()
    await _index(service, _document(_RUNBOOK), causes=[_cause("A"), _cause("B")])

    stamps = [m.get("cause_letters") for m in _written_metadata(service)]
    assert all(s is not None for s in stamps), "every chunk must carry the key"
    assert "A" in ",".join(stamps) and "B" in ",".join(stamps)


@pytest.mark.asyncio
async def test_prose_chunks_are_stamped_empty_rather_than_left_unstamped():
    """Uniform stamping is what lets key-absence mean exactly one thing. Stamping
    only cause-bearing chunks would make the fallback fire forever on ordinary
    prose and tell us nothing about the migration."""
    service = _service()
    await _index(service, _document("# Notes\n\nSome prose about nodes.\n"))

    stamps = [m.get("cause_letters") for m in _written_metadata(service)]
    assert stamps and all(s == "" for s in stamps)


@pytest.mark.asyncio
async def test_pack_supplied_chunks_are_stamped_too():
    """The pack path is the one that matters most in practice — it serves most
    retrievals — and it is the branch a stamp added after the chunking split
    would have missed."""
    service = _service()
    await _index(
        service,
        _document(_RUNBOOK),
        prechunked=[("## Causes\n\n### Cause A: x\n", [0.0, 1.0])],
        causes=[_cause("A")],
    )
    assert _written_metadata(service)[0]["cause_letters"] == "A"


# ---------------------------------------------------------------------------
# The read path prefers the stamp, and says so when it cannot
# ---------------------------------------------------------------------------


def test_a_stamped_hit_is_read_not_parsed():
    """The point of the change: the text is not consulted, so the grammar in
    force at read time cannot change the answer."""
    with patch(_UNSTAMPED) as counter:
        got = _read_stamped_cause_letters(
            {"cause_letters": "A,B"}, "### Cause Z: text that disagrees"
        )
    assert got == ["A", "B"]
    assert counter.inc.call_count == 0


def test_an_empty_stamp_means_no_letters_not_go_and_parse():
    with patch(_UNSTAMPED) as counter:
        got = _read_stamped_cause_letters({"cause_letters": ""}, "### Cause A: x")
    assert got == []
    assert counter.inc.call_count == 0, "an empty stamp is an ANSWER, not a gap"


def test_an_unstamped_hit_falls_back_to_parsing_and_is_counted():
    """Legacy chunks keep working exactly as before — strictly better for new
    data, worse for nothing — and each fallback is counted so the drain is
    observable and the fallback can eventually be deleted."""
    with patch(_UNSTAMPED) as counter:
        got = _read_stamped_cause_letters({"title": "x"}, "### Cause A: x")
    assert got == ["A"]
    assert counter.inc.call_count == 1


def test_missing_metadata_entirely_also_falls_back():
    with patch(_UNSTAMPED) as counter:
        assert _read_stamped_cause_letters(None, "### Cause A: x") == ["A"]
    assert counter.inc.call_count == 1


def test_a_broken_counter_cannot_break_retrieval():
    with patch(_UNSTAMPED) as counter:
        counter.inc.side_effect = RuntimeError("metrics gone")
        assert _read_stamped_cause_letters(None, "### Cause A: x") == ["A"]


@pytest.mark.asyncio
async def test_a_grammar_change_cannot_reinterpret_a_stamped_chunk():
    """The reproduction from #1108, run against the fix.

    Index under today's grammar, then serve a retrieval under a TIGHTENED one.
    Before the stamp, Cause A silently stopped being seedable. Now the answer is
    whatever was written, because nothing re-parses.
    """
    service = _service()
    authored = (
        "# Checkout 504s\n\n## Causes\n\n"
        "### Cause A: upstream pool exhausted\n**Statement:** all workers busy\n\n"
        "### Cause B: DNS resolution flapping\n**Statement:** resolver timeouts\n"
    )
    await _index(service, _document(authored), causes=[_cause("A"), _cause("B")])
    stamped = _written_metadata(service)
    chunk_text = service._vector_store.add_documents.await_args.args[0][0]["content"]

    from faultmaven.modules.knowledge.domain.services import runbook_grammar as g

    saved = g.CAUSE_HEADING_RE
    try:
        # Requires a title-cased cause name; the authored runbook's are lowercase.
        g.CAUSE_HEADING_RE = re.compile(r"^### Cause ([A-Z]): ([A-Z].*?)\s*$", re.M)
        read_stamped = _read_stamped_cause_letters(stamped[0], chunk_text)
        with patch(_UNSTAMPED):
            read_legacy = _read_stamped_cause_letters({}, chunk_text)
    finally:
        g.CAUSE_HEADING_RE = saved

    assert "A" in read_stamped, "the stamp must survive a later grammar change"
    assert "A" not in read_legacy, (
        "the un-stamped path must still show the old hazard — otherwise this "
        "test proves nothing about what the stamp bought"
    )


# ---------------------------------------------------------------------------
# The reverse disagreement, now decidable at write time
# ---------------------------------------------------------------------------


def test_a_chunk_letter_the_record_lacks_is_reported():
    """What ``kb_cause_seed_letter_mismatch_total`` reports from the far end,
    after a case has already been served without those seeds."""
    assert _unrecorded_chunk_letters([["A"], ["Z"]], [_cause("A")]) == ["Z"]


def test_a_miscased_record_letter_is_not_also_blamed_on_the_markdown():
    """One defect, one message. A record declaring ``"a"`` against a
    ``### Cause A:`` heading is a MIS-CASED RECORD — already reported, with
    wording that names the record as the thing to fix. Reporting ``A`` here too
    would say the markdown carries a letter the record lacks, sending a producer
    to inspect healthy markdown: the exact misdirection the caller's
    ungrammatical branch exists to prevent."""
    assert _unrecorded_chunk_letters([["A"]], [{"cause_letter": "a"}]) == []


def test_a_genuinely_undeclared_letter_survives_the_case_fold():
    """The guard above must not swallow the real thing it sits next to."""
    assert _unrecorded_chunk_letters([["A"], ["Z"]], [{"cause_letter": "a"}]) == ["Z"]


def test_a_document_with_no_record_is_not_alarmed():
    """An anonymous upload has cause headings and no record by design — it is
    never seedable for a reason that is not a defect. Alarming would fire on
    healthy content."""
    assert _unrecorded_chunk_letters([["A"], ["B"]], None) == []
    assert _unrecorded_chunk_letters([["A"]], []) == []


def test_chunk_text_handed_in_place_of_parsed_letters_is_refused():
    """The one wrong shape that is SILENT: ``set.update`` on a string iterates
    characters, and cause letters are single uppercase characters that occur in
    ordinary prose — so the result looks plausible and nothing reports it."""
    with pytest.raises(TypeError):
        _carried_cause_letters(["### Cause A: text"])


# ---------------------------------------------------------------------------
# restamp_document: the service half of the convergence path
# ---------------------------------------------------------------------------


def _restamp_service(row):
    service = KnowledgeService.__new__(KnowledgeService)
    service.reindex_missing_vectors = AsyncMock(return_value=4)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    service._db_session_factory = MagicMock(return_value=session)
    return service


@pytest.mark.asyncio
async def test_restamp_reindexes_a_published_row():
    row = MagicMock()
    row.is_published = True
    service = _restamp_service(row)
    assert await service.restamp_document("doc-1") == 4
    service.reindex_missing_vectors.assert_awaited_once_with("doc-1")


@pytest.mark.asyncio
async def test_restamp_refuses_an_unpublished_row():
    """Retrieval ignores ``is_published``, so a retired runbook is one whose
    vectors were deleted. Writing fresh ones would return it to investigations
    on the strength of a maintenance pass — and there are no chunks on it to
    re-stamp in the first place."""
    row = MagicMock()
    row.is_published = False
    service = _restamp_service(row)
    assert await service.restamp_document("doc-1") == 0
    service.reindex_missing_vectors.assert_not_awaited()


@pytest.mark.asyncio
async def test_restamp_of_a_vanished_row_is_a_no_op():
    service = _restamp_service(None)
    assert await service.restamp_document("doc-1") == 0
    service.reindex_missing_vectors.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_stamp_is_recorded_only_after_the_vectors_are_written():
    """Recording it first would mark a row converged whose chunks were never
    rewritten — it would then be skipped forever, with the legacy parse serving
    it and nothing saying so."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._record_chunk_stamp = AsyncMock()
    service._vector_store = MagicMock()
    service._index_document_in_vector_store = AsyncMock(return_value=0)

    row = MagicMock()
    row.item_id = "doc-1"
    row.title = "t"
    row.content = "# c\n"
    row.item_type = "runbook"
    row.tags = []
    row.source_url = None
    row.scope = "global"
    row.owner_id = None
    row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.knowledge_metadata = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    service._db_session_factory = MagicMock(return_value=session)

    assert await service.reindex_missing_vectors("doc-1") == 0
    service._record_chunk_stamp.assert_not_awaited()

    service._index_document_in_vector_store = AsyncMock(return_value=3)
    assert await service.reindex_missing_vectors("doc-1") == 3
    service._record_chunk_stamp.assert_awaited_once_with("doc-1")


# ---------------------------------------------------------------------------
# Recording the stamp must not destroy metadata it could not read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_metadata_blob_is_never_overwritten_by_the_stamp(caplog):
    """Recording the stamp is read-modify-write, and the decoder answers ``{}``
    for an unreadable value — so writing would replace a corrupt blob with
    ``{"chunk_stamp": ...}``, destroying whatever causes record it held and
    leaving the row looking healthy afterwards. It was already unusable to every
    reader, but unrecoverable and unusable are different things.
    """
    service = KnowledgeService.__new__(KnowledgeService)
    row = MagicMock()
    row.knowledge_metadata = '{"causes": [{"cause_letter": "A"}'  # truncated
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    service._db_session_factory = MagicMock(return_value=session)

    with caplog.at_level("ERROR"):
        await service._record_chunk_stamp("doc-1")

    session.commit.assert_not_awaited()
    assert row.knowledge_metadata == '{"causes": [{"cause_letter": "A"}'
    assert any(
        "UNREADABLE KNOWLEDGE METADATA doc-1" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_readable_row_still_gets_its_stamp_recorded():
    """The converse, so the guard above cannot become a blanket refusal."""
    import json as _json

    service = KnowledgeService.__new__(KnowledgeService)
    row = MagicMock()
    row.knowledge_metadata = _json.dumps({"causes": [{"cause_letter": "A"}]})
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    service._db_session_factory = MagicMock(return_value=session)

    await service._record_chunk_stamp("doc-1")

    session.commit.assert_awaited_once()
    written = _json.loads(row.knowledge_metadata)
    assert written["chunk_stamp"]
    assert written["causes"] == [{"cause_letter": "A"}], "the record must survive"
