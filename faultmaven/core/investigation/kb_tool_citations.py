"""The runbooks ``kb_qa`` answered from, cited in the turn's ``sources``.

The pull channel (``kb_push.py`` names the three) used to reach the user only
as a title list inside the model's tool result: ``kb_qa`` returned plain text,
and a ``Source`` without a document id is dropped by the citation check, so a
reply grounded in a runbook the model fetched cited nothing. Here each section
the answer cited becomes an entry of the same shape the push and the question
lookup report (``TURN_METADATA_KB_PROMPTED``), with ``trigger`` ``kb_qa``.

They are added only when the reply the turn returns came from the tool loop
that saw them (``KbCitations.confirm``): a no-tools fallback, or the minimal
retry after a context-length rejection, answered without them, and citing a
runbook the model was not shown is worse than citing none.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from faultmaven.core.investigation.kb_question import (
    KB_QA_TRIGGER,
    KB_QUESTION_TRIGGER,
    excerpt_of,
    section_key,
    take_over_pushed,
)

#: At most this many sections are cited from a turn's kb_qa calls, and this
#: many per runbook: the question lookup's caps, so a turn that calls kb_qa
#: several times on one topic does not flood the list with one runbook.
KB_QA_MAX_ENTRIES = 5
KB_QA_MAX_PER_RUNBOOK = 2


def kb_qa_entry(chunk: Any) -> Optional[dict[str, Any]]:
    """One cited chunk as a reported entry (the push's and lookup's shape), or
    ``None`` for a chunk that cannot be cited: bookkeeping never fails the
    answer the tool already produced."""
    if not isinstance(chunk, dict):
        return None
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        return None
    content = chunk.get("content")
    score = chunk.get("score")
    return {
        "title": metadata.get("title") or metadata.get("document_title"),
        "parent_document_id": metadata.get("parent_document_id"),
        "summary": excerpt_of(content if isinstance(content, str) else None),
        "score": float(score) if isinstance(score, (int, float)) else None,
        "trigger": KB_QA_TRIGGER,
    }


def _key(entry: dict[str, Any]) -> tuple[str, str]:
    return section_key(entry.get("parent_document_id"), entry.get("summary"))


def add_kb_qa_entries(rendered: list, cited: Iterable[dict[str, Any]]) -> None:
    """Add ``cited`` to the turn's report ``rendered``, each section once, the
    best-scored first, within ``KB_QA_MAX_ENTRIES`` and
    ``KB_QA_MAX_PER_RUNBOOK``.

    A section the question lookup also carries stays the lookup's: it answers
    what the user asked. A section the push carried becomes ``kb_qa``'s: the
    model fetched it to answer this turn, so it is new this turn whatever
    earlier turns showed. Entries without a document id are not citable and
    are left out. Changes ``rendered`` in place: it is the list the engine
    reports.
    """
    lookup = {
        _key(e)
        for e in rendered
        if isinstance(e, dict) and e.get("trigger") == KB_QUESTION_TRIGGER
    }
    added: list[dict[str, Any]] = []
    seen = set(lookup)
    per_runbook: dict[str, int] = {}
    best_first = sorted(cited, key=lambda e: -(e.get("score") or 0.0))
    for entry in best_first:
        key = _key(entry)
        runbook = entry.get("parent_document_id")
        if not runbook or key in seen:
            continue
        if per_runbook.get(runbook, 0) >= KB_QA_MAX_PER_RUNBOOK:
            continue
        seen.add(key)
        per_runbook[runbook] = per_runbook.get(runbook, 0) + 1
        added.append(entry)
        if len(added) == KB_QA_MAX_ENTRIES:
            break
    if added:
        rendered[:] = take_over_pushed(rendered, added) + added
