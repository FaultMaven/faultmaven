"""The one bound on a runbook draft's text (#1950).

A draft's text is a database row (``conversion_drafts.content``), and a row a
user cannot save back is a draft they cannot edit. So every write of a draft's
own text is held to one bound: ``ConversionDraftModel``'s ``content`` validator
enforces it on every ORM write, and each writer that can see its input early
(the edit request, the template create, a conversion, the KB upload, the scan)
checks it first with this same guard, to refuse before work is spent.

Verify's frontmatter rewrite is the one write exempt from it: it marks a draft
the user was allowed to save as verified, which can lengthen the text by a few
characters (``status: verified``, the verifier's name). It is a compare-and-swap
``UPDATE`` the ORM validator does not see, so a draft at the bound still
verifies.
"""

from faultmaven.exceptions import ValidationException

#: The longest draft text, in characters. Sized above every draft the system
#: makes or imports: a converted draft stops at ``RUNBOOK_MAX_TOKENS_CEILING``
#: (8192 tokens, some 33,000 characters), and the hand-written runbooks the scan
#: imports run longer — the largest shipped one is 48,354 bytes. Twice that,
#: rounded, leaves room to edit any of them.
DRAFT_CONTENT_MAX_CHARS = 100_000


def draft_content_too_long_message(length: int) -> str:
    """The refusal's sentence: the length and the bound, nothing of the text."""
    return (
        f"A runbook draft holds at most {DRAFT_CONTENT_MAX_CHARS} characters; "
        f"this one has {length}."
    )


def ensure_draft_content_within_bound(content: str) -> str:
    """Return ``content``, or raise ``ValidationException`` (422) when it is
    longer than ``DRAFT_CONTENT_MAX_CHARS``."""
    if content is not None and len(content) > DRAFT_CONTENT_MAX_CHARS:
        raise ValidationException(draft_content_too_long_message(len(content)))
    return content
