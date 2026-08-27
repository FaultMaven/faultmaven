"""Turn Pipeline — Pre-LLM processing utilities for the unified ingestion pipeline.

Provides utilities for the two-step turn processing pipeline:
Step 1 (pre-LLM): Attachment preprocessing, implicit query generation
Step 2 (LLM): Handled by MilestoneEngine
"""

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from faultmaven.modules.case.domain.models import UploadedFile


def _name_in_users_voice(uf: "UploadedFile") -> str:
    """How this string refers to one submitted item.

    Sentence register, not the citable-identifier register: this text is
    written as the USER's turn and shown back to them, so a paste is "the
    text you pasted", not the ``display_name`` identifier "pasted text
    (turn 3)" — and never the minted ``pasted-content-<ts>.txt``, which is
    #666's most literal form (the user reads that they submitted a file
    they never had).
    """
    return uf.submission_phrase or uf.filename


def generate_implicit_query(uploaded_files: List["UploadedFile"]) -> str:
    """Generate a system query when data is submitted without a question.

    When a user submits attachments but no query text, this function
    creates an implicit query so the LLM knows to analyze the submitted
    data. Post-010: reads classification from ``UploadedFile.data_type``
    (was on the auto-Evidence row in the dual-path model).

    Takes only the file rows. It previously also took the raw attachments
    and read names off those; the two lists are parallel, but only the rows
    carry the provenance the naming turns on, and preprocessing can drop an
    attachment (content-hash dedup) while the count came from the rows.

    Args:
        uploaded_files: UploadedFile rows created from preprocessing

    Returns:
        Implicit query string for the LLM
    """
    if len(uploaded_files) == 1:
        uf = uploaded_files[0]
        if uf.submission_phrase is not None:
            # "I've submitted the text you pasted" is not English. A paste
            # is something the user DID, so the verb carries it.
            verb = "captured" if uf.is_page_capture else "pasted"
            subject = "a page" if uf.is_page_capture else "some text"
            data_type_label = uf.data_type or "unclassified data"
            return (
                f"I've {verb} {subject} "
                f"(classified as {data_type_label}). "
                f"Analyze this data and tell me what you find."
            )
        data_type_label = uf.data_type or "unclassified data"
        return (
            f"I've submitted {uf.filename} "
            f"(classified as {data_type_label}). "
            f"Analyze this data and tell me what you find."
        )
    names = ", ".join(_name_in_users_voice(uf) for uf in uploaded_files)
    return (
        f"I've submitted {len(uploaded_files)} items: {names}. "
        f"Analyze this data and tell me what you find."
    )
