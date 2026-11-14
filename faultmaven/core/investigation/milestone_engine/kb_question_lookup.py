"""The knowledge-base question lookup: the search behind
``core/investigation/kb_question.py``'s block (design §5.6 of
``orchestration-capabilities.md``)."""

from __future__ import annotations

import logging
from typing import Any

from faultmaven.core.investigation.kb_question import (
    KB_QUESTION_CANDIDATE_FLOOR,
    KB_QUESTION_FETCH_LIMIT,
    KB_QUESTION_MAX_ENTRIES,
    KB_QUESTION_MAX_OTHER_TITLES,
    KB_QUESTION_MAX_PER_RUNBOOK,
    KB_QUESTION_QUERY_MAX_CHARS,
    KB_QUESTION_TRIGGER,
    QUERY_FROM_MESSAGE,
    QUERY_FROM_PROBLEM_STATEMENT,
    QUERY_FROM_QUESTION,
    KbLookup,
    KbLookupStatus,
    excerpt_of,
)
from faultmaven.core.investigation.milestone_engine.kb_prefetch import (
    _admit_diverse,
    _chunk_label,
)
from faultmaven.core.investigation.milestone_engine.retrieval_scope import (
    case_retrieval_scope,
)

logger = logging.getLogger(__name__)


def _query_for(case: Any, question: dict) -> tuple[str, str]:
    """What to search with, and where it came from.

    The question's topic — which the classifier already took from the rest of
    the message when the question itself named none ("Our nginx returns 502s.
    Is there a runbook for this?"). Failing that, the case's problem statement:
    the confirmed one, or in INQUIRY, where ``description`` is still empty, the
    one proposed to the user.
    """
    if question["topic"]:
        source = (
            QUERY_FROM_MESSAGE if question.get("from_context") else QUERY_FROM_QUESTION
        )
        return question["topic"], source
    inquiry = getattr(case, "inquiry", None)
    for statement in (
        getattr(case, "description", None),
        getattr(inquiry, "proposed_problem_statement", None),
    ):
        statement = (statement or "").strip()
        if statement:
            return statement[:KB_QUESTION_QUERY_MAX_CHARS], QUERY_FROM_PROBLEM_STATEMENT
    return "", ""


async def look_up_kb_question(deps: Any, case: Any, question: dict) -> KbLookup:
    """Search the knowledge base the case's driver can read, for ``question``.

    ``deps`` carries ``knowledge_service``, ``team_service`` and
    ``share_repository`` (the engine's dependencies). Never raises: a failure
    is the ``UNAVAILABLE`` status.
    """
    if question["kind"] == "listing":
        return KbLookup(status=KbLookupStatus.LISTING)
    query, source = _query_for(case, question)
    if not query:
        return KbLookup(status=KbLookupStatus.NO_TOPIC)
    knowledge_service = getattr(deps, "knowledge_service", None)
    if knowledge_service is None:
        return KbLookup(
            status=KbLookupStatus.UNAVAILABLE, query=query, query_source=source
        )
    try:
        # A failed team lookup must not narrow the scope silently: a narrowed
        # search would back a "not found" it did not establish.
        scope = await case_retrieval_scope(
            case,
            team_service=getattr(deps, "team_service", None),
            share_repository=getattr(deps, "share_repository", None),
            raise_on_failure=True,
        )
        results = await knowledge_service.search_knowledge(
            query=query,
            limit=KB_QUESTION_FETCH_LIMIT,
            filters=scope,
            min_score=KB_QUESTION_CANDIDATE_FLOOR,
        )
    except Exception:
        logger.warning(
            "Knowledge-base question lookup failed for case %s",
            getattr(case, "case_id", None),
            exc_info=True,
        )
        return KbLookup(
            status=KbLookupStatus.UNAVAILABLE, query=query, query_source=source
        )

    ranked = [
        r for r in results or [] if (r.score or 0.0) >= KB_QUESTION_CANDIDATE_FLOOR
    ]
    if not ranked:
        return KbLookup(status=KbLookupStatus.NOTHING, query=query, query_source=source)

    shown = _admit_diverse(
        ranked, limit=KB_QUESTION_MAX_ENTRIES, per_runbook=KB_QUESTION_MAX_PER_RUNBOOK
    )
    entries = tuple(
        {
            "title": r.title,
            "section": _chunk_label(r.content or r.snippet),
            "summary": excerpt_of(r.content or r.snippet),
            "score": r.score,
            "type": getattr(r, "document_type", "runbook"),
            "parent_document_id": getattr(r, "parent_document_id", None),
            "trigger": KB_QUESTION_TRIGGER,
        }
        for r in shown
    )
    shown_titles = {e["title"] for e in entries}
    others: list[str] = []
    for r in ranked:
        if r.title not in shown_titles and r.title not in others:
            others.append(r.title)
    return KbLookup(
        status=KbLookupStatus.CANDIDATES,
        query=query,
        query_source=source,
        entries=entries,
        other_titles=tuple(others[:KB_QUESTION_MAX_OTHER_TITLES]),
    )
