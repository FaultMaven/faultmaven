"""One answer, for every vector store, to "what does this ChromaDB distance mean".

No collection in this codebase declares ``hnsw:space``, so every one of them —
``faultmaven_kb`` and every per-case evidence collection — is created with
ChromaDB's **default space, ``l2``**, and ChromaDB's ``l2`` returns the
*squared* euclidean distance. Every vector written here is BGE-M3 output, which
sentence-transformers L2-normalizes, so for unit vectors::

    d = ||q - v||^2 = 2 - 2*cos     =>     cos = 1 - d/2

Four retrieval paths computed ``1.0 - d`` instead, which is not cosine but
``2*cos - 1``: the same ordering, on a scale shifted by -1 and stretched by 2.
Ordering being preserved is exactly why it survived — ranking looked right, and
only an *absolute* comparison could expose it. One did. ``UnifiedKBConfig``
refuses synthesis below a documented "cosine similarity" floor of 0.3, which on
the real scale is a cosine floor of **0.65** — inside the on-topic population,
not below it. The KB was measured refusing on-topic queries whose top hit was
the correct runbook, and telling the investigating model the KB did not cover
the topic (#1072).

``RunbookKnowledgeBase`` had this right (``1.0 - distance / 2.0``) while its
four siblings had it wrong, which is the same drift ``embedding_guard`` exists
to end: a conversion restated per store is a conversion that can disagree with
itself. It lives here now, once, and every store calls it.

**The unit-norm assumption is load-bearing.** ``cosine_from_chroma_distance``
is only cosine because the vectors are normalized; on unnormalized vectors it
is meaningless. That holds today at every write site because they all embed
through ``model_cache.aembed_*`` (BGE-M3 ships a ``Normalize`` module) or from
the pre-normalized KB pack.

**The ``l2`` assumption is load-bearing too, and cannot be fixed by declaring
cosine.** ChromaDB silently *ignores* a ``configuration`` space that disagrees
with an existing collection — asking for cosine on a live ``l2`` collection
returns an ``l2`` collection and no error, so "just declare cosine" is a
no-op wearing a fix's clothes, and would need a full reindex to mean anything.
Converting correctly needs no reindex at all. What guards the assumption is a
test asserting the collection-creation path still yields ``l2``, so a future
change to ChromaDB's default breaks a test rather than the knowledge base.
"""

from typing import Final

# ChromaDB's default vector space, which every collection here inherits by not
# declaring one. Named so the assumption is greppable from the conversion.
CHROMA_DEFAULT_SPACE: Final[str] = "l2"


def cosine_from_chroma_distance(distance: float) -> float:
    """Convert a ChromaDB ``l2`` distance to cosine similarity.

    Valid only for L2-normalized vectors, where ChromaDB's squared-euclidean
    distance is ``2 - 2*cos``. See the module docstring for why every store
    must route through here rather than open-coding the arithmetic.

    Args:
        distance: A distance from ``results["distances"]`` on a collection
            using the default ``l2`` space.

    Returns:
        Cosine similarity in ``[-1.0, 1.0]``. NOT clamped: a negative value
        means genuinely opposed vectors, and callers that want a floor should
        apply their own, visibly.
    """
    return 1.0 - (distance / 2.0)
