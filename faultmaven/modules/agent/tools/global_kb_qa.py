"""
Global Knowledge Base Q&A Tool

Wrapper for DocumentQATool configured for Global Knowledge Base.
Provides system-wide access to troubleshooting documentation and best practices.
"""

from faultmaven.tools.document_qa_tool import DocumentQATool
from faultmaven.tools.kb_configs.global_kb_config import GlobalKBConfig
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter


class AnswerFromGlobalKB(DocumentQATool):
    """
    Q&A tool for system-wide knowledge base.

    Configured for general guidance with:
    - No scoping (system-wide access)
    - Article IDs and titles in citations
    - Educational best practices system prompt
    - 7-day cache TTL (system KB changes rarely)
    """

    name: str = "answer_from_global_kb"
    description: str = """Answer questions from the system-wide knowledge base.

Use this tool for general troubleshooting guidance, best practices, and standards.

**When to use**:
- Need industry-standard approaches
- Looking for general troubleshooting methodologies
- Want common solutions to known problems
- Need best practices and gotchas

**Examples**:
- "Standard approach for diagnosing memory leaks?"
- "Common causes of API timeouts?"
- "How to analyze Java thread dumps?"
- "Best practices for database connection pooling"
- "What are the typical causes of high CPU usage?"

**Returns**: General best practices and system-wide guidance with KB article citations.

**Note**: This is system-wide documentation, not case-specific or user-specific.
Use answer_from_case_evidence for case files or answer_from_user_kb for personal runbooks."""

    def __init__(
        self,
        vector_store: CaseVectorStore,
        llm_router: LLMRouter
    ):
        """
        Initialize global KB Q&A tool.

        Args:
            vector_store: ChromaDB vector store instance
            llm_router: LLM router for synthesis calls
        """
        super().__init__(
            vector_store=vector_store,
            llm_router=llm_router,
            kb_config=GlobalKBConfig()  # Inject global KB strategy
        )

    async def _arun(self, question: str, k: int = 5) -> str:
        """
        Query global knowledge base.

        Args:
            question: Question about general troubleshooting/best practices
            k: Number of chunks to retrieve (default: 5)

        Returns:
            General best practices with KB article citations
        """
        # No access control needed - global read access for all users
        return await super()._arun(question, scope_id=None, k=k)
