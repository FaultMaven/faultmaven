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
# A leading UTF-8 BOM does not stop a document having frontmatter. Both readers
# that produce text here preserve it — `Path.read_text(encoding="utf-8")` and a
# `bytes.decode("utf-8")` alike, since only `utf-8-sig` strips one — and this
# pattern is `re.match`-anchored at offset 0, so a single invisible character
# made every REQUIRED_METADATA check fire on a document carrying all of them
# ("No YAML frontmatter found", score 94.0 -> 80.5, a 422 from
# `POST /knowledge/documents`). "UTF-8 with BOM" is the default save of several
# Windows editors, so this is the other half of the Windows-authoring story
# #1403 is about: that issue fixed the CRLF half, and a BOM+CRLF file — the
# actual Windows default — was still refused until this.
#
# Tolerated here rather than stripped at a boundary because this is the ONE
# place that decides where frontmatter starts; `document_preprocessor` strips a
# BOM via `_LEADING_NOISE` but only on the CONVERSION path, which is why the
# upload path never got it. Optional and zero-width, so it changes nothing for
# a document without one.
_BOM = "﻿?"
_DELIMITER = r"---[ \t]*\r?\n"
FRONTMATTER_RE = re.compile(rf"^{_BOM}{_DELIMITER}(.*?)\r?\n{_DELIMITER}", re.DOTALL)


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
        loaded = yaml.safe_load(match.group(1))
    except Exception as e:  # yaml raises a family, not one class
        logger.warning(f"Failed to parse frontmatter YAML: {e}")
        return {}
    # `or {}` is not enough. A frontmatter body that is valid YAML but not a
    # MAPPING -- `---\ndomain of the service\n---` parses to a plain string --
    # used to be handed back as-is, and every consumer subscripts it:
    # `if key in fm` is a SUBSTRING test on a str, and `fm[key]` then raises
    # TypeError. That reached an unhandled 500 from `validate_content`,
    # `score_content` and `extract_frontmatter_metadata` on caller-supplied
    # content. `document_preprocessor` already guarded for this locally; the
    # guard belongs here, where there is one copy of it.
    return loaded if isinstance(loaded, dict) else {}


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
    fm = parse_frontmatter(content)

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
