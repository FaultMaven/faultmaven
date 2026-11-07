"""The typed exceptions the conversion pipeline raises to its routes."""

#: The ``LLM_UNAVAILABLE`` refusal's detail: Step 0's, and the upload route's
#: for the LLM layer's configuration error met past it (#1632).
LLM_NOT_CONFIGURED = (
    "No LLM provider is configured. Set CHAT_PROVIDER in your .env file "
    "or configure a provider in Dashboard > LLM Settings."
)


class ConversionRejectedError(Exception):
    """Raised when a document is rejected during preprocessing or analysis."""

    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        super().__init__(message)
        self.error_code = error_code
