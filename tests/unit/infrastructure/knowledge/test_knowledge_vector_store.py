"""Tests for KnowledgeVectorStore — reranker, keyword extraction, chunking."""

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    RERANK_WEIGHT_FRESHNESS,
    RERANK_WEIGHT_METADATA,
    RERANK_WEIGHT_TERM_OVERLAP,
    RERANK_WEIGHT_VECTOR,
    SCOPE_PRIORITY,
    KnowledgeVectorStore,
)


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
        """No case context means no domain/service boost."""
        score = KnowledgeVectorStore._compute_metadata_score(
            {"domain": "networking"}, {}
        )
        assert score == 0.0

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
        assert score == 0.0


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

    def test_weights_sum_to_one(self):
        total = (
            RERANK_WEIGHT_VECTOR
            + RERANK_WEIGHT_TERM_OVERLAP
            + RERANK_WEIGHT_METADATA
            + RERANK_WEIGHT_FRESHNESS
        )
        assert abs(total - 1.0) < 0.001

    def test_vector_is_largest_weight(self):
        assert RERANK_WEIGHT_VECTOR > RERANK_WEIGHT_TERM_OVERLAP
        assert RERANK_WEIGHT_VECTOR > RERANK_WEIGHT_METADATA
        assert RERANK_WEIGHT_VECTOR > RERANK_WEIGHT_FRESHNESS


class TestScopePriority:
    """Verify scope priority constants."""

    def test_personal_highest_priority(self):
        assert SCOPE_PRIORITY["personal"] < SCOPE_PRIORITY["team"]
        assert SCOPE_PRIORITY["team"] < SCOPE_PRIORITY["global"]
