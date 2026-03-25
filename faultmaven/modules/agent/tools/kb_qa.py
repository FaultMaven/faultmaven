"""
Unified Knowledge Base Q&A Tool

Single tool that searches all KB scopes the user has access to:
- Global: system-wide runbooks (accessible to all)
- Personal: user's own runbooks (filtered by owner_id)
- Team: team runbooks (filtered by team_id)

The agent doesn't choose a scope — the tool builds a combined filter
from the user's identity and returns the most relevant results regardless
of where they came from.
"""

from typing import List, Optional

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig


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
