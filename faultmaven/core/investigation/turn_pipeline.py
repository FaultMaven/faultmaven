"""Turn Pipeline — Pre-LLM processing utilities for the unified ingestion pipeline.

Provides utilities for the two-step turn processing pipeline:
Step 1 (pre-LLM): Attachment preprocessing, implicit query generation
Step 2 (LLM): Handled by MilestoneEngine
"""

from typing import TYPE_CHECKING, List

from faultmaven.modules.case.contracts import is_minted_filename

if TYPE_CHECKING:
    from faultmaven.modules.case.domain.models import UploadedFile


def submitted_name(submitted_filename: "str | None", uf: "UploadedFile") -> str:
    """The name to show for THIS submission — not simply ``uf.display_name``.

    Content-hash dedup reuses an existing row, and it matches on the hash
    ALONE, not the filename. So a user who re-uploads identical bytes as
    ``nginx-2026-07-10.log`` gets back the row they first submitted as
    ``nginx-2026-07-09.log``, and naming the submission from that row tells
    them they sent a file they did not send. The name the user chose is the
    right answer for anything describing what they just did.

    A minted name is the one case where the row wins: there the submitted
    filename is the storage artifact #666 is about, and the row's
    ``display_name`` is the only user-meaningful name either side has.

    The branch is on the SUBMITTED NAME, not on ``uf.has_synthetic_filename``.
    Those differ exactly when dedup crosses kinds -- paste the contents of a
    file you uploaded earlier and the stored row is a real file while the
    submitted name is minted -- and asking the row there returned the minted
    name verbatim, in the user's own voice (#1198 review).
    """
    if submitted_filename and not is_minted_filename(submitted_filename):
        return submitted_filename
    return uf.display_name


def _name_in_users_voice(submitted_filename: "str | None", uf: "UploadedFile") -> str:
    """How the implicit query refers to one submitted item.

    Sentence register, not the citable-identifier register: this text is
    written as the USER's turn and shown back to them, so a paste is "the
    text you pasted", not the ``display_name`` identifier "pasted text
    (turn 3)" — and never the minted ``pasted-content-<ts>.txt``, which is
    #666's most literal form (the user reads that they submitted a file
    they never had).
    """
    return uf.submission_phrase or submitted_name(submitted_filename, uf)


def generate_implicit_query(
    uploaded_files: List["UploadedFile"],
    submitted_filenames: "list[str | None] | None" = None,
) -> str:
    """Generate a system query when data is submitted without a question.

    When a user submits attachments but no query text, this function
    creates an implicit query so the LLM knows to analyze the submitted
    data. Post-010: reads classification from ``UploadedFile.data_type``
    (was on the auto-Evidence row in the dual-path model).

    ``submitted_filenames`` is positionally parallel to ``uploaded_files``
    (the caller builds both from one pass over the turn's attachments) and
    carries the name the user actually gave each item. It is needed because
    dedup can hand back a row the user named differently — see
    ``submitted_name``. Omitted, every item falls back to its row's name,
    which is right for pastes and stale for a re-named re-upload.

    Args:
        uploaded_files: UploadedFile rows created from preprocessing
        submitted_filenames: parallel list of the names the user submitted

    Returns:
        Implicit query string for the LLM
    """
    given = list(submitted_filenames or [None] * len(uploaded_files))
    if len(given) != len(uploaded_files):
        given = [None] * len(uploaded_files)

    if len(uploaded_files) == 1:
        uf = uploaded_files[0]
        data_type_label = uf.data_type or "unclassified data"
        if uf.submission_phrase is not None:
            # "I've submitted the text you pasted" is not English. A paste
            # is something the user DID, so the verb carries it.
            verb = "captured a page" if uf.is_page_capture else "pasted some text"
            return (
                f"I've {verb} "
                f"(classified as {data_type_label}). "
                f"Analyze this data and tell me what you find."
            )
        return (
            f"I've submitted {submitted_name(given[0], uf)} "
            f"(classified as {data_type_label}). "
            f"Analyze this data and tell me what you find."
        )
    names = ", ".join(
        _name_in_users_voice(g, uf) for g, uf in zip(given, uploaded_files)
    )
    return (
        f"I've submitted {len(uploaded_files)} items: {names}. "
        f"Analyze this data and tell me what you find."
    )
