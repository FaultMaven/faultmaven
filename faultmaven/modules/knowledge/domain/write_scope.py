"""The tier a KB write publishes to must be named, not inherited (#1166).

``global`` is the platform corpus: its rows are readable by **every** tenant.
Every write-side default used to be ``global``, so a publish path that simply
*neglected* to set a scope published tenant-authored content platform-wide —
and an omission appears nowhere in a diff, which is the one failure shape review
is worst at catching. The read side already fails CLOSED on the same omission
(an unidentifiable principal collapses to ``{"scope": "global"}``, i.e. reads
LESS); this is what makes the write side do the same.

Two ChromaDB writers exist, not one:

* ``KnowledgeService._index_document_in_vector_store`` — the live path (uploads,
  conversions, the shipped-runbook bootstrap, re-index and repair).
* ``KnowledgeIngester._process_and_store`` — ``collection.add`` directly, no SQL
  row, no RLS write policy. Dead today, and pinned dead; it matters because
  #1166 is precisely about the shape the *next* publish path will have, and a
  dead writer someone revives is that scenario.

Both call :func:`require_write_scope`, so neither can grow its own idea of what
an absent tier means. Keep it that way: a third writer belongs here too.
"""

from typing import Any, Optional

from faultmaven.models.exceptions import KnowledgeBaseError


def require_write_scope(document_id: Any, scope: Optional[str]) -> str:
    """Return the validated knowledge tier, or refuse the write.

    Args:
        document_id: Named in the error only, so the operator can find the
            document. Read defensively — a caller that got the tier wrong may
            have got this wrong too.
        scope: The tier the caller is publishing to.

    Returns:
        The validated tier, one of ``global`` | ``team`` | ``personal``.
        Callers stamp from THIS value rather than re-reading the document, so
        a property-backed ``scope`` cannot return one thing to the check and
        another to the store.

    Raises:
        KnowledgeBaseError: ``KNOWLEDGE_SCOPE_REQUIRED`` when the tier is
            absent or empty — the omission this issue is about.
            ``KNOWLEDGE_SCOPE_INVALID`` when it is present but not a real tier.
            The second is not pedantry: the stamp is derived as
            ``"global" if scope == "global" else "personal"``, so ``"Global"``
            and ``" "`` would be silently demoted to a tier readable by nobody
            — content that vanishes from retrieval under a row that looks
            healthy to every consistency check there is. Fail-closed, but
            silent, and this refusal is what makes it loud.
    """
    # Lazy: the knowledge domain models pull in persistence, and the services
    # importing this module are on the far side of that cycle.
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        KnowledgeScope,
    )

    if not scope:
        raise KnowledgeBaseError(
            f"Refusing to index document {document_id!r} with no scope: the "
            "knowledge tier is an explicit decision, and the absent one used "
            "to resolve to 'global' — the platform corpus every tenant reads.",
            error_code="KNOWLEDGE_SCOPE_REQUIRED",
        )

    valid = {member.value for member in KnowledgeScope}
    if scope not in valid:
        raise KnowledgeBaseError(
            f"Refusing to index document {document_id!r}: scope {scope!r} is "
            f"not a knowledge tier (expected one of {sorted(valid)}). An "
            "unrecognised tier is not stored as given — it is stamped with the "
            "narrow floor, which no read filter matches, so the document would "
            "be silently unretrievable rather than wrongly shared.",
            error_code="KNOWLEDGE_SCOPE_INVALID",
        )

    return scope


def metadata_scope_floor(scope: str) -> str:
    """Collapse a validated tier to the floor stamped on each chunk.

    Chunk metadata carries only the immutable floor: ``global`` (platform) vs
    ``personal`` (owner-only). A ``team`` item is stamped ``personal`` — its
    team visibility lives in the share table and is resolved to an id allowlist
    at query time. Writing ``team`` here would orphan the chunk on unshare, and
    writing ``global`` would leak it to everyone (ADR-013 §D4 / ADR-011 D3).
    """
    return "global" if scope == "global" else "personal"
