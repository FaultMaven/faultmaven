"""Frontmatter extraction utilities for knowledge base documents.

Extracts RAG-relevant metadata from YAML frontmatter in markdown documents.
Used by ``KnowledgeService`` and the conversion service to enrich chunk
metadata for hybrid search, reranking, and staleness-aware synthesis.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import yaml
from frontmatter import Post, YAMLHandler
from frontmatter import loads as _post_loads

from faultmaven.utils.exception_name import exception_log_name

logger = logging.getLogger(__name__)

#: The deepest nesting a frontmatter block may have: the top-level mapping is 1
#: and a field's list 2, which is the deepest every shipped runbook's
#: frontmatter reaches (91 in ``resources/knowledge/pack``, 99 in the kb
#: toolkit, measured 2026-11-26); one more level lets a list item that is
#: itself a list get its field's own refusal. ``frontmatter.dumps`` re-emits a
#: nested flow sequence one indent per level, so verify's re-emission grows
#: with the nesting (an 81,504-character draft published 12 MB, #2674).
MAX_FRONTMATTER_DEPTH = 3

#: The gate's refusals of what no frontmatter load accepts (#2674).
ANCHOR_REFUSAL = (
    "YAML anchors and aliases (&name, *name) are not allowed in runbook "
    "frontmatter; write each value out in full"
)
DEPTH_REFUSAL = (
    f"Runbook frontmatter may nest at most {MAX_FRONTMATTER_DEPTH} levels "
    "(a field, its list, a list inside it); write deeper values out flat"
)

#: libyaml's loader where the C extension is built, as python-frontmatter
#: used; the pure loader only where it is absent.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class FrontmatterRefused(yaml.YAMLError):
    """Frontmatter no load accepts, refused from its parse events before any
    value is built: a value whose re-emission or string form the author's
    few characters could make enormous (#2674)."""


class FrontmatterAnchorRefused(FrontmatterRefused):
    """An anchor or alias: an alias names a shared value whose string form
    grows with every level of aliases."""


class FrontmatterTooDeep(FrontmatterRefused):
    """Nesting past ``MAX_FRONTMATTER_DEPTH``."""


def _refuse_unbounded(text: str) -> None:
    """Raise ``FrontmatterRefused`` at the first anchor, alias or level past
    ``MAX_FRONTMATTER_DEPTH`` among ``text``'s parse events."""
    depth = 0
    for event in yaml.parse(text, Loader=_LOADER):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise FrontmatterAnchorRefused(ANCHOR_REFUSAL)
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > MAX_FRONTMATTER_DEPTH:
                raise FrontmatterTooDeep(DEPTH_REFUSAL)
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1


def load_frontmatter_yaml(text: str) -> Any:
    """A frontmatter block's YAML, loaded as ``yaml.safe_load`` loads it but
    refusing any anchor, alias or nesting past ``MAX_FRONTMATTER_DEPTH``
    (``FrontmatterRefused``): every frontmatter load in the codebase goes
    through here or :func:`load_post`."""
    _refuse_unbounded(text)
    return yaml.load(text, Loader=_LOADER)  # noqa: S506 - a safe loader


class _BoundedHandler(YAMLHandler):
    """python-frontmatter's YAML handler, loading with :func:`load_frontmatter_yaml`."""

    def load(self, fm: str, **kwargs: object) -> Any:
        return load_frontmatter_yaml(fm)


def load_post(content: str) -> Post:
    """``frontmatter.loads(content)`` with :func:`load_frontmatter_yaml` as its
    YAML loader. The handler is passed only when it detects its format, as
    ``frontmatter.loads`` chooses one: forced on a text that does not open with
    ``---`` (a leading BOM), it would split a body rule as frontmatter. It
    detects on the stripped text, as ``frontmatter.loads`` parses it, so a
    leading newline cannot hand the block to the stock handler."""
    handler = _BoundedHandler()
    detected = handler.detect(content.strip())
    return _post_loads(content, handler=handler if detected else None)


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
# place that decides where frontmatter starts; `preparation` strips a
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
        loaded = load_frontmatter_yaml(match.group(1))
    except Exception as e:  # yaml raises a family, not one class
        logger.warning(f"Failed to parse frontmatter YAML: {exception_log_name(e)}")
        return {}
    # `or {}` is not enough. A frontmatter body that is valid YAML but not a
    # MAPPING -- `---\ndomain of the service\n---` parses to a plain string --
    # used to be handed back as-is, and every consumer subscripts it:
    # `if key in fm` is a SUBSTRING test on a str, and `fm[key]` then raises
    # TypeError. That reached an unhandled 500 from `validate_content`,
    # `score_content` and `extract_frontmatter_metadata` on caller-supplied
    # content. `preparation` already guarded for this locally; the
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
