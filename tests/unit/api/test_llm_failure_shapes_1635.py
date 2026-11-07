"""An LLM failure that is not an ``LLMException`` answers the LLM's status (#1635).

Two shapes carry one. The LLM router's open breaker is a ``CircuitBreakerError``
and its deadline an ``ExternalCallTimeout``, both raised by every external
client, so only their ``service`` says they are the LLM's. The LLM layer's
configuration error is an ``LLMProviderError``. The global handler read an LLM
failure only as an ``LLMException`` on the chain, so each answered a static
500; the ``/turns`` mapping answered the breaker one too, with ``Retry-After:
10``. A breaker or deadline of another service, or of none, answers as before,
and so do #1633's rows: data faults the ``/turns`` mapping would read as the
provider's.
"""

from __future__ import annotations

import pytest

from faultmaven.api.exception_handlers import (
    global_service_exception_http_exception,
    llm_service_error_http_exception,
)
from faultmaven.exceptions import (
    LLM_CONFIG_ERROR,
    PROVIDER_AUTH_FAILED,
    QUOTA_EXHAUSTED,
    ExternalCallTimeout,
    ServiceException,
)
from faultmaven.infrastructure.base_client import (
    LLM_BREAKER_SERVICE,
    CircuitBreakerError,
)
from faultmaven.models.exceptions import LLMProviderError
from tests.unit.api.test_service_exception_global_handler import (
    _BILLING_TEXT,
    _corrupt_blob,
    _corrupt_record,
    _wrap,
)

pytestmark = pytest.mark.unit


def _breaker(service=LLM_BREAKER_SERVICE, error_code=None) -> CircuitBreakerError:
    return CircuitBreakerError(
        f"Circuit breaker is open for {service}", error_code=error_code, service=service
    )


def _deadline(service) -> ExternalCallTimeout:
    return ExternalCallTimeout(
        f"External call to {service}.route timed out after 30.0s",
        service=service,
        timeout=30.0,
    )


#: (status, x-error-code, Retry-After)
UNAVAILABLE = (503, "LLM_PROVIDER_UNAVAILABLE", "30")
PROVIDER_ERROR = (502, "LLM_PROVIDER_ERROR", None)
BILLING = (402, QUOTA_EXHAUSTED, None)
INVALID_RESPONSE = (503, "LLM_INVALID_RESPONSE", "30")
GLOBAL_500 = (500, "SERVICE_ERROR", None)
TURNS_500 = (500, "SERVICE_ERROR", "10")

#: name: (failure, the global mapping's answer, the /turns mapping's answer)
_ROWS = {
    # Moved by #1635: each answered a 500 at the global handler.
    "llm-breaker": (_breaker, UNAVAILABLE, UNAVAILABLE),
    "llm-breaker-auth": (
        lambda: _breaker(error_code=PROVIDER_AUTH_FAILED),
        PROVIDER_ERROR,
        PROVIDER_ERROR,
    ),
    "llm-config-error": (
        lambda: LLMProviderError("no provider", error_code=LLM_CONFIG_ERROR),
        PROVIDER_ERROR,
        PROVIDER_ERROR,
    ),
    "llm-provider-error-default-code": (
        lambda: LLMProviderError("provider failed"),
        PROVIDER_ERROR,
        PROVIDER_ERROR,
    ),
    "llm-deadline": (lambda: _deadline(LLM_BREAKER_SERVICE), UNAVAILABLE, UNAVAILABLE),
    # As on main.
    "llm-breaker-billing": (
        lambda: _breaker(error_code=QUOTA_EXHAUSTED),
        BILLING,
        BILLING,
    ),
    "unnamed-breaker": (lambda: _breaker(service=None), GLOBAL_500, TURNS_500),
    "chromadb-breaker": (lambda: _breaker(service="ChromaDB"), GLOBAL_500, TURNS_500),
    "unnamed-deadline": (lambda: _deadline(None), GLOBAL_500, UNAVAILABLE),
    "s3-deadline": (lambda: _deadline("s3"), GLOBAL_500, UNAVAILABLE),
    "corrupt-record": (_corrupt_record, GLOBAL_500, INVALID_RESPONSE),
    "corrupt-blob": (_corrupt_blob, GLOBAL_500, INVALID_RESPONSE),
    "billing-wording-no-cause": (
        lambda: ServiceException(f"Export failed: {_BILLING_TEXT}"),
        GLOBAL_500,
        BILLING,
    ),
}


def _answer(http) -> tuple:
    return (
        http.status_code,
        http.headers["x-error-code"],
        http.headers.get("Retry-After"),
    )


@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "wrapped"])
@pytest.mark.parametrize("name", sorted(_ROWS))
def test_each_failure_answers_its_row_in_both_mappings(name, wrapped):
    """Raw is how ``llm_failure_handler`` receives one; wrapped in a
    ``ServiceException`` is how a service hands one on."""
    factory, global_answer, turns_answer = _ROWS[name]
    exc = _wrap(factory()) if wrapped else factory()

    assert _answer(global_service_exception_http_exception(exc)) == global_answer
    assert _answer(llm_service_error_http_exception(exc)) == turns_answer
