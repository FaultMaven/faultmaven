"""
Unified Knowledge Base Q&A Tool

Single tool that searches all KB scopes the user has access to:
- Global: system-wide runbooks (accessible to all)
- Owned: user's own runbooks (filtered by owner_id)
- Team: runbooks shared to the user's teams (resolved from the share table into
  an id allowlist by the orchestrator; ADR-013 §D4)

The agent doesn't choose a scope — the tool builds a combined filter
from the user's identity and returns the most relevant results regardless
of where they came from.
"""

import logging
from typing import Dict, List, Optional

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    build_kb_scope_filter,
)

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
        shared_kb_ids: Optional[List[str]] = None,
        k: int = 5,
        context_metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Query knowledge base with automatic scope filtering.

        Args:
            question: Question about troubleshooting, procedures, or best practices
            user_id: Current user ID (for personal/owned scope filtering)
            shared_kb_ids: KB item ids shared to the user's teams (ADR-013 §D4),
                pre-resolved from the share table by the orchestrator — the team
                arm of the read allowlist
            k: Number of chunks to retrieve (default: 5)
            context_metadata: Optional case context (e.g. affected service) for
                metadata-aware reranking (soft boost only; see DocumentQATool)

        Returns:
            Relevant documentation with citations from all accessible scopes
        """
        # Single source of truth for the KB read allowlist (ADR-013 §D4 /
        # ADR-011 D3): global ∪ owned ∪ items shared to the user's teams.
        filters = build_kb_scope_filter(user_id, shared_kb_ids or [])
        return await super()._arun(
            question,
            scope_id=None,
            k=k,
            filters=filters,
            context_metadata=context_metadata,
        )
