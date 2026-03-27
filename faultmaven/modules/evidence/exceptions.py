"""Evidence module exceptions.

This module defines exceptions specific to the evidence management module.
"""

from typing import Any

from faultmaven.exceptions import FaultMavenException


class EvidenceException(FaultMavenException):
    """Base exception for evidence management errors."""

    pass


class EvidenceNotFoundError(EvidenceException):
    """Raised when evidence is not found."""

    def __init__(
        self,
        message: str = "Evidence not found",
        evidence_id: str | None = None,
        case_id: str | None = None,
    ):
        self.evidence_id = evidence_id
        self.case_id = case_id
        super().__init__(
            message, details={"evidence_id": evidence_id, "case_id": case_id}
        )


class EvidenceUploadError(EvidenceException):
    """Raised when evidence upload fails.

    This exception is raised when file upload fails due to
    validation, storage, or processing errors.
    """

    def __init__(
        self,
        message: str,
        filename: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.filename = filename
        self.error_code = error_code
        super().__init__(
            message,
            details={**(details or {}), "filename": filename, "error_code": error_code},
        )


class EvidenceValidationError(EvidenceException):
    """Raised when evidence validation fails.

    This exception is raised when evidence data fails validation,
    such as unsupported file types, size limits, or missing metadata.
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        constraint: str | None = None,
    ):
        self.field = field
        self.constraint = constraint
        super().__init__(message, details={"field": field, "constraint": constraint})


class EvidenceStorageError(EvidenceException):
    """Raised when evidence storage operations fail.

    This exception is raised when file storage (local, S3, Azure)
    operations fail.
    """

    def __init__(
        self,
        message: str,
        storage_backend: str | None = None,
        file_path: str | None = None,
        operation: str | None = None,
    ):
        self.storage_backend = storage_backend
        self.file_path = file_path
        self.operation = operation
        super().__init__(
            message,
            details={
                "storage_backend": storage_backend,
                "file_path": file_path,
                "operation": operation,
            },
        )


class EvidenceAccessError(EvidenceException):
    """Raised when access to evidence is denied."""

    def __init__(
        self,
        message: str = "Access denied",
        evidence_id: str | None = None,
        organization_id: str | None = None,
    ):
        self.evidence_id = evidence_id
        self.organization_id = organization_id
        super().__init__(
            message,
            details={"evidence_id": evidence_id, "organization_id": organization_id},
        )


class EvidenceProcessingError(EvidenceException):
    """Raised when evidence processing fails.

    This exception is raised when evidence content cannot be
    processed (e.g., parsing logs, extracting text from PDFs).
    """

    def __init__(
        self,
        message: str,
        evidence_id: str | None = None,
        processing_step: str | None = None,
        error_code: str | None = None,
    ):
        self.evidence_id = evidence_id
        self.processing_step = processing_step
        self.error_code = error_code
        super().__init__(
            message,
            details={
                "evidence_id": evidence_id,
                "processing_step": processing_step,
                "error_code": error_code,
            },
        )
