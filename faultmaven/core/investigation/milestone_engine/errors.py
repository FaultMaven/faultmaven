"""MilestoneEngineError: the one exception the engine and its collaborators raise for a turn that cannot complete."""

from typing import Optional

from faultmaven.exceptions import LLMErrorCategory


class MilestoneEngineError(Exception):
    """Base exception for milestone engine errors.

    Carries an optional ``error_code`` (e.g. ``QUOTA_EXHAUSTED``) so the API
    layer can map the failure to a precise HTTP status and user-facing message
    instead of a generic 500.

    ``category`` relays the provider's typed ``LLMErrorCategory`` (#509) when
    this error was raised on behalf of one. It has to be RELAYED rather than
    inherited from ``__cause__`` because the retry-loop path deliberately does
    NOT chain the provider exception (chaining would put an HTTP 400 on the
    chain and re-route the documented ``TOKEN_LIMIT`` -> 503 to a 502). Without
    it the degrade metric loses its reason label, which is what the folded-in
    provider wording used to supply.
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        category: Optional[LLMErrorCategory] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.category = category
