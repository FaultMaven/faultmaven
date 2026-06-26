"""
Unified Knowledge Base Q&A Tool

Single tool that searches all KB scopes the user has access to:
- Global: system-wide runbooks (accessible to all)
- Personal: user's own runbooks (filtered by owner_id)
- Team: team runbooks (filtered by team_id)

The agent doesn't choose a scope — the tool builds a combined filter
from the user's identity and returns the most relevant results regardless
of where they came from.

Beyond the prose ``answer_from_kb`` path (for the LLM), this tool also exposes
``aget_cause_matches`` — the structured runbook-Cause matcher entry point. It
retrieves the most relevant runbooks for a question, resolves each to its v4
per-Cause causal-chain record (``knowledge_items.metadata["causes"]``), and runs
the rung-level ``IndicatorEvaluator`` against current case state. The graph
record lives in the ``knowledge_items`` row, *not* ChromaDB (matcher spec §6), so
the caller supplies a ``resolve_causes(item_id)`` callable backed by a
request-scoped knowledge repository. Inert until the engine wires it (increment 4).
"""

import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
)

from faultmaven.core.investigation.cause_schemas import CauseMatchResult, CauseRecord
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig

if TYPE_CHECKING:
    from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator

logger = logging.getLogger(__name__)

# Supplied by the engine (increment 4): runbook ``item_id`` → the runbook's v4
# per-Cause graph records (``knowledge_items.metadata["causes"]``), or None/[]
# when the runbook has none (upload-path / pre-v4). Kept as a callable rather
# than a repo dependency because this tool is a singleton while the knowledge
# repository is request-scoped (a fresh DB session per turn).
CausesResolver = Callable[[str], Awaitable[Optional[List[Dict[str, Any]]]]]


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

    # ------------------------------------------------------------------
    # Structured runbook-Cause matching (matcher spec §3, §6)
    # ------------------------------------------------------------------

    async def aget_cause_matches(
        self,
        question: str,
        user_id: str,
        *,
        resolve_causes: CausesResolver,
        evaluator: "IndicatorEvaluator",
        team_ids: Optional[List[str]] = None,
        k: int = 8,
        max_runbooks: int = 3,
    ) -> List[CauseMatchResult]:
        """Retrieve the top runbooks for ``question`` and match their Causes.

        For each of the top ``max_runbooks`` distinct runbooks retrieved, resolve
        its v4 per-Cause graph record via ``resolve_causes(item_id)`` and run the
        rung-level ``evaluator`` against current case state. Returns one
        ``CauseMatchResult`` per matched runbook, in retrieval-rank order (best
        first). Runbooks with no causes record (upload-path / pre-v4) are
        skipped — there is no chain to match.

        The matcher is a *prior, not a gate*: any failure (retrieval error, a
        single malformed Cause) degrades to fewer results, never an exception
        that could break the turn.

        Args:
            question: Search query (typically the case's current focus).
            user_id: For personal-scope filtering.
            resolve_causes: item_id → ``metadata["causes"]`` (or None/[]).
            evaluator: A configured ``IndicatorEvaluator`` (carries the case's
                step-output resolver + optional ``case_evidence_qa`` fallback).
            team_ids: For team-scope filtering.
            k: Number of chunks to retrieve (spans multiple runbooks).
            max_runbooks: Cap on distinct runbooks evaluated.

        Returns:
            List of ``CauseMatchResult`` (possibly empty), best runbook first.
        """
        filters = self._build_scope_filter(user_id, team_ids or [])
        chunks = await self._retrieve_chunks(question, k=k, filters=filters)
        item_ids = self._rank_runbook_ids(chunks, max_runbooks)

        results: List[CauseMatchResult] = []
        for item_id in item_ids:
            try:
                causes_raw = await resolve_causes(item_id)
            except Exception as exc:  # noqa: BLE001 — a prior must not break the turn
                logger.warning("resolve_causes failed for %s: %s", item_id, exc)
                continue
            if not causes_raw:
                continue  # upload-path / pre-v4 runbook — no chain to match
            cause_records = self._build_cause_records(item_id, causes_raw)
            if not cause_records:
                continue
            results.append(await evaluator.evaluate(item_id, cause_records))
        return results

    async def _retrieve_chunks(
        self, question: str, k: int, filters: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Raw vector-store retrieval (chunk dicts), mirroring the search-mode
        dispatch in ``DocumentQATool.answer_question``. Returns ``[]`` on any
        retrieval error so the matcher degrades rather than raising."""
        collection = self._kb_config.get_collection_name(None)
        try:
            if self._kb_config.search_mode == "hybrid" and hasattr(
                self._vector_store, "hybrid_search"
            ):
                return await self._vector_store.hybrid_search(
                    collection_name=collection, query=question, k=k, where=filters
                )
            return await self._vector_store.search(
                collection_name=collection, query=question, k=k, where=filters
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("KB retrieval failed for cause matching: %s", exc)
            return []

    @staticmethod
    def _rank_runbook_ids(chunks: List[Dict[str, Any]], max_runbooks: int) -> List[str]:
        """Distinct runbook ``item_id``s in retrieval-rank order (best first).

        Chunks come back ranked, so a runbook's first-seen chunk is its best;
        first-occurrence order is the runbook ranking. The item_id is the chunk's
        ``parent_document_id`` metadata; we fall back to stripping the
        ``_chunk_N`` suffix the ingest path appends to the chunk id."""
        ordered: List[str] = []
        seen = set()
        for chunk in chunks:
            item_id = AnswerFromKB._item_id_for_chunk(chunk)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            ordered.append(item_id)
            if len(ordered) >= max_runbooks:
                break
        return ordered

    @staticmethod
    def _item_id_for_chunk(chunk: Dict[str, Any]) -> Optional[str]:
        meta = chunk.get("metadata") or {}
        parent = meta.get("parent_document_id")
        if parent:
            return str(parent)
        chunk_id = chunk.get("id")
        if isinstance(chunk_id, str) and "_chunk_" in chunk_id:
            return chunk_id.rsplit("_chunk_", 1)[0]
        return str(chunk_id) if chunk_id else None

    @staticmethod
    def _build_cause_records(
        item_id: str, causes_raw: List[Dict[str, Any]]
    ) -> List[CauseRecord]:
        """Build ``CauseRecord``s from a runbook's stored causes list.

        Tolerant per entry: a malformed Cause is logged and skipped rather than
        failing the whole runbook (re-establishes the robustness the v3
        chunk-parser had against bad stored metadata)."""
        records: List[CauseRecord] = []
        for entry in causes_raw:
            if not isinstance(entry, dict):
                logger.warning(
                    "Skipping non-dict cause entry in runbook %s: %r", item_id, entry
                )
                continue
            try:
                records.append(CauseRecord(**entry))
            except Exception as exc:  # noqa: BLE001 — bad stored metadata, not fatal
                logger.warning(
                    "Skipping malformed cause %s in runbook %s: %s",
                    entry.get("cause_letter", "?"),
                    item_id,
                    exc,
                )
        return records

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
