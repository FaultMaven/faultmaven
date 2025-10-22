"""
User Knowledge Base Q&A Tool

Wrapper for DocumentQATool configured for User Knowledge Base.
Provides user-scoped access to personal runbooks and procedures.
"""

from faultmaven.tools.document_qa_tool import DocumentQATool
from faultmaven.tools.kb_configs.user_kb_config import UserKBConfig
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter


class AnswerFromUserKB(DocumentQATool):
    """
    Q&A tool for user's personal knowledge base.

    Configured for procedural knowledge with:
    - User scoping (requires user_id)
    - Document titles and categories in citations
    - Procedural clarity system prompt
    - 24-hour cache TTL (runbooks change infrequently)
    """

    name: str = "answer_from_user_kb"
    description: str = """Answer questions from your personal runbooks and procedures.

Use this tool to retrieve your documented best practices, procedures, and runbooks.

**When to use**:
- User references "my runbook" or "my procedure"
- Need user's documented approach for common issues
- Looking for user's standard operating procedures

**Examples**:
- "Show me my database timeout runbook"
- "What's my standard rollback procedure?"
- "How do I handle API rate limits according to my docs?"
- "My procedure for investigating memory leaks"
- "What does my MongoDB troubleshooting guide say?"

**Returns**: Your documented procedures and best practices with runbook citations.

**Note**: This is YOUR personal knowledge base, not system-wide documentation.
Use answer_from_global_kb for general guidance."""

    def __init__(
        self,
        vector_store: CaseVectorStore,
        llm_router: LLMRouter
    ):
        """
        Initialize user KB Q&A tool.

        Args:
            vector_store: ChromaDB vector store instance
            llm_router: LLM router for synthesis calls
        """
        super().__init__(
            vector_store=vector_store,
            llm_router=llm_router,
            kb_config=UserKBConfig()  # Inject user KB strategy
        )

    async def _arun(self, user_id: str, question: str, k: int = 5) -> str:
        """
        Query user's knowledge base.

        Args:
            user_id: User identifier (REQUIRED)
            question: Question about user's runbooks/procedures
            k: Number of chunks to retrieve (default: 5)

        Returns:
            User's documented procedures with runbook citations
        """
        # TODO: Add access control
        # Verify requesting user matches owner
        # if current_user.id != user_id:
        #     raise PermissionError("Cannot access other user's knowledge base")

        return await super()._arun(question, scope_id=user_id, k=k)
