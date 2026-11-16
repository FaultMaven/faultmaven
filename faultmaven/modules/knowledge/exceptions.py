"""Knowledge module exceptions.

This module defines exceptions specific to the knowledge base module,
including RAG, embeddings, and semantic search operations.
"""

from typing import Any, Dict, List, Optional

from faultmaven.exceptions import (
    EmbeddingException,
    FaultMavenException,
    KnowledgeBaseException,
    ServiceUnavailableException,
    ValidationException,
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
#: publish it twice.
INGEST_OUTCOME_UNKNOWN_DETAIL = (
    "The runbook may have been published. Check the knowledge base before retrying."
)

#: ``POST /knowledge/documents``'s text for the same 500: a retry under the
#: upload's ``Idempotency-Key`` replays it if it landed and runs if not (#2433).
UPLOAD_OUTCOME_UNKNOWN_DETAIL = (
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
    ``Retry-After`` by ``api.exception_handlers``: ``INGEST_OUTCOME_UNKNOWN_DETAIL``,
    or ``UPLOAD_OUTCOME_UNKNOWN_DETAIL`` once the upload route sets ``from_upload``.
    Not a 503, which in this API says nothing was saved.
    """

    def __init__(self, item_id: str, *, from_upload: bool = False) -> None:
        self.item_id = item_id
        self.from_upload = from_upload
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


class RunbookQualityError(ValidationException):
    """Content was refused by the runbook quality gate.

    A ``ValidationException`` so the registered global handler answers **422** —
    the same status the upload route already answers by hand, reached from the
    service layer without the route having to translate anything.

    ``errors`` / ``warnings`` are kept as structured attributes for callers that
    want to render them; the flattened message carries them too, because
    ``validation_exception_handler`` renders ``str(exc)`` and a bare "does not
    meet quality standards" tells a reviewer nothing about what to fix.
    """

    def __init__(self, errors: List[str], warnings: Optional[List[str]] = None):
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        joined = "; ".join(self.errors) if self.errors else "unspecified"
        super().__init__(
            f"Content does not meet runbook quality standards: {joined}",
            details={"errors": self.errors, "warnings": self.warnings},
        )


# The runbook gate's answers when a check does not finish (#2661), raised by
# ``domain.services.runbook_gate`` and answered by ``api.exception_handlers``.

#: The ``Retry-After`` of the gate's 503s: its slot wait
#: (``runbook_gate.GATE_SLOT_WAIT_SECONDS`` is this value).
RUNBOOK_CHECK_RETRY_AFTER_SECONDS = 5


class GateRefused(RunbookQualityError):
    """A check stopped for its input alone: the gate's 422, with ``reason`` and
    the ruled sentence as its one error. Nothing is admitted."""

    reason: str
    message: str

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__(errors=[self.message])
        self.args = (self.message,)


class GateTimedOut(GateRefused):
    """Past the check's CPU-time deadline (or its ``RLIMIT_CPU`` backstop)."""

    reason = "validation_timeout"
    message = "This document took too long to check. Split it into smaller runbooks."


class GateOutputTooLarge(GateRefused):
    """An answer past the gate's output limit."""

    reason = "validation_output_too_large"
    message = (
        "This document has too many problems to list. Split it into smaller "
        "runbooks."
    )


class GateTooLarge(GateRefused):
    """Past the gate's memory limit."""

    reason = "validation_too_large"
    message = "This document is too large to check. Split it into smaller runbooks."


#: Each refusal by its ``reason``: a refusal stored on a row is raised again.
GATE_REFUSALS: Dict[str, type] = {
    refusal.reason: refusal
    for refusal in (GateTimedOut, GateOutputTooLarge, GateTooLarge)
}


class GateUnavailable(FaultMavenException):
    """The check could not run (load, a kill, a start that failed): a 503 with
    ``Retry-After``. Nothing is admitted."""

    error_code = "RUNBOOK_VALIDATION_UNAVAILABLE"
    message = "The runbook check could not run. Try again in a moment."
    retry_after_seconds = RUNBOOK_CHECK_RETRY_AFTER_SECONDS

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__(self.message)
        self.args = (self.message,)


class GateBusy(GateUnavailable):
    """No slot within the wait."""

    error_code = "RUNBOOK_VALIDATION_BUSY"
    message = "The server is busy checking other runbooks. Try again in a moment."


class GateFailed(FaultMavenException):
    """The check itself raised (a defect in it, not the input's size or load):
    the generic 500 ``SERVICE_ERROR``, not retryable. Nothing is admitted. The
    worker's failure is in the log, never in the answer."""

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__("The runbook check failed.")
        self.args = ("The runbook check failed.",)
