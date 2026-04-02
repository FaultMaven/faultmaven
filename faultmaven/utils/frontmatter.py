"""Frontmatter extraction utilities for knowledge base documents.

Extracts RAG-relevant metadata from YAML frontmatter in markdown documents.
Used by both ingestion paths (KnowledgeIngester and KnowledgeService) to
enrich chunk metadata for hybrid search, reranking, and staleness-aware synthesis.
"""

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

# Scalar fields: extracted as-is (str conversion)
_SCALAR_FIELDS = ("domain", "service", "last_updated", "status", "severity")

# List fields: joined to comma-separated string for ChromaDB metadata
_LIST_FIELDS = ("symptom_class",)

# Compiled pattern for frontmatter block
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


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
