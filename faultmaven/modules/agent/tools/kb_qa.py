"""
Unified Knowledge Base Q&A Tool

Single tool that searches every KB scope the case may draw on: global
runbooks, plus the personal and team runbooks the case's audience may read
(``case_retrieval_scope``, #1919).

The agent doesn't choose a scope. The orchestrator resolves the case's scope
filter into the ``ToolContext``, and the tool searches under it unchanged.
"""

import logging
from typing import Any, Dict

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

    Searches every scope the case may draw on in a single query. Scope
    filtering is automatic — the agent just asks a question.
    """

    name: str = "answer_from_kb"
    description: str = """Search the knowledge base for runbooks, best practices, and documented procedures.

Returns the most relevant results from every source this case may draw on:
global documentation, plus the personal and team runbooks the case's readers can see.

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
        scope_filter: Dict[str, Any],
        k: int = 5,
    ) -> str:
        """
        Query knowledge base under the case's scope filter.

        Args:
            question: Question about troubleshooting, procedures, or best practices
            scope_filter: The case's KB read filter, built by
                ``case_retrieval_scope`` and carried on ``ToolContext``
            k: Number of chunks to retrieve (default: 5)

        Returns:
            Relevant documentation with citations from the case's scope
        """
        return await super()._arun(
            question,
            scope_id=None,
            k=k,
            filters=scope_filter,
        )
