"""Evidence module exceptions.

This module defines exceptions specific to the evidence management module.
"""

from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException, ServiceError


class EvidenceException(FaultMavenException):
    """Base exception for evidence management errors."""

    pass


class EvidenceNotFoundError(EvidenceException):
    """Raised when evidence is not found."""

    def __init__(
        self,
        message: str = "Evidence not found",
        evidence_id: Optional[str] = None,
        case_id: Optional[str] = None,
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
        filename: Optional[str] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
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
        field: Optional[str] = None,
        constraint: Optional[str] = None,
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
        storage_backend: Optional[str] = None,
        file_path: Optional[str] = None,
        operation: Optional[str] = None,
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
        evidence_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
    ):
        self.evidence_id = evidence_id
        self.enterprise_id = enterprise_id
        super().__init__(
            message,
            details={"evidence_id": evidence_id, "enterprise_id": enterprise_id},
        )


class EvidenceProcessingError(EvidenceException):
    """Raised when evidence processing fails.

    This exception is raised when evidence content cannot be
    processed (e.g., parsing logs, extracting text from PDFs).
    """

    def __init__(
        self,
        message: str,
        evidence_id: Optional[str] = None,
        processing_step: Optional[str] = None,
        error_code: Optional[str] = None,
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
