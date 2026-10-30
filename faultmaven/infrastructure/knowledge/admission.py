"""Row-checked KB retrieval: the chunk-id rules and the admission filter (#2397).

A KB chunk is served only when its parent document has a complete, published
``knowledge_items`` row at query time and the chunk carries that row's
``vector_generation`` (#2388). The knowledge module owns that check
(``IKnowledgeService.admitted_generations``) and hands it to every KB reader
as an ``admit`` callback (:data:`faultmaven.models.interfaces.AdmitParents`).
The readers never read ``knowledge_items`` themselves: they name each hit's
parent by the one rule below, await ``admit`` once per page read, and drop every
hit that :func:`chunk_admitted` refuses.
"""

from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
)

from faultmaven.models.interfaces import AdmitParents

#: A reader deepens a thinned page by doubling its fetch, up to this many times
#: the first.
MAX_DEEPENING = 8

R = TypeVar("R")
K = TypeVar("K")

#: Chunk ids are minted as ``f"{parent_document_id}_chunk_{i}_{generation}"``
#: (:func:`chunk_id`); a chunk written before #2388 has no ``_{generation}``.
CHUNK_ID_SEPARATOR = "_chunk_"


def chunk_id(parent_document_id: str, index: int, generation: str) -> str:
    """The id of chunk ``index`` of ``parent_document_id`` under ``generation``."""
    return f"{parent_document_id}{CHUNK_ID_SEPARATOR}{index}_{generation}"


def chunk_generation_from_id(chunk_id_: Any) -> Optional[str]:
    """The generation a chunk id names: the part after its ``_chunk_{i}_``, or
    None for an id with none (written before #2388). What the collectors read,
    since their listings are id-only."""
    if not isinstance(chunk_id_, str) or CHUNK_ID_SEPARATOR not in chunk_id_:
        return None
    _index, _sep, generation = chunk_id_.rsplit(CHUNK_ID_SEPARATOR, 1)[1].partition("_")
    return generation or None


def chunk_parent_id(
    chunk_id: Any, metadata: Optional[Mapping[str, Any]]
) -> Optional[str]:
    """The parent document a KB chunk belongs to, or None.

    ``metadata["parent_document_id"]`` when the chunk carries it, which every
    chunk the live writer stamps does; otherwise the part of the chunk id
    before its last ``_chunk_``. None when neither names a parent: such a
    chunk is never admitted.
    """
    parent = (metadata or {}).get("parent_document_id")
    if isinstance(parent, str) and parent:
        return parent
    if isinstance(chunk_id, str) and chunk_id:
        return chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[0] or None
    return None


def chunk_admitted(
    chunk_id_: Any,
    metadata: Optional[Mapping[str, Any]],
    admitted: Mapping[str, str],
) -> bool:
    """Whether ``admitted`` (an ``admit`` answer) admits this chunk: its parent
    is a key, and its ``metadata["vector_generation"]`` is a non-empty string
    equal to that key's value (#2388). A chunk with no generation, or another
    lifetime's, is never admitted."""
    parent = chunk_parent_id(chunk_id_, metadata)
    generation = (metadata or {}).get("vector_generation")
    return (
        parent is not None
        and isinstance(generation, str)
        and bool(generation)
        and admitted.get(parent) == generation
    )


async def admitted_hits(
    hits: List[Dict[str, Any]], admit: AdmitParents
) -> List[Dict[str, Any]]:
    """``hits`` less every hit :func:`chunk_admitted` refuses.

    One ``admit`` call for the distinct parents, none for an empty list. Order
    is kept. Whatever ``admit`` raises propagates: a check that could not run
    is a read that did not complete, never an empty one.
    """
    if not hits:
        return []
    parents = {chunk_parent_id(hit.get("id"), hit.get("metadata")) for hit in hits}
    parents.discard(None)
    admitted = await admit(sorted(parents)) if parents else {}
    return [
        hit
        for hit in hits
        if chunk_admitted(hit.get("id"), hit.get("metadata"), admitted)
    ]


async def read_deepening(
    read: Callable[[int], Awaitable[R]],
    judge: Callable[[R, int], Awaitable[Tuple[K, bool]]],
    first: int,
) -> Tuple[K, bool]:
    """Read a page of ``first`` rows, and again with the fetch doubled while
    admission leaves it short, up to :data:`MAX_DEEPENING` times ``first``: an
    unpublished built-in keeps its vectors until the next pack-loading
    bootstrap, and they can fill a whole page.

    ``judge(page, size)`` answers what the reader keeps, and whether the page
    came back full and admission left fewer than the reader needs (still true
    only at the cap).
    """
    size = first
    while True:
        kept, short = await judge(await read(size), size)
        if not short or size >= first * MAX_DEEPENING:
            return kept, short
        size *= 2
