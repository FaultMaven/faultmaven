"""A knowledge item's title is scrubbed the same way on create as on update (#1981).

A title is shown to every reader of the tier the item lives in (team and global
included), in every list, and it is never reversed: nothing maps its
placeholders back. It is also free text someone typed (an upload's ``title``
form field, a draft's title at publish) or a model wrote (a draft built from a
case). So it passes through the deployment's sanitizer, the same one the body
passes through when a document is updated, before any row or vector carries it.

Until #1981 only ``update_document`` did. A runbook created with a title like
``Fix login (password=hunter2)`` stored and displayed it verbatim, while the
same string typed into an edit was redacted.

Three writers exist, and all three call :func:`sanitize_item_title`:

* ``KnowledgeService.ingest_runbook``: the one place a ``knowledge_items`` row
  is created. Uploads, published conversion drafts and the KB pack all reach it.
  The pack alone is exempt (``shipped=True``): its titles are vendored,
  reviewed source, not user or model text, and a redaction outage must not be
  able to stop the platform corpus from seeding.
* ``KnowledgeService.upload_document``: writes the draft row and the upload row
  from the title BEFORE ``ingest_runbook`` runs, so it scrubs first and every
  row it writes holds the same text.
* ``KnowledgeService.update_document``.

``KnowledgeIngester.ingest_document`` and ``ingest_document_object`` call it
too. Nothing routes there today (``write_scope`` pins it dead), but they are the
shape the next publish path will have, and a revived one must not be the one
that publishes a title unscrubbed. A fourth writer belongs here as well.
"""

from faultmaven.models.interfaces import ISanitizer


async def sanitize_item_title(sanitizer: ISanitizer, title: str) -> str:
    """Return ``title`` with whatever the sanitizer detects redacted.

    The sanitizer's regex patterns always apply; Presidio's entities apply when
    its analyzer answers. Idempotent on its own output (a placeholder is not a
    detectable span), which is what lets ``upload_document`` scrub the title and
    ``ingest_runbook`` scrub it again without the second pass changing it.

    Raises whatever the sanitizer raises, ``RedactionUnavailableError`` under a
    fail-closed posture included: a title that cannot be scrubbed is not
    written, and the caller has written nothing yet.
    """
    return await sanitizer.asanitize(title)
