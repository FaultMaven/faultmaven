"""
Shared utilities for Tier 1 extractors.

Provides:
- Token budget constants
- Input validation (empty/whitespace guard)
- Output truncation (keep beginning + end, truncate middle)
"""

# Token budget: approximate maximum output size for any extractor.
# Individual extractors may set lower budgets but should never exceed this.
MAX_STRUCTURAL_INDEX_TOKENS = 2500
MAX_STRUCTURAL_INDEX_CHARS = (
    MAX_STRUCTURAL_INDEX_TOKENS * 4
)  # ~10000 chars at 4 chars/token

# Standardized response for empty/degenerate input
EMPTY_CONTENT_RESPONSE = "[No content to analyze]"

# Minimum content length to attempt extraction
_MIN_CONTENT_LENGTH = 10


def has_content(content: str) -> bool:
    """Check if content is non-empty and worth analyzing.

    Returns False for empty strings, whitespace-only strings,
    and strings shorter than 10 characters after stripping.
    """
    if not content:
        return False
    stripped = content.strip()
    return len(stripped) >= _MIN_CONTENT_LENGTH


def truncate_output(text: str, max_chars: int = MAX_STRUCTURAL_INDEX_CHARS) -> str:
    """Truncate text to fit within a character budget.

    Strategy: keep the first 40% and last 40% of the budget,
    insert a marker in the middle showing how much was removed.
    This preserves context from both the beginning (headers, first
    findings) and end (summaries, final results) of extractor output.

    Args:
        text: The output text to truncate.
        max_chars: Maximum character budget (default: MAX_STRUCTURAL_INDEX_CHARS).

    Returns:
        The text unchanged if within budget, or truncated with a marker.
    """
    if len(text) <= max_chars:
        return text

    keep_start = int(max_chars * 0.4)
    keep_end = int(max_chars * 0.4)
    removed = len(text) - keep_start - keep_end

    marker = f"\n\n... [Truncated {removed} characters for context budget] ...\n\n"

    return text[:keep_start] + marker + text[-keep_end:]
