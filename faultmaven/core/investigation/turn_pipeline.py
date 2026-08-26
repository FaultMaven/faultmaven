"""Turn Pipeline — Pre-LLM processing utilities for the unified ingestion pipeline.

Provides utilities for the two-step turn processing pipeline:
Step 1 (pre-LLM): Attachment preprocessing, implicit query generation
Step 2 (LLM): Handled by MilestoneEngine
"""

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from faultmaven.core.investigation.schemas import Attachment
    from faultmaven.modules.case.domain.models import UploadedFile


def generate_implicit_query(
    attachments: List["Attachment"], uploaded_files: List["UploadedFile"]
) -> str:
    """Generate a system query when data is submitted without a question.

    When a user submits attachments but no query text, this function
    creates an implicit query so the LLM knows to analyze the submitted
    data. Post-010: reads classification from ``UploadedFile.data_type``
    (was on the auto-Evidence row in the dual-path model).

    The string is written in the user's voice and shown back to them as
    their turn, so it names each item by ``display_name`` rather than by
    ``filename`` — a paste has no filename the user would recognise, and
    putting the minted one here is #666's leak in the most literal form:
    the user reading "I've submitted pasted-content-20260709T105531.txt"
    never submitted any such thing. ``attachments`` is retained for the
    caller's signature and for its 1:1 correspondence with
    ``uploaded_files``; names come from the file rows because only those
    carry the classification the display name is built from.

    Args:
        attachments: Original attachments from the turn payload
        uploaded_files: UploadedFile rows created from preprocessing

    Returns:
        Implicit query string for the LLM
    """
    if len(uploaded_files) == 1:
        uf = uploaded_files[0]
        if uf.has_synthetic_filename:
            # display_name already carries the classification ("pasted
            # logs"); restating it would read "pasted logs (classified as
            # logs)".
            return (
                f"I've submitted {uf.display_name}. "
                f"Analyze this data and tell me what you find."
            )
        data_type_label = uf.data_type or "unclassified data"
        return (
            f"I've submitted {uf.display_name} "
            f"(classified as {data_type_label}). "
            f"Analyze this data and tell me what you find."
        )
    names = ", ".join(uf.display_name for uf in uploaded_files)
    return (
        f"I've submitted {len(uploaded_files)} items: {names}. "
        f"Analyze this data and tell me what you find."
    )
