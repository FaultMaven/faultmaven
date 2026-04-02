"""Frontmatter extraction utilities for knowledge base documents.

Extracts RAG-relevant metadata (domain, service, last_updated, status)
from YAML frontmatter in markdown documents. Used by both ingestion
paths (KnowledgeIngester and KnowledgeService) to enrich chunk metadata.
"""

import logging
import re
from typing import Dict

logger = logging.getLogger(__name__)

# Fields to extract from frontmatter for RAG enrichment
_RAG_FIELDS = ("domain", "service", "last_updated", "status")

# Compiled pattern for frontmatter block
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def extract_frontmatter_metadata(content: str) -> Dict[str, str]:
    """Extract RAG-relevant metadata from YAML frontmatter.

    Pulls domain, service, last_updated, and status from runbook
    frontmatter so they can be stored in chunk metadata for
    hybrid search filtering and staleness-aware synthesis.

    Args:
        content: Full document content (markdown with optional YAML frontmatter).

    Returns:
        Dict with string values for any RAG fields found. Empty dict if
        no frontmatter or parsing fails.
    """
    fm_match = _FRONTMATTER_PATTERN.match(content)
    if not fm_match:
        return {}

    try:
        import yaml

        fm = yaml.safe_load(fm_match.group(1)) or {}
    except Exception as e:
        logger.warning(f"Failed to parse frontmatter YAML: {e}")
        return {}

    result: Dict[str, str] = {}
    for key in _RAG_FIELDS:
        if key in fm and fm[key] is not None:
            result[key] = str(fm[key])
    return result
