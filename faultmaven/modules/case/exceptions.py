"""Case module exceptions.

This module defines exceptions specific to the case management module.
"""

from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException, ServiceError


class CaseException(FaultMavenException):
    """Base exception for case management errors."""

    pass


class CaseNotFoundError(CaseException):
    """Raised when a case is not found."""

    def __init__(self, message: str = "Case not found", case_id: Optional[str] = None):
        self.case_id = case_id
        super().__init__(message, details={"case_id": case_id})


class CaseStateError(CaseException):
    """Raised when a case state transition is invalid.

    This exception is raised when attempting an operation
    that is not valid for the case's current state.
    """

    def __init__(
        self,
        message: str,
        case_id: Optional[str] = None,
        current_state: Optional[str] = None,
        requested_state: Optional[str] = None,
    ):
        self.case_id = case_id
        self.current_state = current_state
        self.requested_state = requested_state
        super().__init__(
            message,
            details={
                "case_id": case_id,
                "current_state": current_state,
                "requested_state": requested_state,
            },
        )


class CaseAccessError(CaseException):
    """Raised when access to a case is denied.

    This exception is raised when a user/organization does not
    have permission to access or modify a case.
    """

    def __init__(
        self,
        message: str = "Access denied",
        case_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        required_permission: Optional[str] = None,
    ):
        self.case_id = case_id
        self.organization_id = organization_id
        self.required_permission = required_permission
        super().__init__(
            message,
            details={
                "case_id": case_id,
                "organization_id": organization_id,
                "required_permission": required_permission,
            },
        )


class CaseValidationError(CaseException):
    """Raised when case validation fails.

    This exception is raised when case data fails validation,
    such as missing required fields or invalid values.
    """

    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        value: Optional[Any] = None,
        constraint: Optional[str] = None,
    ):
        self.field = field
        self.value = value
        self.constraint = constraint
        super().__init__(
            message,
            details={
                "field": field,
                "value": str(value) if value is not None else None,
                "constraint": constraint,
            },
        )


class CaseOperationError(CaseException):
    """Raised when a case operation fails.

    Generic exception for case operations that fail due to
    business logic or infrastructure issues.
    """

    def __init__(
        self,
        message: str,
        case_id: Optional[str] = None,
        operation: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.case_id = case_id
        self.operation = operation
        self.error_code = error_code
        super().__init__(
            message,
            details={
                "case_id": case_id,
                "operation": operation,
                "error_code": error_code,
            },
        )
