"""The two knowledge search surfaces do what they are published as doing (#1288).

Both were documented as more than they were: ``GET /documents/{id}/snippet`` as
"semantic … based on vector similarity" while doing Python word overlap, and
``POST /documents/search`` as "keyword-based text matching across document
titles **and content**" while scoring titles only.

These tests pin BEHAVIOUR, not wording. A docstring-vs-prose guard would be
satisfied by any rewording and would prove nothing about what the endpoints
compute, so each claim here is pinned by an input pair that a lexical
implementation and a semantic one rank DIFFERENTLY:

* ``TestSnippetRanksLexically`` feeds a document holding a window packed with
  the query's WORDS but unrelated in meaning, and a window that paraphrases the
  query with no shared content word. Word overlap picks the first; an embedding
  picks the second. Swapping the implementation to a real vector search would
  fail this test, which is the point — the published description would then be
  wrong in the other direction.
* ``TestFulltextSearchMatchesContent`` uses a corpus of three documents whose
  match is in the title only, the body only, and neither. Title-only scoring
  cannot separate the body match from the non-match, so pinning that separation
  is exactly the claim the endpoint failed to keep.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    KnowledgeScope,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
    KnowledgeItemRepository,
)

# ---------------------------------------------------------------------------
# A. GET /documents/{id}/snippet — lexical, and provably not semantic
# ---------------------------------------------------------------------------

SNIPPET_QUERY = "disk is full"

# With max_lines=3 the windows are lines 1-3, 4-6 and 7-9.
LEXICAL_DECOY_LINES = (1, 3)
SEMANTIC_ANSWER_LINES = (4, 6)

DISCRIMINATING_DOC = "\n".join(
    [
        # Lines 1-3 — every content word of the query, no related meaning.
        "The full moon is bright tonight.",
        "A disk jockey is not a storage device.",
        "This paragraph is full of the query words and means nothing.",
        # Lines 4-6 — the actual answer, sharing no content word with the query.
        "ENOSPC: no space left on device.",
        "The volume has reached capacity and writes are being rejected.",
        "Free up storage or extend the filesystem to recover.",
        # Lines 7-9 — filler.
        "Unrelated: the cat sat on the mat.",
        "Nothing here at all.",
        "Filler line.",
    ]
)


# A document whose best-matching window does NOT start on a window boundary, so
# the two pre-#1288 algorithms disagreed about it. Window scoring returns lines
# 4-6; the line-substring scan centres on the FIRST verbatim hit (line 4) and
# returns 3-5. The query must actually occur, or both branches fall through to
# the same no-match path and a configuration-dependence test is vacuous.
CONFIG_DIVERGENCE_QUERY = "connection timeout"

CONFIG_DIVERGENCE_DOC = "\n".join(
    [
        "Intro paragraph, nothing to see.",  # 1
        "Some preamble text.",  # 2
        "More preamble.",  # 3
        "Diagnosing connection timeout errors in the pooler.",  # 4
        "Raise the connection timeout and retry.",  # 5
        "Check the upstream health first.",  # 6
        "Appendix A.",  # 7
        "Appendix B.",  # 8
        "Appendix C.",  # 9
    ]
)


def _snippet_service(vector_store, content=DISCRIMINATING_DOC):
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        db_session_factory=MagicMock(),
    )
    service._vector_store = vector_store

    async def _get_document(document_id):
        return {"document_id": document_id, "title": "T", "content": content}

    service.get_document = _get_document
    return service


@pytest.mark.asyncio
class TestSnippetRanksLexically:
    async def test_word_overlap_wins_over_meaning(self):
        """The published description says keyword matching. Prove it is that.

        A vector-similarity implementation would return the paraphrase at lines
        4-6. This asserts the word-packed decoy at lines 1-3, so the test fails
        if the endpoint ever silently becomes semantic — or ever silently stops
        being lexical while the docs still say so.
        """
        result = await _snippet_service(MagicMock()).get_relevant_snippet(
            "doc-1", SNIPPET_QUERY, max_lines=3
        )

        assert (result["line_start"], result["line_end"]) == LEXICAL_DECOY_LINES, (
            "the snippet ranked by meaning, not by word overlap — the endpoint "
            "is documented as keyword matching"
        )
        assert "disk jockey" in result["snippet"]
        assert "ENOSPC" not in result["snippet"]

    async def test_the_vector_store_is_never_consulted(self):
        """No embedding and no vector search happen on this path.

        Before #1288 this method was gated on ``if self._vector_store:`` and
        then did word overlap inside the gate, so the store was reached exactly
        once — for ``__bool__``. Asserting zero calls pins that the gate is
        gone, not merely that the ranking is still lexical.
        """
        store = MagicMock()
        service = _snippet_service(store)

        await service.get_relevant_snippet("doc-1", SNIPPET_QUERY, max_lines=3)

        assert store.mock_calls == [], (
            "the snippet path touched the vector store: "
            f"{store.mock_calls}. It is documented as not doing so."
        )

    async def test_the_answer_does_not_depend_on_vector_store_configuration(self):
        """Same document, same query, with and without a store bound.

        The old gate decided which of two lexical algorithms ran, so a
        deployment with ChromaDB configured got a different snippet from one
        without — for a query neither configuration embedded. Nothing about
        this endpoint's answer may depend on a component it does not call.

        Uses ``CONFIG_DIVERGENCE_DOC``, where the two algorithms provably
        disagreed (4-6 gated vs 3-5 ungated). A document the query does not
        occur in would send both branches down the same no-match path and make
        this assertion pass whatever the code does.
        """
        with_store = await _snippet_service(
            MagicMock(), CONFIG_DIVERGENCE_DOC
        ).get_relevant_snippet("d", CONFIG_DIVERGENCE_QUERY, max_lines=3)
        without_store = await _snippet_service(
            None, CONFIG_DIVERGENCE_DOC
        ).get_relevant_snippet("d", CONFIG_DIVERGENCE_QUERY, max_lines=3)

        # Guard the guard: a fallthrough on both sides would make the equality
        # below vacuous, so pin that the query really did match a window.
        assert with_store["relevance_score"] is not None
        assert (with_store["line_start"], with_store["line_end"]) == (4, 6)

        assert with_store == without_store, (
            "the snippet changed with vector-store configuration, though "
            "neither path consults it"
        )

    async def test_no_overlap_falls_back_to_a_window_not_an_error(self):
        """A query matching nothing still renders a hover card."""
        result = await _snippet_service(MagicMock()).get_relevant_snippet(
            "d", "zzzznonexistent", max_lines=3
        )

        assert result is not None
        assert result["snippet"]
        assert result["relevance_score"] is None


# ---------------------------------------------------------------------------
# B. POST /documents/search — matches title AND content
# ---------------------------------------------------------------------------

TITLE_MATCH_ID = "kb_aaaaaaaaaaaa"
BODY_MATCH_ID = "kb_bbbbbbbbbbbb"
NO_MATCH_ID = "kb_cccccccccccc"

SEARCH_TERM = "ENOSPC"

#: The enterprise the ``search_service`` fixture inserts; the corpus must use it
#: or every create fails the FK before a single search runs.
SEEDED_ENTERPRISE = "ent-1"


def _item(item_id, title, content, *, category=None, tags=None):
    """A published global runbook.

    Global on purpose: the platform tier carries NO organization_id (#770), and
    that is precisely what the deleted ``search_by_text`` could not see. It does
    carry an ``enterprise_id`` — isolation is not optional for any tier — and it
    must be the enterprise the fixture seeds, because the FK is enforced here.
    """
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id=SEEDED_ENTERPRISE,
        title=title,
        content=content,
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.GLOBAL,
        owner_id=None,
        is_published=True,
        category=category,
        tags=tags or [],
    )


CORPUS = [
    _item(
        TITLE_MATCH_ID,
        "ENOSPC triage",
        "# Steps\nUnrelated body text carrying no matching token.",
        category="storage",
    ),
    _item(
        BODY_MATCH_ID,
        "Storage volume runbook",
        "# Steps\nThe pod logs show ENOSPC once the volume fills.",
        category="storage",
        tags=["kubernetes"],
    ),
    _item(
        NO_MATCH_ID,
        "Kafka rebalance loop",
        "# Steps\nConsumer group thrashing, nothing relevant here.",
        category="streaming",
    ),
]


@pytest.fixture
async def search_service():
    """Real ``KnowledgeService`` search over FK-on SQLite.

    The relational read is real, so the RBAC-isolated row set this endpoint
    scores is the one production scores. Mirrors the fixture in
    ``test_documents_inventory.py``.
    """
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # replicates the #378 connect listener
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES ('org-1', 'ent-1', 'Org', 'org')"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    service = KnowledgeService.__new__(KnowledgeService)
    service._db_session_factory = factory
    service._vector_store = MagicMock()
    service._vector_store.delete_documents_by_parent_id = AsyncMock(return_value=0)
    service._tracer = MagicMock()
    service._share_repo = None

    async with factory() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        for item in CORPUS:
            await repo.create(item)

    yield service
    await engine.dispose()


# An AUTHENTICATED caller. It matters that ``user_id`` is set: body excerpts are
# returned only to a caller who could have read the document directly, and the
# first version of this fixture used ``user_id=None`` — an anonymous caller —
# while asserting it received content, which is the exposure #1310's review
# found.
USER = SimpleNamespace(user_id="user-1", organization_id="org-1")
ANONYMOUS = None


@pytest.mark.asyncio
class TestFulltextSearchMatchesContent:
    async def test_a_body_only_match_is_found_and_outranks_a_non_match(
        self, search_service
    ):
        """The claim under audit: titles AND content.

        Title-only scoring gave the body match 0.0 — the same score as the
        document matching nothing — so the two were indistinguishable and a real
        hit could be crowded out by ``limit`` in creation order. Separating them
        is the whole claim, so this asserts the ORDERING and the exclusion, not
        merely that something came back.
        """
        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER
        )

        scores = {r["document_id"]: r["similarity_score"] for r in result["results"]}

        assert BODY_MATCH_ID in scores, (
            "a document whose BODY contains the query was not returned — the "
            "endpoint is published as matching content"
        )
        assert NO_MATCH_ID not in scores, (
            "a document matching neither title nor body was returned; this is a "
            "search, not a ranked listing of the knowledge base"
        )
        assert scores[BODY_MATCH_ID] > 0.0
        assert (
            scores[TITLE_MATCH_ID] > scores[BODY_MATCH_ID]
        ), "a title hit must outrank a body hit"

    async def test_scores_share_the_scale_similarity_threshold_declares(
        self, search_service
    ):
        """The API declares ``similarity_threshold`` in 0.0-1.0, so scores must be.

        The old scorer was ``0.8 + 0.3 * matched_words``, unbounded above 1.0,
        which made the declared threshold domain meaningless.
        """
        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER
        )

        assert result["results"]
        for hit in result["results"]:
            assert 0.0 < hit["similarity_score"] <= 1.0, hit

    async def test_similarity_threshold_excludes_the_weaker_match(self, search_service):
        """A threshold between the two scores keeps the title hit and drops the body hit."""
        unfiltered = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER
        )
        scores = {
            r["document_id"]: r["similarity_score"] for r in unfiltered["results"]
        }
        cutoff = (scores[BODY_MATCH_ID] + scores[TITLE_MATCH_ID]) / 2

        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER, similarity_threshold=cutoff
        )

        returned = {r["document_id"] for r in result["results"]}
        assert returned == {TITLE_MATCH_ID}

    async def test_results_carry_a_content_excerpt(self, search_service):
        """The published response example shows ``content``; it was always "".""" ""
        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER
        )

        by_id = {r["document_id"]: r for r in result["results"]}
        assert (
            SEARCH_TERM in by_id[BODY_MATCH_ID]["content"]
        ), "the excerpt for a body match should show the match"
        assert by_id[TITLE_MATCH_ID][
            "content"
        ], "a title match should still carry a body excerpt, not an empty string"

    async def test_the_category_filter_is_applied(self, search_service):
        """ "Filtering by document_type, category, tags" — category was ignored."""
        matched = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER, category="storage"
        )
        excluded = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER, category="networking"
        )

        assert {r["document_id"] for r in matched["results"]} == {
            TITLE_MATCH_ID,
            BODY_MATCH_ID,
        }
        assert excluded["results"] == [], (
            "category was accepted and never read, so every value returned the "
            "same rows"
        )

    async def test_the_tags_filter_is_applied(self, search_service):
        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER, tags=["kubernetes"]
        )

        assert {r["document_id"] for r in result["results"]} == {BODY_MATCH_ID}

    async def test_global_runbooks_are_visible(self, search_service):
        """The corpus is entirely global (org-free platform tier, #770).

        Any implementation reaching for an ``organization_id ==`` predicate
        without a global arm returns nothing here — which is exactly why
        ``KnowledgeItemRepository.search_by_text`` was deleted rather than
        wired up as #1288 suggested.
        """
        result = await search_service.fulltext_search_documents(
            query=SEARCH_TERM, user=USER
        )

        assert result["total_results"] > 0


@pytest.mark.asyncio
class TestExcerptAnchorsOnTheMatch:
    """``content`` must show the match, not the head of the document.

    The excerpt first anchored only on the WHOLE query appearing verbatim, so a
    multi-word query — the endpoint's own documented example is
    "PostgreSQL connection timeout" — essentially never anchored and every
    result showed its opening lines instead.
    """

    async def test_a_multi_word_query_anchors_on_a_matching_word(self):
        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            _match_excerpt,
            _tokenize,
        )

        # Long enough that the head cannot reach the hit by accident — without
        # this the assertion passes on the unfixed code.
        body = "Preamble. " * 60 + "The ENOSPC alert fires here. " + "Tail. " * 60
        assert "ENOSPC" not in body[:240], "fixture too short to discriminate"

        excerpt = _match_excerpt(_tokenize("disk enospc"), body)

        assert "ENOSPC" in excerpt, (
            "a query whose words are present but not contiguous fell back to "
            f"the document head: {excerpt[:60]!r}"
        )

    async def test_no_matching_word_still_yields_the_head(self):
        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            _match_excerpt,
            _tokenize,
        )

        body = "Preamble. " * 60 + "The ENOSPC alert fires here. "
        excerpt = _match_excerpt(_tokenize("kafka rebalance"), body)

        assert excerpt.startswith("Preamble.")


@pytest.mark.asyncio
class TestSearchArmsDoNotDivergeOnCategory:
    """``POST /knowledge/search`` must answer the same with or without vectors.

    Its vector arm filters only on ``document_type``; the scope filter has no
    category term. Passing ``category`` down to the vectorless fallback — which
    CAN filter it — made the endpoint's answer depend on whether a store
    happened to be bound, which is the configuration-dependence #1288 removed
    from the snippet path.
    """

    async def test_the_vectorless_fallback_ignores_category_as_the_vector_arm_does(
        self, search_service
    ):
        search_service._vector_store = None

        with_category = await search_service.search_documents(
            query=SEARCH_TERM, user=USER, category="a-category-no-document-has"
        )
        without_category = await search_service.search_documents(
            query=SEARCH_TERM, user=USER
        )

        assert [r["document_id"] for r in with_category["results"]] == [
            r["document_id"] for r in without_category["results"]
        ], (
            "the vectorless arm filtered on category while the vector arm "
            "cannot, so this endpoint's answer depends on configuration"
        )
        assert without_category["results"], "guard the guard: the query matched nothing"


class TestSearchByTextIsGone:
    """The dead, schema-stale repository method stays deleted.

    It had no caller — the two that look like callers pass
    ``query_text``/``scope_filter``/``top_k``/``min_similarity`` to
    ``RunbookKnowledgeBase.search_by_text``, a signature it cannot accept — and
    its ``organization_id`` predicate carried no global arm, so adopting it
    would have hidden every platform-tier runbook.
    """

    def test_the_repository_contract_does_not_offer_it(self):
        assert not hasattr(KnowledgeItemRepository, "search_by_text")
        assert not hasattr(DatabaseKnowledgeItemRepository, "search_by_text")
