"""Report module exceptions.

This module defines exceptions specific to the report generation module.
"""

from faultmaven.exceptions import FaultMavenException


class ReportException(FaultMavenException):
    """Base exception for report generation errors."""

    pass


class ReportNotFoundError(ReportException):
    """Raised when a report is not found."""

    def __init__(
        self,
        message: str = "Report not found",
        report_id: str | None = None,
        case_id: str | None = None,
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
        case_id: str | None = None,
        report_type: str | None = None,
        error_code: str | None = None,
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
        template_name: str | None = None,
        error_code: str | None = None,
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
        report_id: str | None = None,
        export_format: str | None = None,
        error_code: str | None = None,
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
        report_id: str | None = None,
        organization_id: str | None = None,
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
        field: str | None = None,
        constraint: str | None = None,
    ):
        self.field = field
        self.constraint = constraint
        super().__init__(message, details={"field": field, "constraint": constraint})
