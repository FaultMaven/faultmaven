"""The typed exceptions the conversion pipeline raises to its routes."""


class ConversionRejectedError(Exception):
    """Raised when a document is rejected during preprocessing or analysis."""

    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        super().__init__(message)
        self.error_code = error_code


class ScanAbortedError(RuntimeError):
    """Raised when the disk scan refuses to discard every active draft.

    Typed so that ``POST /knowledge/scan`` renders THIS refusal's hand-written
    sentence as its 409 and nothing else: a bare ``except RuntimeError`` there
    would put any library's ``RuntimeError`` text into the response (#836).
    """
