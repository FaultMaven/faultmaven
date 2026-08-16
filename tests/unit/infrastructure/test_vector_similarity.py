"""The distance -> cosine conversion, and the two assumptions holding it up.

#1072: every KB and evidence collection is created without declaring
``hnsw:space``, so all of them use ChromaDB's default ``l2``, whose distance is
*squared* euclidean. Four retrieval paths converted it with ``1 - distance``,
which is ``2*cos - 1`` — order-preserving, so ranking looked correct and only
an absolute threshold could expose it. One did, by refusing on-topic queries.

These tests pin the arithmetic AND the two facts it depends on, because the
arithmetic is silently wrong if either stops being true.
"""

import math

import numpy as np
import pytest

from faultmaven.infrastructure.vector_similarity import (
    CHROMA_DEFAULT_SPACE,
    cosine_from_chroma_distance,
)


@pytest.mark.unit
class TestCosineFromChromaDistance:
    def test_identical_vectors(self):
        assert cosine_from_chroma_distance(0.0) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        # Unit vectors at cos=0 are sqrt(2) apart; squared, that is 2.0.
        assert cosine_from_chroma_distance(2.0) == pytest.approx(0.0)

    def test_opposed_vectors(self):
        assert cosine_from_chroma_distance(4.0) == pytest.approx(-1.0)

    def test_is_not_the_old_expression(self):
        """The regression guard: `1 - d` and `1 - d/2` must not be confused.

        At the distance behind the #1072 refusal, the two differ by 0.36 —
        the whole width of the gap between the on- and off-topic populations.
        """
        distance = 0.818  # measured: cos 0.591, the weakest on-topic retrieval
        assert cosine_from_chroma_distance(distance) == pytest.approx(0.591, abs=1e-3)
        assert (1.0 - distance) == pytest.approx(0.182, abs=1e-3)

    def test_recovers_true_cosine_for_normalized_vectors(self):
        """Property check against numpy over random unit vectors."""
        rng = np.random.default_rng(1072)
        for _ in range(200):
            a = rng.normal(size=64)
            b = rng.normal(size=64)
            a /= np.linalg.norm(a)
            b /= np.linalg.norm(b)

            squared_l2 = float(np.sum((a - b) ** 2))
            true_cosine = float(np.dot(a, b))

            assert cosine_from_chroma_distance(squared_l2) == pytest.approx(
                true_cosine, abs=1e-9
            )

    def test_is_not_clamped(self):
        """Callers apply their own floor, visibly — see RunbookKnowledgeBase."""
        assert cosine_from_chroma_distance(3.0) < 0.0


@pytest.mark.unit
class TestLoadBearingAssumptions:
    """Both are true today. The conversion is wrong the moment either isn't."""

    def test_bge_m3_vectors_in_the_shipped_kb_pack_are_normalized(self):
        """Assumption 1: unit norm. Without it, `1 - d/2` is not cosine."""
        pack = (
            __import__("pathlib").Path(__file__).parents[3]
            / "resources/knowledge/pack/vectors.npz"
        )
        if not pack.exists():
            pytest.skip("KB pack not vendored in this checkout")

        vectors = np.load(pack)["vectors"].astype("float64")
        norms = np.linalg.norm(vectors, axis=1)

        assert np.allclose(norms, 1.0, atol=1e-5), (
            f"KB pack vectors are not unit-normalized "
            f"(norms {norms.min():.6f}..{norms.max():.6f})"
        )

    def test_kb_collections_are_created_in_the_default_l2_space(self):
        """Assumption 2: ``l2``.

        No collection declares ``hnsw:space``, so this pins ChromaDB's default
        rather than our own configuration. If a future ChromaDB changes that
        default to cosine, newly created collections silently return a
        different distance and every score here doubles — this test is what
        makes that a red build instead of a degraded knowledge base.

        Note the fix could NOT have been "declare cosine": ChromaDB silently
        ignores a configuration space that disagrees with an existing
        collection, so it is a no-op on any deployed KB without a full reindex.
        """
        chromadb = pytest.importorskip("chromadb")
        from chromadb.config import Settings as ChromaSettings

        # Settings are PINNED for the same reason ``create_persistent_client``
        # and ``tests/infrastructure/knowledge/test_runbook_kb_scope.py`` pin
        # them (#823): chromadb caches one System per identifier and refuses a
        # second client for it whose ``Settings`` differ in any field — and
        # ``Settings.environment`` defaults to the ambient ENVIRONMENT
        # variable, which other tests set and clear. A bare
        # ``EphemeralClient()`` here does not just make THIS test
        # order-dependent, it breaks every other ephemeral-client test that
        # runs after it in the same process.
        client = chromadb.EphemeralClient(
            settings=ChromaSettings(
                anonymized_telemetry=False,
                allow_reset=False,
                environment="",
                is_persistent=False,
            )
        )
        collection = client.get_or_create_collection(
            name="space-assumption-probe",
            metadata={"type": "knowledge_base"},
        )
        collection.add(ids=["a", "b"], embeddings=[[1.0, 0.0], [0.0, 1.0]])

        # Query with a unit vector identical to "a". Under l2 the distance to
        # "b" (orthogonal) is the SQUARED distance, 2.0; under cosine it is
        # 1 - cos = 1.0. The returned number identifies the space.
        result = collection.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=2,
            include=["distances"],
        )
        by_id = dict(zip(result["ids"][0], result["distances"][0], strict=True))

        assert math.isclose(by_id["b"], 2.0, abs_tol=1e-4), (
            f"ChromaDB's default space is no longer {CHROMA_DEFAULT_SPACE!r}: "
            f"orthogonal distance is {by_id['b']}, expected 2.0 (squared L2). "
            f"cosine_from_chroma_distance is now wrong for new collections."
        )


@pytest.mark.unit
class TestRerankerConsumesTrueCosine:
    """The reranker blends signal 1 with three genuine 0-1 signals.

    That only balances if signal 1 is one too. Under #1072 it carried
    ``2*cos - 1``, whose slope in the composite is double cosine's, so
    ``RERANK_WEIGHT_VECTOR = 0.40`` configured an effective ~0.80 and the four
    weights never described the blend they appeared to. The constant offset
    cancels across candidates; the slope does not.
    """

    QUERY_TERMS = ["alpha", "beta", "gamma", "delta"]

    def _rank_ids(self, vector_signal):
        """Rank two candidates through the REAL reranker.

        A has the better embedding match, B the better lexical match (1 of 4
        query terms vs 2 of 4). Metadata and freshness are identical, so they
        cancel and the outcome turns purely on the vector/term balance.
        """
        from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
            KnowledgeVectorStore,
        )

        candidates = [
            {
                "id": "A",
                "content": "alpha",
                "metadata": {},
                "score": vector_signal(0.70),
            },
            {
                "id": "B",
                "content": "alpha beta",
                "metadata": {},
                "score": vector_signal(0.60),
            },
        ]
        ranked = KnowledgeVectorStore._rerank(
            candidates=candidates,
            query_terms=self.QUERY_TERMS,
            context_metadata={},
            query="",
        )
        return [c["id"] for c in ranked]

    def test_corrected_scale_lets_the_configured_blend_decide(self):
        """On true cosine, the 40/25 vector/term blend picks the lexical match."""
        assert self._rank_ids(lambda cos: cos) == ["B", "A"]

    def test_old_scale_let_the_vector_signal_override_it(self):
        """Same inputs on the pre-#1072 scale invert the result.

        This is the regression this correction fixes, stated as behaviour: the
        doubled vector slope outvoted a term-overlap advantage that the
        configured weights say should win.
        """
        assert self._rank_ids(lambda cos: 2 * cos - 1) == ["A", "B"]

    def test_rerank_returns_candidates_carrying_cosine_not_its_composite(self):
        """The composite is used to sort and never written back.

        The relevance threshold reads this score and is calibrated in cosine,
        so if `_rerank` ever starts writing its composite here, that threshold
        silently moves into an uncalibrated four-signal space.
        """
        from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
            KnowledgeVectorStore,
        )

        (ranked,) = KnowledgeVectorStore._rerank(
            candidates=[
                {"id": "A", "content": "alpha", "metadata": {}, "score": 0.591}
            ],
            query_terms=self.QUERY_TERMS,
            context_metadata={},
            query="",
        )

        assert ranked["score"] == pytest.approx(0.591)
