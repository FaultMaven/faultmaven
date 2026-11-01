"""Knowledge module exceptions.

This module defines exceptions specific to the knowledge base module,
including RAG, embeddings, and semantic search operations.
"""

from typing import Any, Dict, List, Optional

from faultmaven.exceptions import (
    EmbeddingException,
    KnowledgeBaseException,
    ServiceUnavailableException,
    VectorStoreException,
)


class KnowledgeException(KnowledgeBaseException):
    """Base exception for knowledge module errors."""

    pass


class DocumentNotFoundError(KnowledgeException):
    """Raised when a document is not found in the knowledge base."""

    def __init__(
        self,
        message: str = "Document not found",
        document_id: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
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
        document_name: Optional[str] = None,
        error_code: Optional[str] = None,
        processing_step: Optional[str] = None,
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
        query: Optional[str] = None,
        search_type: Optional[str] = None,
        error_code: Optional[str] = None,
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
        document_id: Optional[str] = None,
        chunk_size: Optional[int] = None,
        reason: Optional[str] = None,
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
        knowledge_base_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
    ):
        self.knowledge_base_id = knowledge_base_id
        self.enterprise_id = enterprise_id
        super().__init__(
            message,
            details={
                "knowledge_base_id": knowledge_base_id,
                "enterprise_id": enterprise_id,
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
        document_ids: Optional[List[str]] = None,
        error_code: Optional[str] = None,
    ):
        self.document_ids = document_ids
        self.error_code = error_code
        super().__init__(
            message, details={"document_ids": document_ids, "error_code": error_code}
        )


class IngestNotRecorded(KnowledgeException):
    """An authored ingest's caller did not record it: its ``record`` callback
    returned False (#2396).

    Raised inside the completing transaction, so the completion rolled back
    with the caller's record, and ``ingest_runbook``'s cleanup removed the row:
    nothing was published.
    """

    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        super().__init__(f"Knowledge item {item_id} was not recorded by its caller")


#: The ``x-error-code`` of the 500 for an :class:`IngestOutcomeUnknown`.
KB_INGEST_OUTCOME_UNKNOWN = "KB_INGEST_OUTCOME_UNKNOWN"

#: What the caller is told: the runbook may be live, so a blind retry could
#: publish it twice. A retry under the same ``Idempotency-Key`` cannot (#2433):
#: it replays the upload if it landed and runs if it did not.
INGEST_OUTCOME_UNKNOWN_DETAIL = (
    "The runbook may have been published. Retry with the same Idempotency-Key "
    "to learn whether it was; without a key, check the knowledge base before "
    "retrying."
)


class KbReceiptExistsError(Exception):
    """A keyed upload or conversion tried to publish under a key that already
    has a receipt (#2433).

    The receipt's key refused the INSERT, so nothing of this request was
    published, and ANOTHER request under the same key already was. Reached
    only by a duplicate that ran without the route's in-flight claim (no
    Redis, a failed claim store, or an expired lease). The route answers it
    with the published request's receipt, as a retry would be answered.
    """

    def __init__(self, route_name: str, idempotency_key: str) -> None:
        self.route_name = route_name
        self.idempotency_key = idempotency_key
        super().__init__(
            f"A {route_name} under Idempotency-Key {idempotency_key!r} already "
            "published"
        )


class IngestOutcomeUnknown(KnowledgeException):
    """An authored ingest's completing transaction raised, and whether its
    COMMIT landed could not be learned: the cleanup's delete raised too
    (#2396).

    The runbook and its caller's record are one transaction, so the state is
    consistent either way: both, or neither, the row then incomplete and never
    served. Answered 500 with ``x-error-code: KB_INGEST_OUTCOME_UNKNOWN`` and no
    ``Retry-After`` by ``api.exception_handlers``. Not a 503, which in this API
    says nothing was saved.
    """

    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        super().__init__(INGEST_OUTCOME_UNKNOWN_DETAIL)


class ItemTitleScrubUnavailable(ServiceUnavailableException):
    """The check that keeps sensitive text out of a knowledge item's title could
    not run, so nothing was written (#1981).

    Distinct from a refusal for what the title SAYS: the title is fine as far as
    anyone knows, and a server-side dependency (the deployment's sanitizer, its
    pseudonym key, Presidio under a fail-closed posture) did not answer, or
    answered with something that cannot be trusted. Answered 503 with a generic
    detail by ``api.exception_handlers``: a client may retry, and nothing in the
    response says what the title held or why the check failed. The cause is on
    ``__cause__`` and in the log, by type, never the text.
    """
