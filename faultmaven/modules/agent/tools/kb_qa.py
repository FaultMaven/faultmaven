"""
Unified Knowledge Base Q&A Tool

Single tool that searches all KB scopes the user has access to:
- Global: system-wide runbooks (accessible to all)
- Personal: user's own runbooks (filtered by owner_id)
- Team: team runbooks (filtered by team_id)

The agent doesn't choose a scope — the tool builds a combined filter
from the user's identity and returns the most relevant results regardless
of where they came from.

v3: also exposes ``aget_top_causes(question, ...)`` which returns
parsed ``CauseChunk`` objects for the engine's KB-resolution path. The
prose ``_arun`` answer is for the LLM; the structured Cause data is for
the milestone-collapse handler.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.cause_schemas import CauseChunk
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig

logger = logging.getLogger(__name__)


class AnswerFromKB(DocumentQATool):
    """
    Unified Q&A tool for the entire knowledge base.

    Searches all scopes the user has access to in a single query.
    Scope filtering is automatic — the agent just asks a question.
    """

    name: str = "answer_from_kb"
    description: str = """Search the knowledge base for runbooks, best practices, and documented procedures.

Returns the most relevant results from all sources you have access to:
global documentation, your personal runbooks, and your team's shared procedures.

**When to use**:
- Need troubleshooting guidance or best practices
- Looking for documented procedures or runbooks
- Want known solutions to common problems

**Examples**:
- "Standard approach for diagnosing memory leaks?"
- "What's the rollback procedure for database migrations?"
- "How to analyze Java thread dumps?"
- "Common causes of API timeouts?"

**Returns**: Relevant runbooks and documentation with citations."""

    def __init__(self, vector_store: KnowledgeVectorStore, llm_router: LLMRouter):
        super().__init__(
            vector_store=vector_store,
            llm_router=llm_router,
            kb_config=UnifiedKBConfig(),
        )

    async def _arun(
        self,
        question: str,
        user_id: str,
        team_ids: Optional[List[str]] = None,
        k: int = 5,
    ) -> str:
        """
        Query knowledge base with automatic scope filtering.

        Args:
            question: Question about troubleshooting, procedures, or best practices
            user_id: Current user ID (for personal scope filtering)
            team_ids: User's team IDs (for team scope filtering)
            k: Number of chunks to retrieve (default: 5)

        Returns:
            Relevant documentation with citations from all accessible scopes
        """
        filters = self._build_scope_filter(user_id, team_ids or [])
        return await super()._arun(question, scope_id=None, k=k, filters=filters)

    async def aget_top_causes(
        self,
        question: str,
        user_id: str,
        team_ids: Optional[List[str]] = None,
        k: int = 5,
    ) -> List[CauseChunk]:
        """Return top-K v3 ``CauseChunk`` results for a question.

        Filters retrieved chunks to those that are ``### Cause N``
        subsections (i.e. carry ``cause_letter`` metadata) and parses
        them into structured ``CauseChunk`` objects. Non-Cause chunks
        (Symptom Recognition, Prevention, etc.) are skipped — they are
        useful for prose synthesis (which ``_arun`` provides) but the
        engine's KB-resolution path needs structured Cause data.

        Args:
            question: Search query
            user_id: For personal-scope filtering
            team_ids: For team-scope filtering
            k: Number of CauseChunks to return (top-K after filtering)

        Returns:
            List of ``CauseChunk``, possibly empty. Ordered by retrieval score.
        """
        filters = self._build_scope_filter(user_id, team_ids or [])
        # Over-fetch because we'll filter non-Cause chunks out.
        raw_chunks = await self._retrieve_raw_chunks(question, k=k * 3, filters=filters)
        causes: List[CauseChunk] = []
        for chunk in raw_chunks:
            cause = self._parse_cause_chunk(chunk)
            if cause is not None:
                causes.append(cause)
            if len(causes) >= k:
                break
        return causes

    async def _retrieve_raw_chunks(
        self, question: str, k: int, filters: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Vector-store retrieval shared by structured-cause + prose paths.

        Returns raw chunk dicts (content + metadata + score) instead of
        the prose-synthesized string. Mirrors the search-mode dispatch
        in ``DocumentQATool.answer_question``.
        """
        collection = self._kb_config.get_collection_name(None)
        search_mode = self._kb_config.search_mode
        if search_mode == "hybrid" and hasattr(self._vector_store, "hybrid_search"):
            return await self._vector_store.hybrid_search(
                collection_name=collection, query=question, k=k, where=filters
            )
        return await self._vector_store.search(
            collection_name=collection, query=question, k=k, where=filters
        )

    @staticmethod
    def _parse_cause_chunk(chunk: Dict[str, Any]) -> Optional[CauseChunk]:
        """Build a CauseChunk from a ChromaDB chunk dict. Returns None
        if the chunk is not a v3 Cause subsection."""
        meta = chunk.get("metadata") or {}
        if "cause_letter" not in meta:
            return None

        match_predicates: List[Dict[str, Any]] = []
        raw_predicates = meta.get("match_predicates")
        if isinstance(raw_predicates, str) and raw_predicates:
            try:
                parsed = json.loads(raw_predicates)
                if isinstance(parsed, list):
                    match_predicates = [p for p in parsed if isinstance(p, dict)]
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed match_predicates JSON in chunk %s: %r",
                    meta.get("section"),
                    raw_predicates[:80],
                )

        indicators_raw = meta.get("cause_indicator", "")
        indicators: List[str] = []
        for line in indicators_raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("- ", "* ", "+ ")):
                stripped = stripped[2:].strip()
            indicators.append(stripped)

        return CauseChunk(
            runbook_id=str(meta.get("id") or meta.get("runbook_id") or "unknown"),
            cause_letter=str(meta["cause_letter"]),
            cause_name=str(meta.get("cause_name", "")),
            statement=str(meta.get("cause_statement", "")),
            mechanism=str(meta.get("cause_mechanism", "")),
            indicators=indicators,
            match_predicates=match_predicates,
            mitigation=str(meta.get("cause_mitigation", "")),
            resolution=str(meta.get("cause_resolution", "")),
            verification=str(meta.get("cause_verification", "")),
            is_fallback=bool(meta.get("is_fallback_cause", False)),
        )

    @staticmethod
    def _build_scope_filter(user_id: str, team_ids: List[str]) -> dict:
        """Build combined scope filter for all accessible KB content."""
        conditions = [{"scope": "global"}]

        if user_id:
            conditions.append({"$and": [{"scope": "personal"}, {"owner_id": user_id}]})

        if team_ids:
            conditions.append(
                {"$and": [{"scope": "team"}, {"team_id": {"$in": team_ids}}]}
            )

        return {"$or": conditions}
