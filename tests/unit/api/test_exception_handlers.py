"""Unit tests for API exception handlers (TASK-014).

Tests:
- NotFoundError → 404 JSON response
- AuthorizationError → 403 JSON response
- ValidationException → 400 JSON response
- ConflictError → 409 JSON response
- ServiceError → 500 JSON response
- OAuthProtocolError → the RFC 6749 §5.2 object, at the status it carries
- Error response format (error, detail, status_code fields)
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from faultmaven.api.exception_handlers import (
    MAX_DETAIL_CHARS,
    authorization_exception_handler,
    conflict_exception_handler,
    get_exception_handlers,
    http_exception_handler,
    not_found_exception_handler,
    oauth_protocol_error_handler,
    service_error_handler,
    validation_exception_handler,
)
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.models.exceptions import OAuthProtocolError


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url.path = "/api/v1/test"
    return request


# ============================================================
# NotFoundError Handler Tests
# ============================================================


class TestNotFoundExceptionHandler:
    """Tests for not_found_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_404_status(self, mock_request):
        """Test handler returns 404 status code."""
        exc = NotFoundError("Case", "case_123")
        response = await not_found_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = NotFoundError("Case", "case_123")
        response = await not_found_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Not Found"' in body
        assert '"status_code":404' in body
        assert "case_123" in body

    @pytest.mark.asyncio
    async def test_includes_exception_message(self, mock_request):
        """Test response includes exception message in detail."""
        exc = NotFoundError("Session", "session_456")
        response = await not_found_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Session" in body
        assert "session_456" in body

    @pytest.mark.asyncio
    async def test_different_resource_types(self, mock_request):
        """Test handler works with different resource types."""
        for resource_type in ["Case", "Session", "Evidence", "Execution"]:
            exc = NotFoundError(resource_type, "test_id")
            response = await not_found_exception_handler(mock_request, exc)
            body = response.body.decode()
            assert resource_type in body

    @pytest.mark.asyncio
    async def test_surfaces_structured_metadata(self, mock_request):
        """Handler must surface resource_type + resource_id in the
        response body so clients branch on them instead of parsing
        the human-readable detail string.

        Spec: docs/architecture/specifications/exception-contract.md
        """
        import json

        exc = NotFoundError(
            resource_type="conversion_job",
            resource_id="conv_abc123",
            message="Conversion job not found",
        )
        response = await not_found_exception_handler(mock_request, exc)
        body = json.loads(response.body)
        assert body["resource_type"] == "conversion_job"
        assert body["resource_id"] == "conv_abc123"
        assert body["error"] == "Not Found"
        assert body["status_code"] == 404

    @pytest.mark.asyncio
    async def test_omits_metadata_when_absent(self, mock_request):
        """When NotFoundError is raised without resource_type /
        resource_id, the fields are absent (not null) — keeps the
        response shape minimal for callers using only the message
        constructor."""
        import json

        exc = NotFoundError(message="Document not found in knowledge base")
        response = await not_found_exception_handler(mock_request, exc)
        body = json.loads(response.body)
        assert "resource_type" not in body
        assert "resource_id" not in body
        # Core fields still present.
        assert body["error"] == "Not Found"
        assert body["status_code"] == 404
        assert "Document not found" in body["detail"]


# ============================================================
# AuthorizationError Handler Tests
# ============================================================


class TestAuthorizationExceptionHandler:
    """Tests for authorization_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_403_status(self, mock_request):
        """Test handler returns 403 status code."""
        exc = AuthorizationError("Access denied")
        response = await authorization_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = AuthorizationError("Not authorized to access this resource")
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Forbidden"' in body
        assert '"status_code":403' in body

    @pytest.mark.asyncio
    async def test_includes_error_message(self, mock_request):
        """Test response includes error message."""
        exc = AuthorizationError("Organization mismatch")
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Organization mismatch" in body

    @pytest.mark.asyncio
    async def test_default_message(self, mock_request):
        """Test default authorization message."""
        exc = AuthorizationError()  # Uses default message
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Not authorized" in body


# ============================================================
# ValidationException Handler Tests
# ============================================================


class TestValidationExceptionHandler:
    """Tests for validation_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_400_status(self, mock_request):
        """Test handler returns 422 status code (Unprocessable Entity)."""
        exc = ValidationException("Invalid input")
        response = await validation_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ValidationException("Title is required")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Validation Error"' in body
        assert '"status_code":422' in body

    @pytest.mark.asyncio
    async def test_includes_validation_details(self, mock_request):
        """Test response includes validation error details."""
        exc = ValidationException("Field 'title' must be at least 1 character")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "title" in body
        assert "1 character" in body

    @pytest.mark.asyncio
    async def test_multiple_field_errors(self, mock_request):
        """Test handling of complex validation messages."""
        exc = ValidationException("title: required, description: too long")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "title" in body or "description" in body


# ============================================================
# ConflictError Handler Tests
# ============================================================


class TestConflictExceptionHandler:
    """Tests for conflict_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_409_status(self, mock_request):
        """Test handler returns 409 status code."""
        exc = ConflictError("Resource already exists")
        response = await conflict_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ConflictError("Case already closed")
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Conflict"' in body
        assert '"status_code":409' in body

    @pytest.mark.asyncio
    async def test_includes_conflict_details(self, mock_request):
        """Test response includes conflict details."""
        exc = ConflictError(
            "Active session already exists",
            resource_type="Session",
            resource_id="session_123",
            conflict_reason="active_session_exists",
        )
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Active session" in body

    @pytest.mark.asyncio
    async def test_case_already_closed(self, mock_request):
        """Test conflict for already closed case."""
        exc = ConflictError(
            "Case is already closed",
            resource_type="Case",
            resource_id="case_123",
            conflict_reason="already_closed",
        )
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "already closed" in body

    @pytest.mark.asyncio
    async def test_surfaces_structured_metadata(self, mock_request):
        """Handler must surface resource_type, resource_id, AND
        conflict_reason in the response body. Clients distinguish
        conflict shapes (duplicate_username vs duplicate_email vs
        already_verified, etc.) by branching on conflict_reason rather
        than regex-matching the message.

        Spec: docs/architecture/specifications/exception-contract.md
        """
        import json

        exc = ConflictError(
            "User with username 'alice' already exists",
            resource_type="user",
            resource_id="alice",
            conflict_reason="duplicate_username",
        )
        response = await conflict_exception_handler(mock_request, exc)
        body = json.loads(response.body)
        assert body["resource_type"] == "user"
        assert body["resource_id"] == "alice"
        assert body["conflict_reason"] == "duplicate_username"
        assert body["error"] == "Conflict"
        assert body["status_code"] == 409

    @pytest.mark.asyncio
    async def test_omits_metadata_when_absent(self, mock_request):
        """When ConflictError is raised with only a message, the
        structured fields are absent (not null) in the response.
        Keeps the shape minimal for legacy raises that haven't been
        migrated to the structured constructor yet."""
        import json

        exc = ConflictError("Resource already exists")
        response = await conflict_exception_handler(mock_request, exc)
        body = json.loads(response.body)
        assert "resource_type" not in body
        assert "resource_id" not in body
        assert "conflict_reason" not in body
        assert body["error"] == "Conflict"
        assert body["status_code"] == 409


# ============================================================
# ServiceError Handler Tests
# ============================================================


class TestServiceErrorHandler:
    """Tests for service_error_handler."""

    @pytest.mark.asyncio
    async def test_returns_500_status(self, mock_request):
        """Test handler returns 500 status code."""
        exc = ServiceError("Database connection failed")
        response = await service_error_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ServiceError("Internal error occurred")
        response = await service_error_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Internal Server Error"' in body
        assert '"status_code":500' in body

    @pytest.mark.asyncio
    async def test_hides_internal_details(self, mock_request):
        """Test that internal error details are hidden from response."""
        exc = ServiceError("Database password: secret123, connection string: ...")
        response = await service_error_handler(mock_request, exc)
        body = response.body.decode()
        # Should NOT expose internal details
        assert "secret123" not in body
        assert "password" not in body
        # Should have generic message
        assert "An unexpected error occurred" in body


# ============================================================
# get_exception_handlers Tests
# ============================================================


class TestGetExceptionHandlers:
    """Tests for get_exception_handlers function."""

    def test_returns_dict(self):
        """Test function returns a dictionary."""
        handlers = get_exception_handlers()
        assert isinstance(handlers, dict)

    def test_includes_all_handlers(self):
        """Test dictionary includes all exception types."""
        handlers = get_exception_handlers()
        assert NotFoundError in handlers
        assert AuthorizationError in handlers
        assert ValidationException in handlers
        assert ConflictError in handlers
        assert ServiceError in handlers
        assert OAuthProtocolError in handlers

    def test_handler_is_callable(self):
        """Test each handler is callable."""
        handlers = get_exception_handlers()
        for handler in handlers.values():
            assert callable(handler)

    def test_handler_count(self):
        """Test expected number of handlers.

        A bare count says nothing about *which* handler was added or lost, so
        the set is asserted with it: a registration silently replaced by
        another would keep the count and change the contract.
        """
        handlers = get_exception_handlers()
        assert set(handlers) == {
            NotFoundError,
            AuthorizationError,
            ValidationException,
            ConflictError,
            ServiceError,
            OAuthProtocolError,
        }


# ============================================================
# Response Format Consistency Tests
# ============================================================


class TestOAuthProtocolErrorHandler:
    """The one handler here that does not answer the house error shape."""

    @pytest.mark.asyncio
    async def test_renders_the_rfc6749_object(self, mock_request):
        """RFC 6749 §5.2: `error` + `error_description`, and nothing else.

        A standards-written OAuth client dispatches on `error`. Adding the
        house `detail`/`status_code` fields beside it would let a client keep
        reading `detail` and never learn the code (#1150).
        """
        response = await oauth_protocol_error_handler(
            mock_request,
            OAuthProtocolError("invalid_grant", "Refresh token expired or revoked"),
        )

        assert response.status_code == 400
        body = json.loads(response.body)
        assert body == {
            "error": "invalid_grant",
            "error_description": "Refresh token expired or revoked",
        }

    @pytest.mark.asyncio
    async def test_carries_the_status_the_error_names(self, mock_request):
        """The status travels on the exception: 415 and 503 both reach here."""
        response = await oauth_protocol_error_handler(
            mock_request,
            OAuthProtocolError("invalid_request", "Unsupported Content-Type", 415),
        )

        assert response.status_code == 415

    @pytest.mark.asyncio
    async def test_is_not_cacheable(self, mock_request):
        """RFC 6749 §5.1 — a refusal names a credential's state."""
        response = await oauth_protocol_error_handler(
            mock_request, OAuthProtocolError("invalid_grant", "nope")
        )

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"


class TestDictDetailIsCoerced:
    """A dict `detail` cannot promise a renderable message.

    `http_exception_handler` pulls a message out of a dict `detail` and puts it
    straight into a JSONResponse. Both branches take whatever the raising code
    put there — `nested["message"]`, or `detail.get("message")` — so a value the
    encoder cannot render raised *inside the handler* and turned a deliberate
    4xx into a 500 with none of the message the client was meant to see. That is
    the defect #1048 fixed for the validation handler, which lived on here.
    """

    @pytest.mark.asyncio
    async def test_a_nonserializable_nested_message_still_answers_its_status(
        self, mock_request
    ):
        class Unrenderable:
            def __repr__(self):
                return "<Unrenderable>"

        response = await http_exception_handler(
            mock_request,
            HTTPException(
                status_code=400,
                detail={"error": {"code": "x", "message": Unrenderable()}},
            ),
        )

        assert response.status_code == 400
        assert json.loads(response.body)["detail"] == "<Unrenderable>"

    @pytest.mark.asyncio
    async def test_a_nonserializable_flat_message_still_answers_its_status(
        self, mock_request
    ):
        response = await http_exception_handler(
            mock_request,
            HTTPException(status_code=409, detail={"message": object()}),
        )

        assert response.status_code == 409
        assert isinstance(json.loads(response.body)["detail"], str)

    @pytest.mark.asyncio
    async def test_a_string_detail_with_a_lone_surrogate_still_answers(
        self, mock_request
    ):
        """The other branch, and the likelier one in practice.

        A `str` detail is not a safe type here. A lone surrogate reaches one
        from a *valid* JSON body — `json.loads('"\ud800"')` succeeds — and
        user-supplied strings are interpolated straight into details
        (`auth.py`'s `username`, `admin_config.py`'s `provider_name`). The
        encoder then raised inside this handler, and the deliberate 4xx became
        a 500 carrying none of the message.
        """
        response = await http_exception_handler(
            mock_request,
            HTTPException(status_code=409, detail="user '\ud800' already exists"),
        )

        assert response.status_code == 409
        # Rendered at all is the property; the surrogate itself is
        # unrepresentable, so what it becomes is `to_json_safe`'s business.
        assert "already exists" in json.loads(response.body)["detail"]

    @pytest.mark.asyncio
    async def test_a_dict_whose_repr_raises_still_answers_its_status(
        self, mock_request
    ):
        """The fallback path, where the stringification itself was the hazard.

        With no `message` or `detail` key the handler fell back to
        `str(detail)` — and `str()` on a container calls `repr()` on its
        members, so a value with a raising `__repr__` blew up inside the
        handler. Coercing had to move ahead of the stringification, not after.
        """

        class Boom:
            def __repr__(self):
                raise RuntimeError("repr exploded")

        response = await http_exception_handler(
            mock_request,
            HTTPException(status_code=400, detail={"code": "x", "payload": Boom()}),
        )

        assert response.status_code == 400
        assert isinstance(json.loads(response.body)["detail"], str)

    @pytest.mark.parametrize("detail", [["a", "b"], 42, ("x",)])
    @pytest.mark.asyncio
    async def test_a_non_string_detail_stays_text(self, mock_request, detail):
        """`detail` is rendered verbatim by clients, so its type is contract.

        Coercing with `to_json_safe` alone preserved native JSON types, which
        would have published a list detail as an array and an int as a number
        where both were previously stringified.
        """
        response = await http_exception_handler(
            mock_request, HTTPException(status_code=400, detail=detail)
        )

        assert isinstance(json.loads(response.body)["detail"], str)

    @pytest.mark.asyncio
    async def test_a_long_detail_survives_the_echo_bound(self, mock_request):
        """Error text is not governed by the echoed-input constant.

        `to_json_safe`'s default (512) exists to bound an echoed *request body*
        in a 422. Borrowing it for `detail` truncated admin and LLM error
        messages at 512 characters where callers previously got the whole
        thing — `admin.py` and `admin_config.py` interpolate `str(e)` into
        details at twenty sites, and a provider error body clears 512 easily.
        This pins that the two numbers are separate, so retuning the echo bound
        for echo reasons cannot move error-message length with it.
        """
        message = "provider said: " + "x" * 600

        response = await http_exception_handler(
            mock_request, HTTPException(status_code=502, detail=message)
        )

        assert json.loads(response.body)["detail"] == message

    @pytest.mark.asyncio
    async def test_a_detail_is_still_bounded(self, mock_request):
        """Bounded, though — an unbounded detail is an unbounded response."""
        response = await http_exception_handler(
            mock_request, HTTPException(status_code=502, detail="x" * 50_000)
        )

        detail = json.loads(response.body)["detail"]
        assert len(detail) < MAX_DETAIL_CHARS + 100, len(detail)
        # Asserted absolutely as well as relative to the constant: measuring
        # only against MAX_DETAIL_CHARS means raising it to a million passes,
        # so the relative check alone cannot tell "bounded" from "unbounded".
        assert MAX_DETAIL_CHARS <= 8192, (
            "MAX_DETAIL_CHARS is a ceiling on a human-facing message; past a "
            "few thousand characters it is a payload, and an unbounded detail "
            "makes an unbounded response"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_string_detail_is_unchanged(self, mock_request):
        response = await http_exception_handler(
            mock_request, HTTPException(status_code=404, detail="Case not found")
        )

        assert json.loads(response.body) == {"detail": "Case not found"}

    @pytest.mark.asyncio
    async def test_an_ordinary_message_is_unchanged(self, mock_request):
        """Coercion must not reshape the common case."""
        response = await http_exception_handler(
            mock_request,
            HTTPException(status_code=404, detail={"error": {"message": "not found"}}),
        )

        assert json.loads(response.body) == {"detail": "not found"}


class TestResponseFormatConsistency:
    """Tests for consistent response format across handlers.

    ``OAuthProtocolError`` is deliberately absent: it answers the RFC 6749
    §5.2 object rather than this module's ``{"error", "detail",
    "status_code"}`` shape, and is covered by the class above.
    """

    @pytest.mark.asyncio
    async def test_all_responses_have_error_field(self, mock_request):
        """Test all error responses have 'error' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"error"' in body

    @pytest.mark.asyncio
    async def test_all_responses_have_status_code_field(self, mock_request):
        """Test all error responses have 'status_code' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"status_code"' in body

    @pytest.mark.asyncio
    async def test_all_responses_have_detail_field(self, mock_request):
        """Test all error responses have 'detail' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"detail"' in body


class TestQuotaExhaustedHttpException:
    """The shared billing→402 mapper used by every LLM-calling endpoint."""

    def test_maps_to_402_with_error_code_and_no_retry_after(self):
        from faultmaven.api.exception_handlers import quota_exhausted_http_exception
        from faultmaven.exceptions import QUOTA_EXHAUSTED

        exc = quota_exhausted_http_exception("corr-123")
        assert exc.status_code == 402
        assert exc.headers["x-error-code"] == QUOTA_EXHAUSTED
        assert exc.headers["x-correlation-id"] == "corr-123"
        # Retrying cannot add credits — no Retry-After.
        assert "Retry-After" not in exc.headers
        assert "credit" in exc.detail.lower()

    def test_correlation_id_optional(self):
        from faultmaven.api.exception_handlers import quota_exhausted_http_exception

        exc = quota_exhausted_http_exception()
        assert exc.status_code == 402
        assert "x-correlation-id" not in exc.headers

    def test_is_quota_exhausted_service_error_predicate(self):
        from faultmaven.api.exception_handlers import (
            is_quota_exhausted_service_error,
        )
        from faultmaven.exceptions import QUOTA_EXHAUSTED, ServiceException

        assert (
            is_quota_exhausted_service_error(
                ServiceException("x", details={"error_code": QUOTA_EXHAUSTED})
            )
            is True
        )
        # Non-billing ServiceException and exceptions without details → False.
        assert is_quota_exhausted_service_error(ServiceException("x")) is False
        assert is_quota_exhausted_service_error(ValueError("y")) is False


def _wrap(cause: BaseException):
    """Reproduce the turn service's ``raise ServiceException(...) from e`` wrap."""
    from faultmaven.exceptions import ServiceException

    try:
        raise ServiceException(f"Turn processing failed: {cause}") from cause
    except ServiceException as e:
        return e


class TestLLMServiceErrorHttpException:
    """Typed LLM-failure → HTTP mapping (replaces the old message-string match).

    Every case wraps the underlying provider failure exactly as the turn service
    does, so these exercise the real ``__cause__`` chain the classifier walks.
    """

    def _classify(self, cause, correlation_id="corr-1"):
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )

        return llm_service_error_http_exception(_wrap(cause), correlation_id)

    def _llm(self, message="provider boom", **kwargs):
        from faultmaven.exceptions import LLMException

        return LLMException(message, **kwargs)

    def test_timeout_504_maps_to_gateway_timeout(self):
        # The provider now stamps status_code=504 on a timeout.
        exc = self._classify(self._llm(status_code=504))
        assert exc.status_code == 504
        assert exc.headers["x-error-code"] == "LLM_TIMEOUT"
        assert exc.headers["Retry-After"] == "30"

    def test_regression_timed_out_message_no_longer_500s(self):
        """The exact string that defeated the old ``"timeout" in msg`` match.

        ``"timed out"`` does not contain ``"timeout"``; before the fix this fell
        through to a naked 500. With the provider stamping 504 it now classifies
        as a gateway timeout off typed metadata regardless of the wording.
        """
        from faultmaven.exceptions import LLMException

        cause = LLMException(
            "Fireworks API request timed out after 90s (model: deepseek-v4-flash)",
            status_code=504,
        )
        exc = self._classify(cause)
        assert exc.status_code == 504
        assert exc.status_code != 500

    def test_rate_limit_429(self):
        exc = self._classify(self._llm(status_code=429))
        assert exc.status_code == 429
        assert exc.headers["x-error-code"] == "RATE_LIMIT_EXCEEDED"
        assert exc.headers["Retry-After"] == "60"

    def test_over_capacity_503(self):
        exc = self._classify(self._llm(status_code=503))
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_OVER_CAPACITY"
        assert exc.headers["Retry-After"] == "60"

    def test_other_5xx_degrades_to_503_unavailable(self):
        exc = self._classify(self._llm(status_code=500))
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"
        assert exc.headers["Retry-After"] == "60"

    def test_provider_4xx_maps_to_502_no_retry(self):
        # e.g. Gemini 400 "schema produces too many states" — retrying the same
        # request fails identically, so NO Retry-After.
        exc = self._classify(self._llm(status_code=400))
        assert exc.status_code == 502
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_ERROR"
        assert "Retry-After" not in exc.headers

    def test_llm_no_status_retryable_flag_respected(self):
        exc = self._classify(self._llm(retryable=True))
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"
        assert exc.headers["Retry-After"] == "30"

    def test_llm_no_status_terminal_maps_to_502(self):
        # No status code, not retryable (e.g. "returned no choices").
        exc = self._classify(self._llm())
        assert exc.status_code == 502
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_ERROR"
        assert "Retry-After" not in exc.headers

    def test_schema_parse_validation_error_degrades_to_503(self):
        from pydantic import BaseModel, ValidationError

        class _M(BaseModel):
            x: int

        try:
            _M(x="not-an-int")
        except ValidationError as ve:
            cause = ve
        exc = self._classify(cause)
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_INVALID_RESPONSE"
        assert exc.headers["Retry-After"] == "30"

    def test_json_decode_error_degrades_to_503(self):
        import json

        try:
            json.loads("{not json")
        except json.JSONDecodeError as je:
            cause = je
        exc = self._classify(cause)
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_INVALID_RESPONSE"

    def test_billing_via_details_maps_to_402(self):
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )
        from faultmaven.exceptions import QUOTA_EXHAUSTED, ServiceException

        wrapped = ServiceException("x", details={"error_code": QUOTA_EXHAUSTED})
        exc = llm_service_error_http_exception(wrapped, "corr-9")
        assert exc.status_code == 402
        assert exc.headers["x-error-code"] == QUOTA_EXHAUSTED
        assert "Retry-After" not in exc.headers

    def test_billing_via_cause_chain_maps_to_402(self):
        from faultmaven.exceptions import QUOTA_EXHAUSTED

        # LLMException auto-classifies a 402 body as QUOTA_EXHAUSTED.
        cause = self._llm(status_code=402, message="insufficient_quota")
        exc = self._classify(cause)
        assert exc.status_code == 402
        assert exc.headers["x-error-code"] == QUOTA_EXHAUSTED

    def test_billing_precedence_over_status_code(self):
        # A billing error must never be mistaken for a transient 429.
        cause = self._llm(
            status_code=429, message="You have exceeded your current quota"
        )
        exc = self._classify(cause)
        assert exc.status_code == 402

    def test_unclassifiable_falls_back_to_500_bounded_detail(self):
        from faultmaven.exceptions import ServiceException

        exc = self._classify(ServiceException("some internal failure"))
        assert exc.status_code == 500
        assert exc.headers["x-error-code"] == "SERVICE_ERROR"
        assert exc.headers["Retry-After"] == "10"
        assert len(exc.detail) < 260  # bounded, never dumps internals wholesale

    def test_correlation_id_threaded_and_optional(self):
        with_id = self._classify(self._llm(status_code=429), correlation_id="cid-x")
        assert with_id.headers["x-correlation-id"] == "cid-x"
        without = self._classify(self._llm(status_code=429), correlation_id=None)
        assert "x-correlation-id" not in without.headers

    def test_typed_metadata_found_through_nested_cause(self):
        # ServiceException → RuntimeError → LLMException: still classified typed.
        from faultmaven.exceptions import LLMException, ServiceException

        llm = LLMException("boom", status_code=503)
        try:
            raise RuntimeError("mid") from llm
        except RuntimeError as mid:
            try:
                raise ServiceException("Turn processing failed") from mid
            except ServiceException as outer:
                wrapped = outer
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )

        exc = llm_service_error_http_exception(wrapped, "c")
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_OVER_CAPACITY"

    # --- Engine semantic error_code path (with_retry → MilestoneEngineError) ---
    # The primary turn path routes LLM calls through LLMErrorHandler.with_retry,
    # which converts the provider exception to a semantic error_code and re-raises
    # MilestoneEngineError WITHOUT a __cause__ link to the original — so no
    # LLMException/ValidationError is reachable on the chain. The code is threaded
    # onto the wrapper's details["error_code"].

    def _by_engine_code(self, code):
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )
        from faultmaven.exceptions import ServiceException

        wrapped = ServiceException(
            "Turn processing failed: Structured output generation failed",
            details={"error_code": code},
        )
        return llm_service_error_http_exception(wrapped, "corr-e")

    def test_engine_retry_exhausted_maps_to_503(self):
        exc = self._by_engine_code("RETRY_EXHAUSTED")
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"
        assert exc.headers["Retry-After"] == "30"

    def test_engine_unknown_error_parse_maps_to_503(self):
        # UNKNOWN_ERROR is what a schema-parse failure becomes on the retry path.
        exc = self._by_engine_code("UNKNOWN_ERROR")
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"

    def test_engine_token_limit_maps_to_503(self):
        exc = self._by_engine_code("TOKEN_LIMIT")
        assert exc.status_code == 503

    def test_engine_circuit_open_maps_to_503(self):
        """fm#1287 — an open LLM breaker is transient and must answer 503 with a
        Retry-After, the same as RETRY_EXHAUSTED.

        Before it had a code of its own, an open breaker reached this mapping as
        UNKNOWN_ERROR, which lands on the same 503 by accident. The status is
        unchanged; what changes is the ``x-error-code`` an operator reads and
        the engine message behind it, which no longer says "unknown" about a
        failure the system understands completely.
        """
        from faultmaven.exceptions import PROVIDER_CIRCUIT_OPEN

        exc = self._by_engine_code(PROVIDER_CIRCUIT_OPEN)
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"
        assert exc.headers["Retry-After"] == "30"

    def test_unknown_engine_code_still_falls_through_to_500(self):
        """POSITIVE CONTROL for the mapping tests above: a code the table does
        NOT know must still reach the generic 500, or "maps to 503" would be
        true of every string."""
        exc = self._by_engine_code("SOMETHING_NOBODY_DEFINED")
        assert exc.status_code == 500
        assert exc.headers["x-error-code"] == "SERVICE_ERROR"

    def test_engine_model_not_found_maps_to_502(self):
        exc = self._by_engine_code("MODEL_NOT_FOUND")
        assert exc.status_code == 502
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_ERROR"
        assert "Retry-After" not in exc.headers

    def test_engine_auth_failed_maps_to_502(self):
        exc = self._by_engine_code("AUTH_FAILED")
        assert exc.status_code == 502
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_ERROR"

    def test_engine_code_read_from_cause_chain_error_code(self):
        # Even without details threading, an error_code on a cause-chain
        # exception (MilestoneEngineError.error_code) is honored.
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )
        from faultmaven.exceptions import ServiceException

        class _EngineErr(Exception):
            def __init__(self, msg, error_code):
                super().__init__(msg)
                self.error_code = error_code

        eng = _EngineErr("generation failed", "RETRY_EXHAUSTED")
        try:
            raise ServiceException("Turn processing failed") from eng
        except ServiceException as outer:
            wrapped = outer
        exc = llm_service_error_http_exception(wrapped, "c")
        assert exc.status_code == 503
        assert exc.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"

    def test_provider_status_wins_over_engine_code(self):
        # When a raw LLMException status IS on the chain (direct path), it is more
        # specific than any threaded engine code and takes precedence.
        from faultmaven.api.exception_handlers import (
            llm_service_error_http_exception,
        )
        from faultmaven.exceptions import LLMException, ServiceException

        llm = LLMException("rate limited", status_code=429)
        try:
            raise ServiceException(
                "Turn processing failed", details={"error_code": "RETRY_EXHAUSTED"}
            ) from llm
        except ServiceException as outer:
            wrapped = outer
        exc = llm_service_error_http_exception(wrapped, "c")
        assert exc.status_code == 429  # provider status beats the engine code

    def test_unknown_engine_code_falls_back_to_500(self):
        # A code that is neither retryable nor terminal is not silently degraded.
        exc = self._by_engine_code("SOME_UNMAPPED_CODE")
        assert exc.status_code == 500
        assert exc.headers["x-error-code"] == "SERVICE_ERROR"
