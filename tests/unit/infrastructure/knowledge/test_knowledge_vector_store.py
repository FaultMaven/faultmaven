"""Tests for KnowledgeVectorStore — reranker, keyword extraction, chunking."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    RERANK_WEIGHT_FRESHNESS,
    RERANK_WEIGHT_FRESHNESS_ID,
    RERANK_WEIGHT_METADATA,
    RERANK_WEIGHT_METADATA_ID,
    RERANK_WEIGHT_TERM_OVERLAP,
    RERANK_WEIGHT_TERM_OVERLAP_ID,
    RERANK_WEIGHT_VECTOR,
    RERANK_WEIGHT_VECTOR_ID,
    SCOPE_FILTER_KEYS,
    SCOPE_PRIORITY,
    KnowledgeVectorStore,
)

#: The tenant stamp every KB chunk must carry since #1168 — ``add_documents``
#: refuses a KB write without it, so a KB-collection write in these tests that
#: is about something else carries it.
_ENTERPRISE = "ent-0001"
#: A case-evidence collection: scoped by case, never stamped, and so the
#: collection the "no metadata" normalisation tests write to.
_CASE_COLLECTION = "case_abc123"


def _make_result(
    id: str,
    score: float,
    content: str = "",
    scope: str = "global",
    domain: str = "",
    service: str = "",
    status: str = "",
    last_updated: str = "",
) -> dict:
    metadata = {"scope": scope}
    if domain:
        metadata["domain"] = domain
    if service:
        metadata["service"] = service
    if status:
        metadata["status"] = status
    if last_updated:
        metadata["last_updated"] = last_updated
    return {
        "id": id,
        "content": content or f"Content for {id}",
        "metadata": metadata,
        "score": score,
    }


class TestExtractSearchKeywords:
    """Tests for _extract_search_keywords static method."""

    def test_filters_stop_words(self):
        keywords = KnowledgeVectorStore._extract_search_keywords(
            "what is the standard approach for handling timeouts"
        )
        assert "what" not in keywords
        assert "the" not in keywords
        assert "timeouts" in keywords

    def test_filters_short_tokens(self):
        keywords = KnowledgeVectorStore._extract_search_keywords("a is on to at OOM")
        assert "OOM" in keywords
        assert "a" not in keywords

    def test_prioritizes_identifiers(self):
        keywords = KnowledgeVectorStore._extract_search_keywords(
            "CrashLoopBackOff error in payment-gateway-svc pod"
        )
        assert keywords[0] in ("CrashLoopBackOff", "payment-gateway-svc")

    def test_handles_error_codes(self):
        keywords = KnowledgeVectorStore._extract_search_keywords(
            "SQLSTATE-42000 connection refused"
        )
        assert keywords[0] == "SQLSTATE-42000"

    def test_empty_query(self):
        assert KnowledgeVectorStore._extract_search_keywords("") == []


class TestComputeTermOverlap:
    """Tests for term overlap scoring (NOT BM25 — binary presence check)."""

    def test_all_terms_present(self):
        score = KnowledgeVectorStore._compute_term_overlap(
            ["memory", "leak", "java"],
            "Java application has a memory leak causing OOM",
        )
        assert score == 1.0

    def test_no_terms_present(self):
        score = KnowledgeVectorStore._compute_term_overlap(
            ["kubernetes", "pod", "crash"],
            "Database connection timeout after 30 seconds",
        )
        assert score == 0.0

    def test_partial_terms(self):
        score = KnowledgeVectorStore._compute_term_overlap(
            ["memory", "leak", "kubernetes"],
            "Memory usage is high but no leak detected",
        )
        # "memory" and "leak" present, "kubernetes" absent → 2/3
        assert abs(score - 2 / 3) < 0.01

    def test_empty_query_terms(self):
        assert KnowledgeVectorStore._compute_term_overlap([], "any content") == 0.0

    def test_case_insensitive(self):
        score = KnowledgeVectorStore._compute_term_overlap(
            ["oom", "error"],
            "OOM Error in production",
        )
        assert score == 1.0


class TestComputeMetadataScore:
    """Tests for metadata-based scoring signals."""

    def test_domain_match(self):
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "networking"},
            {"domain": "networking"},
        )
        assert score >= 0.3

    def test_service_match(self):
        score = KnowledgeVectorStore._compute_metadata_score(
            {"service": "kubernetes"},
            {"service": "kubernetes"},
        )
        assert score >= 0.3

    def test_domain_and_service_match(self):
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "compute", "service": "kubernetes"},
            {"domain": "compute", "service": "kubernetes"},
        )
        assert score >= 0.6

    def test_no_context_metadata(self):
        """No case context means no domain/service boost.

        "No boost" is now the NEUTRAL point of the mapped range, not 0.0. The
        components sum on [-0.3, 1.0] and are mapped affinely onto [0, 1] so
        that the demotion half of the status signal survives; 0.0 is reserved
        for the most-demoted state (#1272). What this test pins is that an
        unboosted chunk scores exactly what a status-less, context-less chunk
        scores — no more, no less.
        """
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "networking"}, {}
        )
        assert score == KnowledgeVectorStore._compute_metadata_score({}, {})
        assert (
            0.0
            < score
            < KnowledgeVectorStore._compute_metadata_score({"status": "verified"}, {})
        )

    def test_verified_status_boost(self):
        score = KnowledgeVectorStore._compute_metadata_score({"status": "verified"}, {})
        assert score >= 0.4

    def test_deprecated_status_penalty(self):
        """Deprecated content should score lower than no-status content."""
        deprecated = KnowledgeVectorStore._compute_metadata_score(
            {"status": "deprecated"}, {}
        )
        no_status = KnowledgeVectorStore._compute_metadata_score({}, {})
        assert deprecated < no_status or deprecated == 0.0

    def test_domain_mismatch_no_boost(self):
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "networking"},
            {"domain": "database"},
        )
        assert score == KnowledgeVectorStore._compute_metadata_score({}, {})
        assert score < KnowledgeVectorStore._compute_metadata_score(
            {"domain": "networking"}, {"domain": "networking"}
        )

    def test_service_match_is_case_insensitive_and_trimmed(self):
        """Free-text case service ("PostgreSQL") must match curated frontmatter
        ("postgresql") — a raw == would miss the most common alignment."""
        score = KnowledgeVectorStore._compute_metadata_score(
            {"service": "postgresql"},
            {"service": "  PostgreSQL "},
        )
        assert score >= 0.3

    def test_domain_match_is_case_insensitive(self):
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "Networking"},
            {"domain": "networking"},
        )
        assert score >= 0.3


class TestComputeFreshnessScore:
    """Tests for staleness decay scoring."""

    def test_recent_content_high_score(self):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        score = KnowledgeVectorStore._compute_freshness_score({"last_updated": today})
        assert score > 0.9

    def test_old_content_low_score(self):
        score = KnowledgeVectorStore._compute_freshness_score(
            {"last_updated": "2020-01-01"}
        )
        assert score < 0.5

    def test_no_last_updated_neutral(self):
        """Missing last_updated should give neutral score, not penalize."""
        score = KnowledgeVectorStore._compute_freshness_score({})
        assert score == 0.5

    def test_invalid_date_neutral(self):
        score = KnowledgeVectorStore._compute_freshness_score(
            {"last_updated": "not-a-date"}
        )
        assert score == 0.5

    def test_iso_format(self):
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        score = KnowledgeVectorStore._compute_freshness_score({"last_updated": recent})
        assert score > 0.8


class TestRerank:
    """Tests for the full reranking pipeline."""

    def test_verified_content_ranked_higher(self):
        """Verified chunks should outrank unverified at similar vector scores."""
        candidates = [
            _make_result("unverified", 0.85, content="memory leak troubleshooting"),
            _make_result(
                "verified",
                0.83,
                content="memory leak troubleshooting guide",
                status="verified",
                last_updated="2026-03-01",
            ),
        ]
        query_terms = ["memory", "leak", "troubleshooting"]
        reranked = KnowledgeVectorStore._rerank(candidates, query_terms, {})

        assert reranked[0]["id"] == "verified"

    def test_domain_match_boosts_ranking(self):
        """Chunk matching case domain should rank higher."""
        candidates = [
            _make_result(
                "wrong_domain",
                0.85,
                domain="database",
                content="connection pool exhaustion",
            ),
            _make_result(
                "right_domain",
                0.82,
                domain="networking",
                content="connection pool exhaustion",
            ),
        ]
        query_terms = ["connection", "pool", "exhaustion"]
        reranked = KnowledgeVectorStore._rerank(
            candidates, query_terms, {"domain": "networking"}
        )
        assert reranked[0]["id"] == "right_domain"

    def test_stale_content_ranked_lower(self):
        """Very old content should rank below fresh content."""
        candidates = [
            _make_result(
                "stale", 0.85, content="how to fix OOM", last_updated="2020-01-01"
            ),
            _make_result(
                "fresh",
                0.83,
                content="how to fix OOM errors",
                last_updated="2026-03-01",
            ),
        ]
        query_terms = ["fix", "oom"]
        reranked = KnowledgeVectorStore._rerank(candidates, query_terms, {})
        assert reranked[0]["id"] == "fresh"

    def test_high_term_overlap_boosts(self):
        """Chunk containing all query terms should outrank one with fewer."""
        candidates = [
            _make_result("partial", 0.88, content="kubernetes deployment is failing"),
            _make_result(
                "full_overlap",
                0.85,
                content="kubernetes pod CrashLoopBackOff restart failure",
            ),
        ]
        query_terms = ["kubernetes", "pod", "crashloopbackoff", "restart"]
        reranked = KnowledgeVectorStore._rerank(candidates, query_terms, {})
        assert reranked[0]["id"] == "full_overlap"

    def test_scope_tiebreaking(self):
        """At equal rerank scores, personal > team > global."""
        # Identical content, scores, metadata — only scope differs
        candidates = [
            _make_result("global_1", 0.90, scope="global", content="fix X"),
            _make_result("personal_1", 0.90, scope="personal", content="fix X"),
        ]
        reranked = KnowledgeVectorStore._rerank(candidates, ["fix"], {})
        assert reranked[0]["id"] == "personal_1"

    def test_empty_candidates(self):
        assert KnowledgeVectorStore._rerank([], ["test"], {}) == []


class TestDeduplicateCandidates:
    """Tests for candidate deduplication."""

    def test_keeps_higher_scoring_duplicate(self):
        vector = [_make_result("dup", 0.80)]
        keyword = [_make_result("dup", 0.90)]
        merged = KnowledgeVectorStore._deduplicate_candidates(vector, keyword)
        assert len(merged) == 1
        assert merged[0]["score"] == 0.90

    def test_merges_unique_results(self):
        vector = [_make_result("a", 0.90)]
        keyword = [_make_result("b", 0.85)]
        merged = KnowledgeVectorStore._deduplicate_candidates(vector, keyword)
        assert len(merged) == 2

    def test_empty_inputs(self):
        assert KnowledgeVectorStore._deduplicate_candidates([], []) == []


class TestRerankWeights:
    """Verify reranker weight configuration."""

    def test_default_weights_sum_to_one(self):
        total = (
            RERANK_WEIGHT_VECTOR
            + RERANK_WEIGHT_TERM_OVERLAP
            + RERANK_WEIGHT_METADATA
            + RERANK_WEIGHT_FRESHNESS
        )
        assert abs(total - 1.0) < 0.001

    def test_identifier_weights_sum_to_one(self):
        total = (
            RERANK_WEIGHT_VECTOR_ID
            + RERANK_WEIGHT_TERM_OVERLAP_ID
            + RERANK_WEIGHT_METADATA_ID
            + RERANK_WEIGHT_FRESHNESS_ID
        )
        assert abs(total - 1.0) < 0.001

    def test_default_vector_is_largest(self):
        assert RERANK_WEIGHT_VECTOR > RERANK_WEIGHT_TERM_OVERLAP

    def test_identifier_term_overlap_is_largest(self):
        """For identifier queries, term overlap should dominate."""
        assert RERANK_WEIGHT_TERM_OVERLAP_ID > RERANK_WEIGHT_VECTOR_ID


class TestDynamicWeights:
    """Tests for dynamic weight shifting based on query identifiers."""

    def test_identifier_query_boosts_term_overlap(self):
        """CrashLoopBackOff should make term overlap matter more."""
        candidates = [
            _make_result(
                "semantic_match",
                0.90,
                content="container restart issues and pod scheduling",
            ),
            _make_result(
                "exact_match", 0.80, content="CrashLoopBackOff error in kubernetes pod"
            ),
        ]
        # With identifier query, term overlap (40%) beats vector (25%)
        reranked = KnowledgeVectorStore._rerank(
            candidates,
            ["crashloopbackoff", "kubernetes", "pod"],
            {},
            query="CrashLoopBackOff in my kubernetes pod",
        )
        assert reranked[0]["id"] == "exact_match"

    def test_natural_language_query_prefers_vector(self):
        """Natural language queries should keep vector weight dominant.

        When both candidates have similar term overlap, the higher vector
        score should win because vector weight (40%) > term overlap (25%).
        """
        candidates = [
            _make_result(
                "high_vector", 0.92, content="diagnose memory leak in java service"
            ),
            _make_result(
                "lower_vector", 0.75, content="fix memory leak in python service"
            ),
        ]
        reranked = KnowledgeVectorStore._rerank(
            candidates,
            ["memory", "leak", "service"],
            {},
            query="how to fix memory leak in service",
        )
        # Both have 3/3 term overlap → tied. Vector score breaks it.
        assert reranked[0]["id"] == "high_vector"

    def test_no_query_uses_defaults(self):
        """Empty query string should use default weights."""
        candidates = [_make_result("a", 0.90, content="test content")]
        result = KnowledgeVectorStore._rerank(candidates, ["test"], {}, query="")
        assert len(result) == 1


class TestHardMetadataFilter:
    """Tests for _apply_hard_metadata_filter."""

    def test_adds_domain_to_where(self):
        where = {"scope": "global"}
        result = KnowledgeVectorStore._apply_hard_metadata_filter(
            where, {"domain": "database"}
        )
        assert "$and" in result

    def test_adds_domain_and_service(self):
        where = {"scope": "global"}
        result = KnowledgeVectorStore._apply_hard_metadata_filter(
            where, {"domain": "database", "service": "postgresql"}
        )
        assert "$and" in result
        # Should have 3 conditions: original where + domain + service
        assert len(result["$and"]) == 3

    def test_no_context_returns_original(self):
        where = {"scope": "global"}
        result = KnowledgeVectorStore._apply_hard_metadata_filter(where, {})
        assert result == where

    def test_none_where_with_context(self):
        result = KnowledgeVectorStore._apply_hard_metadata_filter(
            None, {"domain": "compute"}
        )
        assert result == {"domain": "compute"}


class TestScopePriority:
    """Verify scope priority constants."""

    def test_personal_highest_priority(self):
        assert SCOPE_PRIORITY["personal"] < SCOPE_PRIORITY["team"]
        assert SCOPE_PRIORITY["team"] < SCOPE_PRIORITY["global"]


class _FakeCollection:
    """Minimal ChromaDB collection: in-memory chunks keyed by id with a
    parent_document_id in metadata. Supports the get(where=) equality + ``$in``,
    get(include=[]) id-only scan, and delete(ids=) surface the lifecycle methods
    rely on. Metadata is optional per chunk (some chunks may lack it — id-derived
    enumeration must still find them)."""

    def __init__(self, chunks):
        # chunks: list of (chunk_id, parent_document_id) — parent only used by
        # the metadata-based where filter; enumeration derives it from the id.
        self._ids = {cid: parent for cid, parent in chunks}

    def get(self, where=None, include=None):
        if where and "parent_document_id" in where:
            cond = where["parent_document_id"]
            if isinstance(cond, dict) and "$in" in cond:
                targets = set(cond["$in"])
                ids = [cid for cid, p in self._ids.items() if p in targets]
            else:
                ids = [cid for cid, p in self._ids.items() if p == cond]
            return {"ids": ids}
        # full scan — include=[] returns ids only (the path list_* uses).
        return {"ids": list(self._ids)}

    def delete(self, ids=None):
        for cid in ids or []:
            self._ids.pop(cid, None)


class _FakeClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, name, metadata=None):
        return self._collection

    def get_collection(self, name):
        return self._collection


class TestAddDocumentsAllowlistRefusal:
    """fm#1035: the production KB writer refuses undeclared metadata keys.

    ``VectorMetadata`` is an allowlist; the sibling ``ChromaDBVectorStore``
    already refuses undeclared keys, but this class — the writer the container
    wires into ``KnowledgeService`` whenever the KB Chroma client exists —
    sanitized inline with no allowlist check, so a key the schema does not
    declare vanished silently and read back later as the reader's fallback
    (the #912 defect, on the LIVE write path).
    """

    def _store(self):
        store = KnowledgeVectorStore(client=MagicMock())
        collection = MagicMock()
        store._get_or_create_collection = MagicMock(return_value=collection)
        return store, collection

    @pytest.mark.asyncio
    async def test_declared_keys_still_write(self):
        """Positive control: the guard does not fail closed on good metadata."""
        store, collection = self._store()

        await store.add_documents(
            [
                {
                    "id": "doc1",
                    "content": "Test content",
                    "metadata": {
                        "title": "Test Doc",
                        "chunk_index": 0,
                        "enterprise_id": _ENTERPRISE,
                    },
                }
            ]
        )

        collection.add.assert_called_once_with(
            ids=["doc1"],
            documents=["Test content"],
            metadatas=[
                {"title": "Test Doc", "chunk_index": 0, "enterprise_id": _ENTERPRISE}
            ],
        )

    # "No metadata" has four spellings — absent key, empty dict, explicit
    # None, and (invalidly) a non-dict. The first review of fm#1035 covered
    # only the first two; the guard screened `set(md or {})` while the write
    # re-derived `doc.get("metadata", {})`, so a present-but-null key passed
    # the guard and then raised AttributeError INSIDE call_external — the
    # breaker-poisoning path the guard exists to close. All four shapes are
    # pinned here so no two expressions can disagree about them again. They
    # write to a CASE collection: a KB chunk with no metadata names no tenant
    # and is refused since #1168 (``TestKbWriteRequiresEnterpriseStamp``).

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "doc",
        [
            {"id": "doc1", "content": "c"},  # metadata key absent
            {"id": "doc1", "content": "c", "metadata": {}},  # empty dict
            {"id": "doc1", "content": "c", "metadata": None},  # present-but-null
        ],
        ids=["absent", "empty", "null"],
    )
    async def test_no_metadata_in_any_spelling_writes_an_empty_dict(self, doc):
        store, collection = self._store()

        await store.add_documents([doc], collection_name=_CASE_COLLECTION)

        collection.add.assert_called_once_with(
            ids=["doc1"], documents=["c"], metadatas=[{}]
        )

    @pytest.mark.asyncio
    async def test_null_metadata_is_one_clean_call_with_nothing_charged(self):
        """The blocker's breaker leg: `metadata: None` must not touch the breaker.

        Before the fix this shape passed the guard (`set(None or {})` is
        empty) and blew up on `.items()` inside the wrapper — retried with
        backoff, each attempt recorded as a service failure on the breaker
        shared with the KB read path. With one normalization it is an
        ordinary empty-metadata write: exactly one attempt, success recorded,
        zero failures.
        """
        store, collection = self._store()

        await store.add_documents(
            [{"id": "doc1", "content": "c", "metadata": None}],
            collection_name=_CASE_COLLECTION,
        )

        assert store.circuit_breaker.failure_count == 0
        assert store.circuit_breaker.state == "closed"
        assert store.connection_metrics["failed_calls"] == 0
        assert store.connection_metrics["successful_calls"] == 1
        assert store.connection_metrics["total_calls"] == 1  # no retry
        collection.add.assert_called_once()  # one attempt, not three

    @pytest.mark.parametrize("missing", ["id", "content"])
    @pytest.mark.asyncio
    async def test_a_document_missing_a_required_field_never_reaches_the_breaker(
        self, missing
    ):
        """`id`/`content` are read outside the wrapper for the metadata reason.

        A document missing either is a deterministic programming error, but
        both fields used to be read INSIDE `_add_wrapper` — so the KeyError
        was retried with backoff and each attempt charged to the breaker this
        store shares with the KB read path. Closing the metadata hole while
        leaving these two inside would have fixed one third of the class.
        """
        store, collection = self._store()
        doc = {"id": "doc1", "content": "c", "metadata": {"enterprise_id": _ENTERPRISE}}
        del doc[missing]

        with pytest.raises(ValueError, match="missing required field"):
            await store.add_documents([doc])

        assert store.circuit_breaker.failure_count == 0
        assert store.circuit_breaker.state == "closed"
        assert store.connection_metrics["total_calls"] == 0
        assert store.connection_metrics["failed_calls"] == 0
        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dict_metadata_is_refused_outside_the_machinery(self):
        """A list/string metadata is refused by the guard, never `.items()`ed."""
        store, collection = self._store()

        with pytest.raises(ValueError, match="must be a dict"):
            await store.add_documents(
                [{"id": "doc1", "content": "c", "metadata": ["not", "a", "dict"]}]
            )

        assert store.circuit_breaker.failure_count == 0
        assert store.circuit_breaker.state == "closed"
        assert store.connection_metrics["total_calls"] == 0
        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_a_key_the_schema_would_drop(self):
        """An undeclared metadata key fails the write instead of vanishing.

        Nothing may be written when a key is refused — a partial row is the
        silent-drop failure with extra steps. One bad document in the batch
        refuses the whole batch.
        """
        store, collection = self._store()

        documents = [
            {
                "id": "doc1",
                "content": "ok",
                "metadata": {"title": "Fine", "enterprise_id": _ENTERPRISE},
            },
            {
                "id": "doc2",
                "content": "bad",
                "metadata": {
                    "title": "Doc",
                    "enterprise_id": _ENTERPRISE,
                    "not_a_schema_field": "value",
                },
            },
        ]

        with pytest.raises(ValueError, match="not_a_schema_field"):
            await store.add_documents(documents)

        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_refusal_never_enters_the_external_call_machinery(self):
        """The refusal is raised before ``call_external``, deliberately.

        A malformed metadata dict is a programming error, not a ChromaDB
        failure. Raised inside the wrapper it would consume the retry budget
        on a deterministic failure and count towards the circuit breaker —
        five of them would open it and start failing the *healthy* KB reads
        and writes sharing this store. Pinned by asserting the external-call
        machinery is never entered at all.
        """
        store, _collection = self._store()

        with patch.object(
            KnowledgeVectorStore, "call_external", new=AsyncMock()
        ) as mock_call:
            with pytest.raises(ValueError, match="not_a_schema_field"):
                await store.add_documents(
                    [
                        {
                            "id": "doc1",
                            "content": "c",
                            "metadata": {"not_a_schema_field": "v"},
                        }
                    ]
                )

        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_refusal_leaves_the_breaker_and_call_metrics_untouched(self):
        """The breaker-not-poisoned leg, on the REAL call path (nothing patched).

        The patched test above proves ``call_external`` is not entered; this
        one proves the observable consequences on the shared breaker: no
        failure recorded, breaker still closed, no call attempt (so no retry)
        ever counted. A guard that raised inside the wrapper would pass a
        naive "it raised" test while failing every assertion here.
        """
        store, collection = self._store()
        assert store.circuit_breaker is not None  # the property under test

        with pytest.raises(ValueError, match="not_a_schema_field"):
            await store.add_documents(
                [
                    {
                        "id": "doc1",
                        "content": "c",
                        "metadata": {"not_a_schema_field": "v"},
                    }
                ]
            )

        assert store.circuit_breaker.failure_count == 0
        assert store.circuit_breaker.state == "closed"
        assert store.connection_metrics["total_calls"] == 0
        assert store.connection_metrics["failed_calls"] == 0
        collection.add.assert_not_called()  # zero attempts — no retry occurred


class TestKbWriteRequiresEnterpriseStamp:
    """#1168: every chunk written to the KB collection names its owning tenant.

    ``add_documents`` is the one store every KB write goes through, so the
    refusal lives here and a future writer cannot skip the stamp. Refused
    OUTSIDE ``call_external``, for the ``reject_undeclared_keys`` reason: the
    failure is a deterministic programming error, and raised inside the wrapper
    it would burn the retry budget and charge the breaker the KB read path
    shares. Case-evidence collections are scoped by case and are unaffected.
    """

    def _store(self):
        store = KnowledgeVectorStore(client=MagicMock())
        collection = MagicMock()
        store._get_or_create_collection = MagicMock(return_value=collection)
        return store, collection

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "doc",
        [
            {"id": "doc1", "content": "c"},
            {"id": "doc1", "content": "c", "metadata": None},
            {"id": "doc1", "content": "c", "metadata": {}},
            {"id": "doc1", "content": "c", "metadata": {"scope": "personal"}},
            {"id": "doc1", "content": "c", "metadata": {"enterprise_id": None}},
            {"id": "doc1", "content": "c", "metadata": {"enterprise_id": ""}},
            {"id": "doc1", "content": "c", "metadata": {"enterprise_id": "  "}},
        ],
        ids=["absent", "null", "empty", "unstamped", "stamp-null", "blank", "space"],
    )
    async def test_an_unstamped_kb_write_is_refused_before_any_retry(self, doc):
        store, collection = self._store()

        with patch.object(
            KnowledgeVectorStore, "call_external", new=AsyncMock()
        ) as mock_call:
            with pytest.raises(ValueError, match="carries no enterprise_id"):
                await store.add_documents([doc])

        mock_call.assert_not_called()
        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_refusal_leaves_the_breaker_and_call_metrics_untouched(self):
        """The same refusal on the REAL call path (nothing patched)."""
        store, collection = self._store()

        with pytest.raises(ValueError, match="carries no enterprise_id"):
            await store.add_documents(
                [{"id": "doc1", "content": "c", "metadata": {"scope": "global"}}]
            )

        assert store.circuit_breaker.failure_count == 0
        assert store.circuit_breaker.state == "closed"
        assert store.connection_metrics["total_calls"] == 0
        assert store.connection_metrics["failed_calls"] == 0
        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_unstamped_chunk_refuses_the_whole_batch(self):
        """A partial write is the silent gap with extra steps: the stamped
        chunks would land and the unstamped one would be missing from nothing
        anyone checks."""
        store, collection = self._store()

        with pytest.raises(ValueError, match="doc2"):
            await store.add_documents(
                [
                    {
                        "id": "doc1",
                        "content": "ok",
                        "metadata": {"enterprise_id": _ENTERPRISE},
                    },
                    {"id": "doc2", "content": "bad", "metadata": {"scope": "global"}},
                ]
            )

        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stamped_kb_write_stores_the_stamp(self):
        """Positive control: the guard does not fail closed on a stamped chunk."""
        store, collection = self._store()

        await store.add_documents(
            [
                {
                    "id": "doc1",
                    "content": "c",
                    "metadata": {"scope": "global", "enterprise_id": _ENTERPRISE},
                }
            ]
        )

        collection.add.assert_called_once_with(
            ids=["doc1"],
            documents=["c"],
            metadatas=[{"scope": "global", "enterprise_id": _ENTERPRISE}],
        )

    @pytest.mark.asyncio
    async def test_a_case_collection_write_needs_no_stamp(self):
        """Case evidence is scoped by the case it belongs to, not by a KB
        tenant stamp — the refusal is for the KB collection only."""
        store, collection = self._store()

        await store.add_documents(
            [{"id": "ev1", "content": "log line", "metadata": {"title": "t"}}],
            collection_name=_CASE_COLLECTION,
        )

        collection.add.assert_called_once_with(
            ids=["ev1"], documents=["log line"], metadatas=[{"title": "t"}]
        )


class TestTheFilterPresenceKeySet:
    """#1168 removed ``organization_id`` from ``SCOPE_FILTER_KEYS``.

    No chunk ever carried it and ``VectorMetadata`` no longer declares it, so a
    clause keyed on it alone named a "scope" that could only ever match
    nothing. Dropping it tightens the presence check: such a clause is now
    refused as unscoped, like any other clause naming no scope key.
    """

    def test_organization_id_is_not_a_scope_key(self):
        assert "organization_id" not in SCOPE_FILTER_KEYS

    @pytest.mark.parametrize(
        "where",
        [
            {"organization_id": "org-1"},
            {"$or": [{"organization_id": "org-1"}]},
            {"$and": [{"organization_id": "org-1"}, {"domain": "database"}]},
        ],
        ids=["bare", "in-or", "in-and"],
    )
    def test_a_filter_keyed_only_on_organization_id_is_refused(self, where):
        store = KnowledgeVectorStore(client=MagicMock())

        with pytest.raises(ValueError, match="require scope filter"):
            store._require_kb_filter_present(KB_COLLECTION, where)

    @pytest.mark.asyncio
    async def test_a_search_keyed_only_on_organization_id_never_runs(self):
        """Through the read path that runs the check: refused before the query
        is embedded or ChromaDB is touched."""
        client = MagicMock()
        store = KnowledgeVectorStore(client=client)

        with patch.object(
            KnowledgeVectorStore, "_embed_query_or_raise", new=AsyncMock()
        ) as embed:
            with pytest.raises(ValueError, match="require scope filter"):
                await store.search(
                    KB_COLLECTION, "pool", k=3, where={"organization_id": "org-1"}
                )

        embed.assert_not_called()
        client.get_or_create_collection.assert_not_called()


class TestDeleteDocumentsByParentId:
    """The KB-side delete half of the document lifecycle (was missing entirely —
    its absence let the row-side prune leave vectors orphaned)."""

    @pytest.mark.asyncio
    async def test_deletes_only_the_named_parent(self):
        coll = _FakeCollection(
            [
                ("kb_a_chunk_0", "kb_a"),
                ("kb_a_chunk_1", "kb_a"),
                ("kb_b_chunk_0", "kb_b"),
            ]
        )
        store = KnowledgeVectorStore(_FakeClient(coll))
        deleted = await store.delete_documents_by_parent_id("kb_a")
        assert deleted == 2
        # kb_b's chunk survives; kb_a's are gone.
        assert coll._ids == {"kb_b_chunk_0": "kb_b"}

    @pytest.mark.asyncio
    async def test_absent_parent_is_zero(self):
        coll = _FakeCollection([("kb_b_chunk_0", "kb_b")])
        store = KnowledgeVectorStore(_FakeClient(coll))
        assert await store.delete_documents_by_parent_id("kb_missing") == 0


class TestDeleteDocumentsByParents:
    """Batch delete — one round-trip for many orphaned parents (reconcile path)."""

    @pytest.mark.asyncio
    async def test_deletes_all_named_parents_in_one_call(self):
        coll = _FakeCollection(
            [
                ("kb_a_chunk_0", "kb_a"),
                ("kb_b_chunk_0", "kb_b"),
                ("kb_b_chunk_1", "kb_b"),
                ("kb_keep_chunk_0", "kb_keep"),
            ]
        )
        store = KnowledgeVectorStore(_FakeClient(coll))
        deleted = await store.delete_documents_by_parents(["kb_a", "kb_b"])
        assert deleted == 3
        assert coll._ids == {"kb_keep_chunk_0": "kb_keep"}

    @pytest.mark.asyncio
    async def test_empty_batch_is_zero_and_no_io(self):
        coll = _FakeCollection([("kb_a_chunk_0", "kb_a")])
        store = KnowledgeVectorStore(_FakeClient(coll))
        assert await store.delete_documents_by_parents([]) == 0
        assert coll._ids == {"kb_a_chunk_0": "kb_a"}  # untouched


class TestListParentDocumentIds:
    @pytest.mark.asyncio
    async def test_returns_distinct_parents(self):
        coll = _FakeCollection(
            [
                ("kb_a_chunk_0", "kb_a"),
                ("kb_a_chunk_1", "kb_a"),
                ("kb_b_chunk_0", "kb_b"),
            ]
        )
        store = KnowledgeVectorStore(_FakeClient(coll))
        assert await store.list_parent_document_ids() == {"kb_a", "kb_b"}

    @pytest.mark.asyncio
    async def test_derives_parent_from_id_without_metadata(self):
        # Enumeration is id-based ({item_id}_chunk_N), so a chunk whose metadata
        # lacks parent_document_id (parent passed as None here) is still found —
        # matching the retrieval path's fallback and keeping reconcile complete.
        coll = _FakeCollection([("kb_x_chunk_0", None), ("kb_x_chunk_1", None)])
        store = KnowledgeVectorStore(_FakeClient(coll))
        assert await store.list_parent_document_ids() == {"kb_x"}

    @pytest.mark.asyncio
    async def test_missing_collection_is_empty_set(self):
        from chromadb.errors import NotFoundError

        class _NoCollectionClient:
            def get_collection(self, name):
                raise NotFoundError("collection not found")

        store = KnowledgeVectorStore(_NoCollectionClient())
        assert await store.list_parent_document_ids() == set()

    @pytest.mark.asyncio
    async def test_non_notfound_error_propagates(self):
        # A transient/transport error must NOT be flattened to "empty index"
        # (that would make reconcile warn every row as vector-less).
        class _BrokenClient:
            def get_collection(self, name):
                raise RuntimeError("chroma transport boom")

        store = KnowledgeVectorStore(_BrokenClient())
        with pytest.raises(RuntimeError):
            await store.list_parent_document_ids()
