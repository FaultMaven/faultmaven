"""#1272 — the KB relevance score, and the seeding gate that reads it.

Four independent defects, pinned separately so a regression in one is not
masked by another:

1. ``_rerank`` computed a blend, sorted on it and threw it away, so no consumer
   could see or act on the quantity that had ordered the list.
2. ``_compute_metadata_score`` clamped at zero, which erased the whole demotion
   half of the lifecycle signal — ``deprecated`` scored exactly as well as
   ``draft``.
3. Term overlap weighted every query term equally, so a word in 6% of the
   corpus counted as much as one in 0.8% of it.
4. The keyword arm passed the user's own capitalisation to a CASE-SENSITIVE
   substring filter, so the arm's yield depended on the reporter's shift key.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    IDENTIFIER_DF_RATIO,
    MAX_TERM_INDEX_CHUNKS,
    CorpusTermStats,
    KnowledgeVectorStore,
    _fold_plural,
    _tokenize,
)


def _hit(cid, score, content="", **meta):
    meta.setdefault("scope", "global")
    return {
        "id": cid,
        "content": content or f"content {cid}",
        "metadata": meta,
        "score": score,
    }


# A small corpus where ``pid`` is common and ``enospc`` is rare — the shape the
# shipped pack has (84/1297 vs 11/1297) reproduced at a size a test can read.
_CORPUS = [f"the service wrote its pid file number {i}" for i in range(200)] + [
    "writes fail with ENOSPC no space left on device",
    "check inode exhaustion, enospc on the data volume",
]


@pytest.fixture
def stats():
    return CorpusTermStats(documents=_CORPUS, signature=len(_CORPUS))


class TestCorpusTermStats:
    def test_document_frequency_and_idf_separate_common_from_rare(self, stats):
        assert stats.document_frequency("pid") == 200
        assert stats.document_frequency("enospc") == 2
        assert stats.document_frequency("qemu") == 0
        # Rarer term must weigh more. This is the whole mechanism of #1272.
        assert stats.idf("enospc") > stats.idf("pid")
        assert stats.idf("qemu") > stats.idf("enospc")

    def test_unseen_term_idf_is_finite(self, stats):
        assert stats.idf("qemu") == pytest.approx(
            __import__("math").log((stats.n_chunks + 1) / 1) + 1.0
        )

    def test_identifier_is_decided_by_rarity_not_spelling(self, stats):
        # `enospc` is lowercase and matches none of IDENTIFIER_PATTERNS, but it
        # is rare enough in this corpus to behave like an identifier.
        assert stats.is_identifier("enospc")
        assert stats.is_identifier("qemu")
        assert not stats.is_identifier("pid")

    def test_identifier_threshold_is_the_declared_ratio(self, stats):
        cutoff = IDENTIFIER_DF_RATIO * stats.n_chunks
        for term in ("pid", "enospc", "qemu", "service"):
            assert stats.is_identifier(term) == (
                stats.document_frequency(term) <= cutoff
            )


class TestTokenizer:
    def test_splits_markdown_and_code_punctuation(self):
        # The KB is markdown. Without these separators `df` inside "`df -h`"
        # tokenizes as "`df" and never matches the query term `df`.
        assert "df" in _tokenize("run `df -h` to check")
        assert "syslog" in _tokenize("tail /var/log/syslog")
        assert "heap" in _tokenize("flag=-Xmx|heap")


class TestTermOverlapIsIdfWeighted:
    def test_rare_term_outweighs_common_term(self, stats):
        query = ["pid", "enospc"]
        common_only = KnowledgeVectorStore._compute_term_overlap(
            query, "the service wrote its pid file", stats=stats
        )
        rare_only = KnowledgeVectorStore._compute_term_overlap(
            query, "writes fail with enospc", stats=stats
        )
        assert rare_only > common_only, (
            "the rare term must carry more of the query's mass than the common "
            "one — without IDF these are both exactly 0.5"
        )

    def test_without_stats_it_is_the_old_binary_fraction(self, stats):
        query = ["pid", "enospc"]
        assert KnowledgeVectorStore._compute_term_overlap(
            query, "the service wrote its pid file"
        ) == pytest.approx(0.5)
        assert KnowledgeVectorStore._compute_term_overlap(
            query, "writes fail with enospc"
        ) == pytest.approx(0.5)

    def test_full_coverage_is_one_and_no_coverage_is_zero(self, stats):
        assert KnowledgeVectorStore._compute_term_overlap(
            ["pid", "enospc"], "pid and enospc", stats=stats
        ) == pytest.approx(1.0)
        assert KnowledgeVectorStore._compute_term_overlap(
            ["pid", "enospc"], "nothing relevant here", stats=stats
        ) == pytest.approx(0.0)


class TestMetadataScoreDemotionSurvives:
    def test_lifecycle_states_are_strictly_ordered(self):
        score = KnowledgeVectorStore._compute_metadata_score
        verified = score({"status": "verified"}, {})
        in_review = score({"status": "in-review"}, {})
        unknown = score({}, {})
        draft = score({"status": "draft"}, {})
        stale = score({"status": "stale"}, {})
        deprecated = score({"status": "deprecated"}, {})
        assert verified > in_review > unknown > draft > stale > deprecated, (
            "a DEPRECATED runbook must not score the same as a draft one — "
            "the old max(0.0, ...) clamp collapsed the bottom three to 0.0"
        )

    def test_output_stays_in_unit_range(self):
        score = KnowledgeVectorStore._compute_metadata_score
        best = score(
            {"status": "verified", "domain": "compute", "service": "linux"},
            {"domain": "compute", "service": "linux"},
        )
        worst = score({"status": "deprecated"}, {})
        assert 0.0 <= worst < best <= 1.0

    def test_relative_gaps_between_components_are_preserved(self):
        score = KnowledgeVectorStore._compute_metadata_score
        # The documented components are +0.4 verified vs -0.1 draft: a gap of
        # 0.5 on a range of 1.3.
        gap = score({"status": "verified"}, {}) - score({"status": "draft"}, {})
        assert gap == pytest.approx(0.5 / 1.3)


class TestRerankWritesBackTheScoreItRankedBy:
    def test_rerank_score_is_present_and_is_the_sort_key(self):
        candidates = [
            _hit("a", 0.40, "nothing in common", status="draft"),
            _hit("b", 0.39, "disk full enospc write error", status="draft"),
        ]
        out = KnowledgeVectorStore._rerank(
            candidates=candidates,
            query_terms=["disk", "full", "enospc"],
            context_metadata={},
            query="disk full enospc",
        )
        assert all("rerank_score" in c for c in out), (
            "the blend must be written back — discarding it left every consumer "
            "reading `score` (raw cosine) while the list was ordered by something else"
        )
        assert [c["rerank_score"] for c in out] == sorted(
            (c["rerank_score"] for c in out), reverse=True
        )
        assert out[0]["id"] == "b"

    def test_raw_cosine_is_left_untouched(self):
        candidates = [_hit("a", 0.4321, "x", status="draft")]
        out = KnowledgeVectorStore._rerank(
            candidates=candidates, query_terms=["x"], context_metadata={}, query="x"
        )
        assert out[0]["score"] == pytest.approx(0.4321), (
            "`score` is the absolute, calibrated scale every admission floor "
            "is expressed in; the reranker must not overwrite it"
        )
        assert out[0]["rerank_score"] != out[0]["score"]

    def test_grounding_evidence_is_written_back(self, stats):
        candidates = [
            _hit("a", 0.5, "some prose", title="Linux Disk Full", service="linux")
        ]
        out = KnowledgeVectorStore._rerank(
            candidates=candidates,
            query_terms=["disk", "full"],
            context_metadata={},
            query="my linux disk is full",
            stats=stats,
        )
        assert out[0]["identity_terms_in_query"] == ["disk", "full", "linux"]
        assert out[0]["term_coverage"] == pytest.approx(0.0)

    def test_identity_terms_empty_when_query_does_not_name_the_document(self):
        candidates = [
            _hit(
                "a",
                0.5,
                "prose",
                title="Kubernetes Pod CrashLoopBackOff",
                service="kubernetes",
            )
        ]
        out = KnowledgeVectorStore._rerank(
            candidates=candidates,
            query_terms=["qemu", "pid"],
            context_metadata={},
            query="Failed to start QEMU binary cannot create PID file",
        )
        assert out[0]["identity_terms_in_query"] == [], (
            "#1272's failure exactly: the runbook answers the query plausibly "
            "and the query is not about it"
        )


class TestIdentityMatchingIsTokenLevel:
    """A title word must be a WORD of the query, not a substring of one.

    The substring form grounded runbooks on queries that were not about them —
    measured against the shipped pack, "servicenow tickets are not syncing"
    grounded 6 runbooks through `service` and `sync`, and "podman containers
    exit immediately" grounded 4 Kubernetes runbooks through `pod`. Grounding is
    the check standing between retrieval and asserting a candidate root cause,
    so a false positive here is the failure the gate exists to prevent.
    """

    K8S_POD = {"title": "Kubernetes Pod CrashLoopBackOff", "service": "kubernetes"}
    K8S_SVC = {"title": "Kubernetes Service Unreachable", "service": "kubernetes"}
    ECS = {"title": "AWS ECS service unable to place tasks", "service": "aws-ecs"}
    LDF = {"title": "Linux Disk Full", "service": "linux"}
    PG = {"title": "PostgreSQL Connection Pool Exhaustion", "service": "postgresql"}

    def _named(self, meta, query):
        return KnowledgeVectorStore._identity_terms_in_query(meta, query.lower())

    def test_a_longer_word_does_not_name_a_shorter_title_word(self):
        assert self._named(self.K8S_POD, "podman containers exit immediately") == []
        assert self._named(self.K8S_SVC, "servicenow tickets are not syncing") == []
        assert self._named(self.ECS, "users cannot login to the portal") == []

    def test_a_plural_still_names_its_singular(self):
        """The one thing the substring form was really buying."""
        assert "connection" in self._named(
            self.PG, "Postgres is refusing new connections"
        )
        assert "pod" in self._named(self.K8S_POD, "our pods keep restarting")

    def test_a_genuine_mention_still_names(self):
        assert self._named(self.LDF, "linux disk full on the host") == [
            "disk",
            "full",
            "linux",
        ]

    def test_the_issue_query_does_not_name_a_kubernetes_runbook(self):
        for query in (
            "qemu cannot write its PID file",
            "Failed to start QEMU binary cannot create PID file",
        ):
            assert self._named(self.K8S_POD, query) == []
            assert self._named(self.K8S_SVC, query) == []

    def test_plural_fold_does_not_merge_unrelated_words(self):
        assert _fold_plural("podman") == "podman"
        assert _fold_plural("servicenow") == "servicenow"
        assert _fold_plural("portal") == "portal"
        assert _fold_plural("pods") == "pod"
        assert _fold_plural("connections") == "connection"
        assert _fold_plural("policies") == "policy"


class TestIdentifierDetection:
    def test_uses_corpus_rarity_when_statistics_exist(self, stats):
        assert KnowledgeVectorStore._query_has_identifier(
            "writes fail with enospc", ["writes", "fail", "enospc"], stats
        ), "a lowercase technical noun no shape pattern matches"
        assert not KnowledgeVectorStore._query_has_identifier(
            "the service wrote its pid file", ["service", "wrote", "pid", "file"], stats
        )

    def test_falls_back_to_shape_patterns_without_statistics(self):
        assert KnowledgeVectorStore._query_has_identifier(
            "pods in CrashLoopBackOff", ["pods", "crashloopbackoff"], None
        )
        assert not KnowledgeVectorStore._query_has_identifier(
            "writes fail with enospc", ["writes", "fail", "enospc"], None
        ), "the shape patterns cannot see this one — which is why IDF replaced them"


class TestKeywordArm:
    def test_seen_terms_rank_before_unseen_ones_rarest_first(self, stats):
        out = KnowledgeVectorStore._extract_search_keywords(
            "qemu wrote its pid to enospc", stats=stats
        )
        assert out.index("enospc") < out.index(
            "pid"
        ), "the rarest SEEN term must get the first of only three probes"
        assert out.index("pid") < out.index("qemu"), (
            "a term the index has never seen is speculative — it goes after "
            "every term known to match something"
        )

    def test_unseen_terms_are_never_dropped(self, stats):
        """The index sees only the global tier, so a personal runbook's own
        identifiers have df 0 there and dropping them would make them
        permanently unprobeable — the flywheel's core case."""
        out = KnowledgeVectorStore._extract_search_keywords(
            "acme-billing-svc is throwing errors", stats=stats
        )
        assert "acme-billing-svc" in out

    def test_without_statistics_the_shape_ordering_is_unchanged(self):
        out = KnowledgeVectorStore._extract_search_keywords(
            "CrashLoopBackOff error in payment-gateway-svc pod"
        )
        assert out[0] in ("CrashLoopBackOff", "payment-gateway-svc")

    def test_where_document_clause_is_case_insensitive(self):
        clause = KnowledgeVectorStore._where_document_for_keyword("enospc")
        variants = {c["$contains"] for c in clause["$or"]}
        assert {"enospc", "ENOSPC", "Enospc"} <= variants, (
            "ChromaDB's $contains is case-sensitive: measured on the shipped "
            "pack, 'ENOSPC' matches 11 chunks and 'enospc' matches 0"
        )

    def test_single_variant_keyword_uses_the_plain_form(self):
        assert KnowledgeVectorStore._where_document_for_keyword("503") == {
            "$contains": "503"
        }

    @pytest.mark.asyncio
    async def test_single_keyword_search_sends_the_case_insensitive_clause(self):
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.query.return_value = {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        store._get_or_create_collection = MagicMock(return_value=collection)

        async def _run(**kw):
            return await kw["call_func"]()

        store.call_external = AsyncMock(side_effect=_run)
        await store._single_keyword_search(
            collection_name="faultmaven_kb",
            query_embedding=[0.1],
            keyword="enospc",
            k=3,
            where={"scope": "global"},
        )
        sent = collection.query.call_args.kwargs["where_document"]
        assert "$or" in sent and any(c["$contains"] == "ENOSPC" for c in sent["$or"])


class TestTermIndexLifecycle:
    def test_cache_is_reused_while_the_collection_is_unchanged(self):
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.return_value = 2
        collection.get.return_value = {
            "ids": ["a", "b"],
            "documents": ["alpha", "beta"],
        }
        store._get_or_create_collection = MagicMock(return_value=collection)
        first = store._corpus_term_stats("faultmaven_kb")
        second = store._corpus_term_stats("faultmaven_kb")
        assert first is second
        assert collection.get.call_count == 1

    def test_cache_is_dropped_when_the_collection_changes_size(self):
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.side_effect = [2, 3]
        collection.get.side_effect = [
            {"ids": ["a", "b"], "documents": ["alpha", "beta"]},
            {"ids": ["a", "b", "c"], "documents": ["alpha", "beta", "gamma"]},
        ]
        store._get_or_create_collection = MagicMock(return_value=collection)
        first = store._corpus_term_stats("faultmaven_kb")
        second = store._corpus_term_stats("faultmaven_kb")
        assert first is not second
        assert second.n_chunks == 3

    def test_a_same_size_replace_still_invalidates(self):
        """An edited runbook re-ingests at the same chunk count."""
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.return_value = 2
        collection.get.side_effect = [
            {"ids": ["a", "b"], "documents": ["alpha", "beta"]},
            {"ids": ["a", "b"], "documents": ["alpha", "rewritten"]},
        ]
        store._get_or_create_collection = MagicMock(return_value=collection)
        store._corpus_term_stats("faultmaven_kb")
        store._invalidate_term_stats("faultmaven_kb")
        assert (
            store._corpus_term_stats("faultmaven_kb").document_frequency("rewritten")
            == 1
        )

    def test_oversized_collection_is_skipped_rather_than_scanned(self):
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.return_value = MAX_TERM_INDEX_CHUNKS + 1
        store._get_or_create_collection = MagicMock(return_value=collection)
        assert store._corpus_term_stats("faultmaven_kb") is None
        collection.get.assert_not_called()

    def test_an_empty_global_tier_yields_no_statistics_not_degenerate_ones(self):
        """n_chunks == 0 makes every idf 1.0 and every term an identifier.

        That is silent corruption of all three consumers, not degradation, and
        it is reachable: under TENANT_PROVIDER=multi the web-startup KB
        bootstrap is skipped, so the global tier is empty until the seeding job
        runs.
        """
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.return_value = 42  # other tiers exist
        collection.get.return_value = {"ids": [], "documents": []}
        store._get_or_create_collection = MagicMock(return_value=collection)
        assert store._corpus_term_stats("faultmaven_kb") is None

    def test_an_unusual_collection_never_yields_a_degenerate_index(self):
        """Whatever a collection returns, the result is a usable index or None.

        A store handed something it does not understand must degrade, never
        hand back an index that indexed nothing — and never raise, which would
        surface through the KB tool as "the knowledge base failed" on a search
        that was fine.
        """
        store = KnowledgeVectorStore(MagicMock())
        store._get_or_create_collection = MagicMock(return_value=MagicMock())
        stats = store._corpus_term_stats("faultmaven_kb")
        assert stats is None or stats.n_chunks > 0

    def test_a_failed_build_degrades_rather_than_raises(self):
        store = KnowledgeVectorStore(MagicMock())
        collection = MagicMock()
        collection.count.return_value = 2
        collection.get.side_effect = RuntimeError("chroma is unhappy")
        store._get_or_create_collection = MagicMock(return_value=collection)
        assert store._corpus_term_stats("faultmaven_kb") is None


class TestMinScoreIsAppliedAtAdmission:
    @pytest.mark.asyncio
    async def test_sub_floor_candidates_are_dropped_before_the_top_k_cut(self):
        """A sub-floor candidate the RERANKER ranks second must not take a slot.

        Chosen so the blend alone cannot exclude it: `drop` carries every query
        term and so wins the term-overlap signal outright, which lifts it above
        the on-topic `keep2` despite a cosine of 0.10. Only an admission-time
        floor removes it — a caller filtering the returned k afterwards is left
        with one result where it asked for two.
        """
        store = KnowledgeVectorStore(MagicMock())
        store._embed_query_or_raise = AsyncMock(return_value=[0.1])
        store._corpus_term_stats = MagicMock(return_value=None)
        store.search = AsyncMock(
            return_value=[
                _hit("keep1", 0.90, "an unrelated sentence"),
                _hit("drop", 0.10, "disk full disk full disk full"),
                _hit("keep2", 0.52, "another unrelated sentence"),
            ]
        )
        store._keyword_constrained_search = AsyncMock(return_value=[])

        unfiltered = await store.hybrid_search(
            collection_name="faultmaven_kb",
            query="disk full",
            k=2,
            where={"scope": "global"},
        )
        assert [c["id"] for c in unfiltered] == ["keep1", "drop"], (
            "control: without a floor the reranker really does rank the "
            "sub-floor candidate second — so the assertion below is about the "
            "floor and not about the blend"
        )

        out = await store.hybrid_search(
            collection_name="faultmaven_kb",
            query="disk full",
            k=2,
            where={"scope": "global"},
            min_score=0.5,
        )
        assert [c["id"] for c in out] == ["keep1", "keep2"]

    @pytest.mark.asyncio
    async def test_absent_min_score_changes_nothing(self):
        store = KnowledgeVectorStore(MagicMock())
        store._embed_query_or_raise = AsyncMock(return_value=[0.1])
        store._corpus_term_stats = MagicMock(return_value=None)
        store.search = AsyncMock(return_value=[_hit("a", 0.1, "disk")])
        store._keyword_constrained_search = AsyncMock(return_value=[])
        out = await store.hybrid_search(
            collection_name="faultmaven_kb",
            query="disk",
            k=5,
            where={"scope": "global"},
        )
        assert [c["id"] for c in out] == ["a"]
