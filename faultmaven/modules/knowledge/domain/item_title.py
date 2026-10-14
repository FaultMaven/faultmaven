"""A knowledge item's title is scrubbed the same way on create as on update (#1981).

A title is shown to every reader of the tier its item lives in (team and global
included), in every list, and nothing ever reverses it. It is also free text
someone typed (an upload's ``title`` form field, a manual runbook's title) or a
model wrote (a draft built from a case). So it passes through the deployment's
sanitizer, the one the body passes through when a document is updated, before
any row or vector carries it.

Until #1981 only ``update_document`` did. A runbook created with a title like
``Fix login (password=hunter2)`` stored and displayed it verbatim, while the
same string typed into an edit was redacted.

Every writer of a title calls :func:`sanitize_item_title`:

* ``KnowledgeService.ingest_runbook``: the one place a ``knowledge_items`` row is
  created. Uploads, published conversion drafts and the KB pack all reach it.
  The pack alone is exempt (``shipped=True``): its titles are vendored, reviewed
  source, not user or model text, and a redaction outage that begins after a
  healthy start must not be able to stop the platform corpus from seeding.
* ``KnowledgeService.upload_document``: writes the upload row and the draft row
  from the title BEFORE ``ingest_runbook`` runs, so it scrubs first and every
  row it writes holds the same text.
* ``KnowledgeService.update_document_metadata``.
* ``ConversionService``: ``create_runbook_from_template`` (a title the user
  typed, which also becomes the draft's id slug and the runbook's frontmatter),
  ``_persist_job_rows`` (the one place a ``conversion_drafts`` row is written, so
  a model-written title is covered too) and ``verify_draft``. A draft is NOT
  author-only: ``GET /knowledge/drafts`` lists every global-scope draft to every
  user and a case's drafts are listed to its readers.
* ``KnowledgeIngester.ingest_document`` / ``ingest_document_object``. Nothing
  routes there today (``write_scope`` pins it dead), but they are the shape the
  next publish path will have, and a revived one must not be the one that
  publishes a title unscrubbed.

A fifth writer belongs here as well.
"""

from typing import Any

from faultmaven.models.interfaces import ISanitizer
from faultmaven.modules.knowledge.exceptions import ItemTitleScrubUnavailable

#: How many times the sanitizer may be run over one title before it is judged
#: not to settle. The patterns run in a fixed order, and an earlier one can be
#: blocked by text a later one removes: ``\bpassword[=:]`` needs a word boundary,
#: and in ``10.0.0.5password=hunter2`` the digit before ``p`` is none until the
#: IP pattern (later in the list) has replaced the address. One pass is then not
#: its own fixed point, and the copies of a title that were scrubbed a different
#: number of times diverged. Fuzzed over 30,000 mixed titles, nothing needed more
#: than three passes to be stable.
MAX_SCRUB_PASSES = 4


def _unavailable(reason: str) -> ItemTitleScrubUnavailable:
    """Build the fail-closed refusal. The reason is a type or a fixed phrase,
    never the text: it is logged, and it is what a stack trace carries."""
    return ItemTitleScrubUnavailable(
        f"The sensitive-data check on titles could not run ({reason}); "
        "nothing was written"
    )


async def sanitize_item_title(sanitizer: ISanitizer, title: str) -> str:
    """Return ``title`` with everything the sanitizer detects redacted, settled.

    The sanitizer is run until its output is stable (at most
    :data:`MAX_SCRUB_PASSES` times), so the result is a fixed point: running the
    helper again returns it unchanged. That is what lets one title be scrubbed
    by ``upload_document`` and again by ``ingest_runbook`` and still be one
    string in every row. A clean title costs one call.

    The sanitizer's regex patterns always apply; Presidio's entities apply when
    its analyzer answers.

    Raises:
        ItemTitleScrubUnavailable: fail closed, with nothing to compensate
            because every caller runs this before its first write. Raised when
            there is no sanitizer, when the sanitizer raises anything
            (``RedactionUnavailableError`` under a fail-closed posture and
            ``PseudonymKeyUnavailableError`` among them; the cause is chained),
            when it answers with something that is not text, and when it has not
            settled after ``MAX_SCRUB_PASSES`` passes.
    """
    if sanitizer is None:
        raise _unavailable("no sanitizer is wired")
    current = title
    for _ in range(MAX_SCRUB_PASSES):
        try:
            scrubbed = await sanitizer.asanitize(current)
        except Exception as error:
            raise _unavailable(type(error).__name__) from error
        if not isinstance(scrubbed, str):
            raise _unavailable(f"it answered {type(scrubbed).__name__}, not text")
        if scrubbed == current:
            return scrubbed
        current = scrubbed
    raise _unavailable(f"it did not settle in {MAX_SCRUB_PASSES} passes")


def fit_to_column(text: str, column: Any) -> str:
    """``text`` cut to the length of a ``String(n)`` column.

    Scrubbing can make a title LONGER (a ten-character ``password=x`` becomes a
    27-character placeholder), and PostgreSQL refuses a value past the column's
    length where SQLite keeps it. Cut after the scrub, so what is cut is only
    ever the scrubbed text: no secret can come back by truncation.
    """
    length = column.type.length
    return text if length is None else text[:length]
