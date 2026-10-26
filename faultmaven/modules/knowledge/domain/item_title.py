"""A knowledge item's title is scrubbed the same way on create as on update (#1981).

A title is shown to every reader of the tier its item lives in (team and global
included), in every list, and nothing ever reverses it. It is also free text
someone typed (an upload's ``title`` form field, a manual runbook's title) or a
model wrote (a draft built from a case). So it passes through the deployment's
sanitizer before any row or vector carries it. The body does not: a body is
procedure text, which redaction cut (``get-login-password <PASSWORD_…>``), so a
body holding a credential is refused instead and never rewritten
(``enforce_runbook_security``, #2012). A title is a label, and is redacted.

Until #1981 only ``update_document`` did. A runbook created with a title like
``Fix login (password=hunter2)`` stored and displayed it verbatim, while the
same string typed into an edit was redacted.

Every writer of a title calls :func:`sanitize_item_title`, and every one does it
at the point where the title ENTERS as raw text (user input, model output, a case
title), assigns the scrubbed value back to the object it came in on, and derives
everything else from that value (the draft's id slug, its file name, the text of
a runbook made from a template, a log line, a case-transcript notice, the API
response). Scrubbing a derived copy instead does nothing: ``password-hunter2``
has no ``=`` left for a pattern to match.

* ``KnowledgeService.ingest_runbook``: the one place a ``knowledge_items`` row is
  created. Uploads, published conversion drafts and the KB pack all reach it.
  The pack alone is exempt (``shipped=True``): its titles are vendored, reviewed
  source, not user or model text, and a redaction outage that begins after a
  healthy start must not be able to stop the platform corpus from seeding.
* ``KnowledgeService.upload_document``: writes the upload row and the draft row
  from the title BEFORE ``ingest_runbook`` runs, so it scrubs first and every
  row it writes holds the same text.
* ``KnowledgeService.update_document_metadata``.
* ``ConversionService``: a failure mode's title as soon as the model has written
  it (``convert_document``), a case's title as it arrives (``convert_from_case``)
  and the title in the frontmatter the model writes for a case
  (``_convert_single_failure_mode``), each BEFORE the draft's id is minted from
  it; ``create_runbook_from_template`` (a title the user typed); the disk scan,
  which derives nothing from the title and so enters at the persistence choke;
  ``_persist_job_rows`` (the one place a ``conversion_drafts`` row is written, a
  backstop that also assigns the stored value back to the draft it returns) and
  ``verify_draft``. A draft is NOT author-only: ``GET /knowledge/drafts`` lists
  every global-scope draft to every user and a case's drafts are listed to its
  readers.

A fifth writer belongs here as well.
"""

from typing import Any

from faultmaven.models.interfaces import ISanitizer
from faultmaven.modules.knowledge.exceptions import ItemTitleScrubUnavailable

#: How many times the sanitizer may be run over one title before it is judged
#: not to settle. The copies of a title must be one text, so each is scrubbed to
#: a fixed point, whatever the sanitizer's own idempotence. The regex half has
#: it since #1980: a detection that needs its neighbour gone
#: (``00:1A:2B:3C:4D:5510.0.0.5password=hunter2``: the password waits on the IP,
#: which waits on the MAC) is settled inside one call, so such a title takes one
#: changing call and one more to see nothing change — two. Main's patterns
#: settled it one layer per call: four. The loop stays for a sanitizer without
#: the property (an analyzer's half, a future pattern); the bound is six, so
#: four layers still have slack.
MAX_SCRUB_PASSES = 6


def _unavailable(reason: str) -> ItemTitleScrubUnavailable:
    """Build the fail-closed refusal. The reason is a type or a fixed phrase,
    never the text: it is logged, and it is what a stack trace carries."""
    return ItemTitleScrubUnavailable(
        f"The sensitive-data check on titles could not run ({reason}); "
        "nothing was written"
    )


def require_sanitizer(sanitizer: Any) -> None:
    """Refuse up front when there is no sanitizer, before any work is spent.

    A conversion makes model calls before it has a title to scrub; a deployment
    with no sanitizer wired would pay for them and then refuse, so the entry
    points check first. ``sanitize_item_title`` still refuses on its own.
    """
    if sanitizer is None:
        raise _unavailable("no sanitizer is wired")


async def sanitize_item_title(
    sanitizer: ISanitizer, title: str, column: Any = None
) -> str:
    """Return ``title`` with everything the sanitizer detects redacted, settled.

    The sanitizer is run until its output is stable (at most
    :data:`MAX_SCRUB_PASSES` times), so the result is a fixed point: running the
    helper again returns it unchanged. That is what lets one title be scrubbed
    by ``upload_document`` and again by ``ingest_runbook`` and still be one
    string in every row. A clean title costs one call.

    The sanitizer's regex patterns always apply; Presidio's entities apply when
    its analyzer answers.

    ``column`` is the ``String(n)`` column the result is about to be written to.
    It is cut AFTER each scrub, and the cut text is scrubbed again until the
    two agree. Never cut first: a secret that straddles the cut is left as a
    fragment no pattern matches (the first 21 characters of a 32-character
    key), and it survives. Scrubbing can also make a title LONGER (a
    ten-character ``password=x`` becomes a 27-character placeholder), and
    PostgreSQL refuses a value past the column's length where SQLite keeps it.

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
        if column is not None:
            scrubbed = fit_to_column(scrubbed, column)
        if scrubbed == current:
            return scrubbed
        current = scrubbed
    raise _unavailable(f"it did not settle in {MAX_SCRUB_PASSES} passes")


def fit_to_column(text: str, column: Any) -> str:
    """``text`` cut to the length of a ``String(n)`` column.

    Only ever applied to text that has already been scrubbed (see
    :func:`sanitize_item_title`'s ``column``), so what is cut is only ever
    scrubbed text and no secret can come back by truncation.
    """
    length = column.type.length
    return text if length is None else text[:length]
