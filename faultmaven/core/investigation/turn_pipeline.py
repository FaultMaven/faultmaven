"""Turn Pipeline — Pre-LLM processing utilities for the unified ingestion pipeline.

Provides utilities for the two-step turn processing pipeline:
Step 1 (pre-LLM): Attachment preprocessing, implicit query generation
Step 2 (LLM): Handled by MilestoneEngine
"""

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from faultmaven.core.investigation.schemas import Attachment
    from faultmaven.modules.case.domain.models import Evidence


def generate_implicit_query(
    attachments: List["Attachment"], evidence: List["Evidence"]
) -> str:
    """Generate a system query when data is submitted without a question.

    When a user submits attachments but no query text, this function creates
    an implicit query so the LLM knows to analyze the submitted data.

    Args:
        attachments: Original attachments from the turn payload
        evidence: Evidence records created from preprocessing

    Returns:
        Implicit query string for the LLM
    """
    if len(evidence) == 1:
        ev = evidence[0]
        filename = attachments[0].filename if attachments else "data"
        return (
            f"I've submitted {filename} "
            f"(classified as {ev.data_type}). "
            f"Analyze this data and tell me what you find."
        )
    filenames = ", ".join(att.filename for att in attachments)
    return (
        f"I've submitted {len(evidence)} files: {filenames}. "
        f"Analyze this data and tell me what you find."
    )
