"""Row-checked KB retrieval: the parent rule and the admission filter (#2397).

A KB chunk is served only when its parent document has a complete, published
``knowledge_items`` row at query time. The knowledge module owns that check
(``IKnowledgeService.admitted_document_ids``) and hands it to every KB reader
as an ``admit`` callback (:data:`faultmaven.models.interfaces.AdmitParents`).
The readers never read ``knowledge_items`` themselves: they name each hit's
parent by the one rule below, await ``admit`` once per query, and drop every
hit whose parent it does not return.
"""

from typing import Any, Dict, List, Mapping, Optional

from faultmaven.models.interfaces import AdmitParents

#: Chunk ids are minted as ``f"{parent_document_id}_chunk_{i}"``
#: (``KnowledgeService._index_document_in_vector_store``).
CHUNK_ID_SEPARATOR = "_chunk_"


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


async def admitted_hits(
    hits: List[Dict[str, Any]], admit: AdmitParents
) -> List[Dict[str, Any]]:
    """``hits`` less every hit whose parent ``admit`` does not return.

    One ``admit`` call for the distinct parents, none for an empty list. Order
    is kept. Whatever ``admit`` raises propagates: a check that could not run
    is a read that did not complete, never an empty one.
    """
    if not hits:
        return []
    parents = {chunk_parent_id(hit.get("id"), hit.get("metadata")) for hit in hits}
    parents.discard(None)
    admitted = await admit(sorted(parents)) if parents else set()
    return [
        hit
        for hit in hits
        if chunk_parent_id(hit.get("id"), hit.get("metadata")) in admitted
    ]
