"""Report module exceptions.

This module defines exceptions specific to the report generation module.
"""

from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException, ServiceError


class ReportException(FaultMavenException):
    """Base exception for report generation errors."""

    pass


class ReportNotFoundError(ReportException):
    """Raised when a report is not found."""

    def __init__(
        self,
        message: str = "Report not found",
        report_id: Optional[str] = None,
        case_id: Optional[str] = None,
    ):
        self.report_id = report_id
        self.case_id = case_id
        super().__init__(message, details={"report_id": report_id, "case_id": case_id})


class ReportGenerationError(ReportException):
    """Raised when report generation fails.

    This exception is raised when a report cannot be generated
    due to missing data, template errors, or processing failures.
    """

    def __init__(
        self,
        message: str,
        case_id: Optional[str] = None,
        report_type: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.case_id = case_id
        self.report_type = report_type
        self.error_code = error_code
        super().__init__(
            message,
            details={
                "case_id": case_id,
                "report_type": report_type,
                "error_code": error_code,
            },
        )


class ReportTemplateError(ReportException):
    """Raised when report template processing fails.

    This exception is raised when a report template cannot be
    loaded, parsed, or rendered.
    """

    def __init__(
        self,
        message: str,
        template_name: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.template_name = template_name
        self.error_code = error_code
        super().__init__(
            message, details={"template_name": template_name, "error_code": error_code}
        )


class ReportExportError(ReportException):
    """Raised when report export fails.

    This exception is raised when a report cannot be exported
    to the requested format (PDF, HTML, markdown, etc.).
    """

    def __init__(
        self,
        message: str,
        report_id: Optional[str] = None,
        export_format: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.report_id = report_id
        self.export_format = export_format
        self.error_code = error_code
        super().__init__(
            message,
            details={
                "report_id": report_id,
                "export_format": export_format,
                "error_code": error_code,
            },
        )


class ReportAccessError(ReportException):
    """Raised when access to a report is denied."""

    def __init__(
        self,
        message: str = "Access denied",
        report_id: Optional[str] = None,
        organization_id: Optional[str] = None,
    ):
        self.report_id = report_id
        self.organization_id = organization_id
        super().__init__(
            message,
            details={"report_id": report_id, "organization_id": organization_id},
        )


class ReportValidationError(ReportException):
    """Raised when report validation fails.

    This exception is raised when report data fails validation,
    such as invalid parameters or missing required data.
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
