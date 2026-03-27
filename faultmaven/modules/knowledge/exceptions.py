"""Knowledge module exceptions.

This module defines exceptions specific to the knowledge base module,
including RAG, embeddings, and semantic search operations.
"""

from faultmaven.exceptions import (
    KnowledgeBaseException,
)


class KnowledgeException(KnowledgeBaseException):
    """Base exception for knowledge module errors."""

    pass


class DocumentNotFoundError(KnowledgeException):
    """Raised when a document is not found in the knowledge base."""

    def __init__(
        self,
        message: str = "Document not found",
        document_id: str | None = None,
        knowledge_base_id: str | None = None,
    ):
        self.document_id = document_id
        self.knowledge_base_id = knowledge_base_id
        super().__init__(
            message,
            details={
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )


class DocumentIngestionError(KnowledgeException):
    """Raised when document ingestion fails.

    This exception is raised when a document cannot be processed
    and added to the knowledge base.
    """

    def __init__(
        self,
        message: str,
        document_name: str | None = None,
        error_code: str | None = None,
        processing_step: str | None = None,
    ):
        self.document_name = document_name
        self.error_code = error_code
        self.processing_step = processing_step
        super().__init__(
            message,
            details={
                "document_name": document_name,
                "error_code": error_code,
                "processing_step": processing_step,
            },
        )


class SearchError(KnowledgeException):
    """Raised when a knowledge base search fails.

    This exception is raised when semantic search or keyword
    search operations fail.
    """

    def __init__(
        self,
        message: str,
        query: str | None = None,
        search_type: str | None = None,
        error_code: str | None = None,
    ):
        self.query = query
        self.search_type = search_type
        self.error_code = error_code
        super().__init__(
            message,
            details={
                "query": query[:100] if query else None,  # Truncate long queries
                "search_type": search_type,
                "error_code": error_code,
            },
        )


class ChunkingError(KnowledgeException):
    """Raised when document chunking fails.

    This exception is raised when a document cannot be split
    into appropriate chunks for embedding.
    """

    def __init__(
        self,
        message: str,
        document_id: str | None = None,
        chunk_size: int | None = None,
        reason: str | None = None,
    ):
        self.document_id = document_id
        self.chunk_size = chunk_size
        self.reason = reason
        super().__init__(
            message,
            details={
                "document_id": document_id,
                "chunk_size": chunk_size,
                "reason": reason,
            },
        )


class KnowledgeBaseAccessError(KnowledgeException):
    """Raised when access to knowledge base is denied."""

    def __init__(
        self,
        message: str = "Access denied",
        knowledge_base_id: str | None = None,
        organization_id: str | None = None,
    ):
        self.knowledge_base_id = knowledge_base_id
        self.organization_id = organization_id
        super().__init__(
            message,
            details={
                "knowledge_base_id": knowledge_base_id,
                "organization_id": organization_id,
            },
        )


class IndexingError(KnowledgeException):
    """Raised when indexing operations fail.

    This exception is raised when documents cannot be indexed
    into the vector store.
    """

    def __init__(
        self,
        message: str,
        document_ids: list[str] | None = None,
        error_code: str | None = None,
    ):
        self.document_ids = document_ids
        self.error_code = error_code
        super().__init__(
            message, details={"document_ids": document_ids, "error_code": error_code}
        )
