"""
Case Evidence Q&A Tool

Wrapper for DocumentQATool configured for Case Evidence Store.
Provides case-scoped forensic analysis of uploaded files.
"""

from faultmaven.tools.document_qa_tool import DocumentQATool
from faultmaven.tools.kb_configs.case_evidence_config import CaseEvidenceConfig
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter


class AnswerFromCaseEvidence(DocumentQATool):
    """
    Q&A tool for case-specific evidence (logs, configs, metrics, code).

    Configured for forensic analysis with:
    - Case scoping (requires case_id)
    - Line numbers and timestamps in citations
    - Forensic precision system prompt
    - 1-hour cache TTL (case session duration)
    """

    name: str = "answer_from_case_evidence"
    description: str = """Answer factual questions about files uploaded in this case.

Use this tool for forensic analysis of uploaded logs, configs, metrics, and code.

**When to use**:
- User asks specific questions about uploaded files
- You need detailed information not in preprocessed summary
- Forensic investigation requires line-by-line analysis

**Examples**:
- "What errors are in app.log?"
- "What's the database timeout configured in config.yaml?"
- "Show me all CRITICAL entries with timestamps"
- "What's on line 42 of the application log?"
- "When did the first error occur according to the logs?"

**Returns**: Factual answers with line numbers, timestamps, and file citations.

**Note**: You receive high-level summaries when files are uploaded (8KB preprocessed).
Use this tool for detailed forensic questions that require precise citations."""

    def __init__(
        self,
        vector_store: CaseVectorStore,
        llm_router: LLMRouter
    ):
        """
        Initialize case evidence Q&A tool.

        Args:
            vector_store: ChromaDB vector store instance
            llm_router: LLM router for synthesis calls
        """
        super().__init__(
            vector_store=vector_store,
            llm_router=llm_router,
            kb_config=CaseEvidenceConfig()  # Inject case evidence strategy
        )

    async def _arun(self, case_id: str, question: str, k: int = 5) -> str:
        """
        Query case evidence.

        Args:
            case_id: Case identifier (REQUIRED)
            question: Factual question about case evidence
            k: Number of chunks to retrieve (default: 5)

        Returns:
            Factual answer with forensic citations
        """
        # TODO: Add access control
        # Verify current_user has access to this case
        # if not await self._can_access_case(current_user, case_id):
        #     raise PermissionError(f"No access to case {case_id}")

        return await super()._arun(question, scope_id=case_id, k=k)
