"""Frontmatter extraction utilities for knowledge base documents.

Extracts RAG-relevant metadata from YAML frontmatter in markdown documents.
Used by both ingestion paths (KnowledgeIngester and KnowledgeService) to
enrich chunk metadata for hybrid search, reranking, and staleness-aware synthesis.
"""

import logging
import re
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Scalar fields: extracted as-is (str conversion)
_SCALAR_FIELDS = ("domain", "service", "last_updated", "status", "severity")

# List fields: joined to comma-separated string for ChromaDB metadata
_LIST_FIELDS = ("symptom_class",)

# The ONE frontmatter grammar. Nine copies of this regex used to live across
# the knowledge services, and they drifted: #1395 fixed four of them and left
# five, so a document's YAML was stripped by the chunker but counted as body by
# the validator. Every site imports from here now.
#
# Two properties the spelling is load-bearing for:
#
# * `[ \t]*` not `\s*` before the line ending. `\s` matches `\n`, so `\s*\n`
#   admits many ways to split the same text and the lazy `(.*?)` between the
#   delimiters rescans every one of them — quadratic in document size, on
#   CALLER-SUPPLIED content up to MAX_UPLOAD_SIZE_MB (py/polynomial-redos).
# * `\r?\n` not `\n`. `\s` used to match the `\r` of a CRLF line ending by
#   accident; narrowing to `[ \t]*` alone silently dropped CRLF support, so a
#   Windows-authored runbook's frontmatter stopped parsing entirely and every
#   REQUIRED_METADATA check fired on content that had the metadata. The line
#   ending is matched explicitly instead of being left to `\s`.
#
# Trailing blank lines after a delimiter are NOT part of the block (the old
# `\s*\n` ate them). That ambiguity is exactly what made it quadratic, and YAML
# tolerates the leading newline landing in the body instead.
_DELIMITER = r"---[ \t]*\r?\n"
FRONTMATTER_RE = re.compile(rf"^{_DELIMITER}(.*?)\r?\n{_DELIMITER}", re.DOTALL)

# Backwards-compatible alias for the private name this module used to export.
_FRONTMATTER_PATTERN = FRONTMATTER_RE


def match_frontmatter(content: str) -> Optional[re.Match]:
    """The frontmatter block at the start of ``content``, or ``None``.

    Group 1 is the raw YAML body, without either delimiter line.
    """
    return FRONTMATTER_RE.match(content)


def strip_frontmatter(content: str) -> str:
    """``content`` with a leading frontmatter block removed, if it has one.

    Anchored at position 0, so a `---` rule further down the document is left
    alone — a horizontal rule is body content, not a second frontmatter block.
    """
    return FRONTMATTER_RE.sub("", content, count=1)


def parse_frontmatter(content: str) -> Dict[str, object]:
    """The frontmatter parsed as YAML, or ``{}`` if absent or unparseable.

    Callers that need to tell "no frontmatter" from "empty frontmatter" should
    use :func:`match_frontmatter` instead — this collapses both to ``{}``.
    """
    match = match_frontmatter(content)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception as e:  # yaml raises a family, not one class
        logger.warning(f"Failed to parse frontmatter YAML: {e}")
        return {}


def extract_frontmatter_metadata(content: str) -> Dict[str, str]:
    """Extract RAG-relevant metadata from YAML frontmatter.

    Extracts domain, service, last_updated, status, severity, and
    symptom_class from runbook frontmatter. List fields (symptom_class)
    are joined to comma-separated strings.

    Args:
        content: Full document content (markdown with optional YAML frontmatter).

    Returns:
        Dict with string values for any RAG fields found. Empty dict if
        no frontmatter or parsing fails.
    """
    fm_match = match_frontmatter(content)
    if not fm_match:
        return {}

    try:
        fm = yaml.safe_load(fm_match.group(1)) or {}
    except Exception as e:
        logger.warning(f"Failed to parse frontmatter YAML: {e}")
        return {}

    result: Dict[str, str] = {}

    for key in _SCALAR_FIELDS:
        if key in fm and fm[key] is not None:
            result[key] = str(fm[key])

    for key in _LIST_FIELDS:
        val = fm.get(key)
        if val is not None:
            if isinstance(val, list):
                joined = ",".join(str(v) for v in val if v is not None)
                if joined:
                    result[key] = joined
            elif isinstance(val, str) and val:
                result[key] = val

    return result
