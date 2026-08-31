"""Tests for LLMErrorHandler

Tests retry logic and error recovery for LLM API calls.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.core.investigation.llm_error_handler import (
    ErrorAction,
    ErrorResult,
    LLMErrorHandler,
    RetryConfig,
)


@pytest.fixture
def handler():
    return LLMErrorHandler()


@pytest.fixture
def fast_handler():
    """Handler with fast delays for testing."""
    config = RetryConfig(
        max_retries=3,
        base_delay_seconds=0.01,  # Fast for testing
        max_delay_seconds=0.1,
    )
    return LLMErrorHandler(config)


class TestErrorClassification:
    """Test error type classification."""

    def test_retryable_rate_limit_error(self, handler):
        """Rate limit errors should be retryable."""
        error = Exception("Rate limit exceeded, please slow down")
        assert handler.is_retryable_error(error) is True

    def test_retryable_connection_error(self, handler):
        """Untyped connection wording still reaches the phrase fallback.

        Was ``test_retryable_timeout_error``, which asserted that "Connection
        timeout after 30s" is retryable and read as coverage for timeouts. It
        never was: the string matches on ``"connection"``, so it passed
        identically with ``"timeout"`` present or absent from the list and could
        not have caught #1287. Renamed to say what it actually measures. Real
        timeout coverage is ``TestTypedRetryability`` below, which asserts on
        types rather than sentences.
        """
        error = Exception("Connection timeout after 30s")
        assert handler.is_retryable_error(error) is True

    def test_retryable_503_error(self, handler):
        """503 errors should be retryable."""
        error = Exception("Service returned 503 status code")
        assert handler.is_retryable_error(error) is True

    def test_non_retryable_error(self, handler):
        """Regular errors should not be retryable."""
        error = Exception("Invalid JSON format in response")
        assert handler.is_retryable_error(error) is False

    def test_retryable_502_bad_gateway(self, handler):
        """502 Bad Gateway (string-pattern path) should be retryable."""
        error = Exception("Upstream returned 502 Bad Gateway")
        assert handler.is_retryable_error(error) is True

    def test_llm_exception_5xx_is_retryable_authoritative(self, handler):
        """LLMException with 5xx status must be retryable via typed metadata,
        not the string-pattern fallback."""
        from faultmaven.exceptions import LLMException

        error = LLMException("opaque body", status_code=502)
        assert handler.is_retryable_error(error) is True

    def test_llm_exception_4xx_is_not_retryable_authoritative(self, handler):
        """LLMException with 4xx status must be non-retryable even if its
        message happens to contain a retryable-looking word."""
        from faultmaven.exceptions import LLMException

        # Message includes "timeout" — string-pattern would say retryable.
        # The typed flag must win.
        error = LLMException("400 bad request: timeout field invalid", status_code=400)
        assert handler.is_retryable_error(error) is False

    def test_retryable_walks_cause_chain(self, handler):
        """When a generic wrapper exception has a typed LLMException cause,
        retryability is taken from the cause."""
        from faultmaven.exceptions import LLMException

        try:
            try:
                raise LLMException("upstream 502", status_code=502)
            except LLMException as inner:
                raise RuntimeError("outer wrapper") from inner
        except RuntimeError as outer:
            assert handler.is_retryable_error(outer) is True

    def test_auth_error_detection(self, handler):
        """Auth errors should be detected."""
        error = Exception("API key is invalid or expired")
        assert handler.is_auth_error(error) is True

    def test_auth_401_error(self, handler):
        """401 errors should be auth errors."""
        error = Exception("HTTP 401 Unauthorized")
        assert handler.is_auth_error(error) is True

    def test_token_limit_error(self, handler):
        """Token limit errors should be detected."""
        error = Exception("Request exceeds maximum context length")
        assert handler.is_token_limit_error(error) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "This model's maximum context length is 8192 tokens",
            "context_length_exceeded",
            "Please reduce the length of the messages",
            "prompt is too long: 250000 tokens > 200000 maximum",
            "bad request: too many tokens",  # Cohere-style overflow (was lost
            # when bare "token" was dropped; shared with _is_context_length_error)
            "exceeds the maximum context (8192)",
        ],
    )
    def test_real_overflow_detected(self, handler, msg):
        """Genuine context overflow still triggers compress."""
        assert handler.is_token_limit_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "EOF while parsing a value",  # truncated JSON output
            "Unterminated string starting at line 3",
            "finishReason=MAX_TOKENS",  # Gemini output cap
        ],
    )
    def test_truncation_is_not_read_as_an_input_overflow(self, handler, msg):
        """The prompt fit; the ANSWER did not. Compressing memory is not the
        first remedy for that — raising the generation cap is — so truncation
        must not enter the COMPRESS_MEMORY branch by wording alone."""
        assert handler.is_token_limit_error(Exception(msg)) is False

    def test_truncation_retryability_comes_from_the_typed_signal(self, handler):
        """Not from matching words in a message.

        The engine owns the generation cap, so only the engine knows whether
        retrying is still useful; it says so with ``OutputTruncationError``.
        Keying retryability on phrases instead is what #513 was: neither real
        truncation site emits the words that were being matched — CPython's
        decoder says "Expecting ',' delimiter", never "EOF while parsing" — so
        the list looked like coverage while matching nothing the code produces.
        """
        from faultmaven.core.investigation.llm_error_handler import (
            OutputTruncationError,
        )

        # Decoder wording on a bare exception decides nothing either way.
        assert handler.is_retryable_error(Exception("EOF while parsing a value")) is (
            False
        )

        # The typed signal decides, and it decides both ways: retry while
        # raising the cap is still an option, stop once it is not.
        assert (
            handler.is_retryable_error(OutputTruncationError("cut", cap_reached=False))
            is True
        )
        assert (
            handler.is_retryable_error(OutputTruncationError("cut", cap_reached=True))
            is False
        )

    @pytest.mark.parametrize(
        "msg",
        [
            # The exact OpenAI 400 the over-greedy matcher used to misclassify
            # as a context overflow (masking it as "Context too large").
            "Unsupported parameter: 'max_tokens' is not supported with this "
            "model. Use 'max_completion_tokens' instead.",
            "OpenAI API error 400: invalid_request_error unsupported_parameter",
            "Invalid authentication token",  # bare 'token' must not match
            "Your max_tokens value must be a positive integer",  # param shape error
            # A DB/validation "too long" must not be read as a context overflow
            # (why the classifier keys on "prompt is too long", not bare "too long").
            "value too long for type character varying(255)",
        ],
    )
    def test_config_and_param_errors_not_token_limit(self, handler, msg):
        """A request-shape / parameter error is NOT a token-limit overflow —
        matching it would mask the real cause and loop on futile compression."""
        assert handler.is_token_limit_error(Exception(msg)) is False


class TestDelayCalculation:
    """Test exponential backoff delay calculation."""

    def test_first_retry_delay(self, handler):
        """First retry should use base delay."""
        delay = handler.calculate_delay(0)
        assert delay == 2.0  # Base delay

    def test_exponential_backoff(self, handler):
        """Delay should increase exponentially."""
        delay_0 = handler.calculate_delay(0)
        delay_1 = handler.calculate_delay(1)
        delay_2 = handler.calculate_delay(2)

        assert delay_1 == delay_0 * 2
        assert delay_2 == delay_0 * 4

    def test_max_delay_cap(self, handler):
        """Delay should be capped at max_delay_seconds."""
        delay = handler.calculate_delay(10)  # Would be 2048s without cap
        assert delay == 30.0  # Max delay


class TestErrorHandling:
    """Test error handling actions."""

    @pytest.mark.asyncio
    async def test_auth_error_escalates(self, handler):
        """Auth errors should escalate immediately."""
        error = Exception("API key invalid")
        result = await handler.handle_error(error)

        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == "AUTH_FAILED"

    @pytest.mark.asyncio
    async def test_token_limit_triggers_compress(self, handler):
        """Token limit errors should trigger memory compression."""
        error = Exception("Context length exceeded")
        result = await handler.handle_error(error)

        assert result.action == ErrorAction.COMPRESS_MEMORY
        assert result.error_code == "TOKEN_LIMIT"

    @pytest.mark.asyncio
    async def test_retryable_error_retries(self, fast_handler):
        """Retryable errors should trigger retry."""
        error = Exception("Rate limit exceeded")
        result = await fast_handler.handle_error(error, retry_count=0)

        assert result.action == ErrorAction.RETRY
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted_fails(self, handler):
        """Exhausted retries should fail."""
        error = Exception("Rate limit exceeded")
        result = await handler.handle_error(error, retry_count=3)

        assert result.action == ErrorAction.FAIL
        assert result.error_code == "RETRY_EXHAUSTED"


class TestWithRetry:
    """Test with_retry execution wrapper."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, handler):
        """Successful operation should return immediately."""
        operation = AsyncMock(return_value="success")

        result, error = await handler.with_retry(operation)

        assert result == "success"
        assert error is None
        operation.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, fast_handler):
        """Transient errors should trigger retries."""
        call_count = 0

        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Rate limit exceeded")
            return "success"

        result, error = await fast_handler.with_retry(failing_then_success)

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_unknown_error_fails_fast(self, fast_handler):
        """Unknown (non-retryable) errors should fail immediately, not retry."""
        main_op = AsyncMock(side_effect=Exception("Unknown error"))

        result, error = await fast_handler.with_retry(operation=main_op)

        assert result is None
        assert error is not None
        assert error.action == ErrorAction.FAIL
        assert error.error_code == "UNKNOWN_ERROR"
        # Must not retry — unknown errors are surfaced immediately.
        main_op.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, fast_handler):
        """All retries exhausted should return None."""
        operation = AsyncMock(side_effect=Exception("Rate limit exceeded"))

        result, error = await fast_handler.with_retry(operation)

        assert result is None
        assert error is not None
        assert error.action == ErrorAction.FAIL

    @pytest.mark.asyncio
    async def test_original_exception_preserved_on_failure(self, handler):
        """The triggering exception must be carried back on the ErrorResult so
        the caller can surface its real message — losing it turned an informative
        provider overflow into an opaque engine message (#662). Callers fold it
        into their message TEXT; they deliberately do not chain it onto
        __cause__, which would outrank the engine error_code at the HTTP
        boundary (see ErrorResult.original_exception)."""
        boom = Exception("This model's maximum context length is 8192 tokens")
        operation = AsyncMock(side_effect=boom)

        result, error = await handler.with_retry(operation=operation)

        assert result is None
        assert error is not None
        assert error.error_code == "TOKEN_LIMIT"
        assert error.original_exception is boom


class TestErrorTracking:
    """Test error counting and summary."""

    @pytest.mark.asyncio
    async def test_error_counting(self, handler):
        """Errors should be counted by type."""
        error1 = ValueError("value error")
        error2 = ValueError("another value error")
        error3 = TypeError("type error")

        await handler.handle_error(error1)
        await handler.handle_error(error2)
        await handler.handle_error(error3)

        summary = handler.get_error_summary()

        assert summary["error_counts"]["ValueError"] == 2
        assert summary["error_counts"]["TypeError"] == 1
        assert summary["total_errors"] == 3


class TestBillingErrorHandling:
    """Billing/quota exhaustion is a permanent, operator-actionable failure.
    It must escalate (not retry) with the QUOTA_EXHAUSTED code, so the API and
    UI can tell the user to add credits instead of looping on 'try again'.
    Regression for case_b639fac38fe0."""

    def test_is_billing_error_detects_typed_code(self, handler):
        from faultmaven.exceptions import LLMException

        err = LLMException("insufficient_quota", status_code=429)
        assert handler.is_billing_error(err) is True

    def test_is_billing_error_walks_cause_chain(self, handler):
        """A typed billing error wrapped as the __cause__ of a generic
        exception (e.g. MilestoneEngineError) is still detected."""
        from faultmaven.exceptions import LLMException

        billing = LLMException("check your plan and billing details", status_code=429)
        try:
            raise RuntimeError("Turn processing failed") from billing
        except RuntimeError as wrapped:
            assert handler.is_billing_error(wrapped) is True

    def test_plain_rate_limit_is_not_billing(self, handler):
        from faultmaven.exceptions import LLMException

        err = LLMException("rate limit reached, retry soon", status_code=429)
        assert handler.is_billing_error(err) is False

    @pytest.mark.asyncio
    async def test_handle_billing_escalates_with_quota_code(self, handler):
        from faultmaven.exceptions import QUOTA_EXHAUSTED, LLMException

        err = LLMException(
            "You exceeded your current quota, please check your plan and billing details",
            status_code=429,
        )
        result = await handler.handle_error(err)

        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == QUOTA_EXHAUSTED
        # Message is operator-actionable (mentions credits/billing), not "try again".
        assert "credit" in result.message.lower() or "billing" in result.message.lower()

    @pytest.mark.asyncio
    async def test_handle_open_breaker_billing_escalates(self, handler):
        """An open-breaker error carrying QUOTA_EXHAUSTED is classified as
        billing, not as an opaque unknown/retryable error."""
        from faultmaven.exceptions import QUOTA_EXHAUSTED
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        err = CircuitBreakerError(
            "Circuit breaker is open for LLM_Providers",
            error_code=QUOTA_EXHAUSTED,
        )
        result = await handler.handle_error(err)

        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == QUOTA_EXHAUSTED

    @pytest.mark.asyncio
    async def test_billing_not_retried_by_with_retry(self, fast_handler):
        """with_retry must return immediately on billing (no wasted attempts)."""
        from faultmaven.exceptions import QUOTA_EXHAUSTED, LLMException

        attempts = 0

        async def op():
            nonlocal attempts
            attempts += 1
            raise LLMException("insufficient_quota", status_code=429)

        result, error = await fast_handler.with_retry(operation=op)

        assert result is None
        assert error is not None
        assert error.action == ErrorAction.ESCALATE
        assert error.error_code == QUOTA_EXHAUSTED
        assert attempts == 1


class TestTypedRetryability:
    """fm#1287 — retryability comes from a DECLARATION or a TYPE, never prose.

    The bug: the client-side deadline in ``BaseExternalClient.call_external``
    raised a bare ``TimeoutError("… timed out after 30.0s")``, and the phrase
    list contained ``"timeout"`` — not a substring of ``"timed out"``. A hung
    provider got zero retries.

    Adding ``"timed out"`` would have fixed the instance and left the class
    intact, so the phrase was REMOVED and two typed tiers were put ahead of the
    fallback. These tests are written against shapes that would satisfy a naive
    guard while violating its intent.
    """

    def test_bare_asyncio_timeout_is_retryable(self, handler):
        """``str(asyncio.TimeoutError())`` is the EMPTY STRING.

        No phrase list of any size can classify this, so a phrase-only
        classifier calls every un-wrapped ``wait_for`` timeout permanent. Two
        of ``local_provider``'s three transports (``_call_ollama_api``,
        ``_call_llamacpp_api``) raised exactly this shape.
        """
        error = asyncio.TimeoutError()
        assert str(error) == ""
        assert handler.is_retryable_error(error) is True

    def test_external_call_timeout_declares_retryable(self, handler):
        """The typed deadline error carries its own flag."""
        from faultmaven.exceptions import ExternalCallTimeout

        error = ExternalCallTimeout(
            "External call to LLM_Providers.route_llm_request timed out after 30.0s",
            service="LLM_Providers",
            operation="route_llm_request",
            timeout=30.0,
        )
        assert handler.is_retryable_error(error) is True

    def test_timed_out_wording_is_not_what_decides(self, handler):
        """The exact pre-fix sentence, untyped, must STILL be non-retryable.

        The fix is not a second spelling in the phrase list. If someone
        "restores" ``"timed out"`` there, this fails — and the typed tiers stop
        being what carries the behaviour.
        """
        error = Exception(
            "External call to LLM_Providers.route_llm_request timed out after 30.0s"
        )
        assert handler.is_retryable_error(error) is False

    def test_declared_false_beats_the_timeout_type_rule(self, handler):
        """A type rule must never override an explicit declaration.

        Shape: an exception that IS a ``TimeoutError`` but whose raising code
        said ``retryable=False``. Tier 3 alone would call it retryable.
        """

        class _DeclaredPermanentTimeout(TimeoutError):
            retryable = False

        assert handler.is_retryable_error(_DeclaredPermanentTimeout("gone")) is False

    def test_unset_retryable_is_not_a_declaration(self, handler):
        """``retryable`` absent → keep looking, do not read it as False.

        A ``getattr(..., False)`` default would make every untyped exception a
        permanent failure and silently disable the phrase fallback entirely.
        """

        class _Untyped(Exception):
            pass

        # No declaration anywhere, but the message matches a phrase.
        assert handler.is_retryable_error(_Untyped("upstream 503")) is True
        # No declaration, no phrase → the honest answer is False.
        assert handler.is_retryable_error(_Untyped("something odd")) is False

    def test_non_bool_retryable_is_not_a_declaration(self, handler):
        """A truthy non-bool must not be mistaken for a declaration.

        A ``Mock``'s auto-attribute is truthy, so a bare ``getattr`` check
        would make every mocked exception retryable forever — the exact shape
        that makes a guard unfailable in its own test suite.
        """
        error = Exception("something odd")
        error.retryable = Mock()  # truthy, not a bool
        assert handler.is_retryable_error(error) is False

        error2 = Exception("something odd")
        error2.retryable = "yes"  # truthy string
        assert handler.is_retryable_error(error2) is False

        error3 = Exception("something odd")
        error3.retryable = None  # explicit "no opinion"
        assert handler.is_retryable_error(error3) is False

    def test_first_declaration_on_the_chain_wins(self, handler):
        """A wrapper with no opinion defers to its cause."""
        from faultmaven.exceptions import ExternalCallTimeout

        try:
            try:
                raise ExternalCallTimeout("deadline")
            except ExternalCallTimeout as inner:
                raise RuntimeError("engine wrapper") from inner
        except RuntimeError as outer:
            assert handler.is_retryable_error(outer) is True

    def test_type_rule_also_reads_the_cause_chain(self, handler):
        """A bare timeout wrapped by an undeclared wrapper is still a timeout."""
        try:
            try:
                raise asyncio.TimeoutError()
            except asyncio.TimeoutError as inner:
                raise RuntimeError("engine wrapper") from inner
        except RuntimeError as outer:
            assert handler.is_retryable_error(outer) is True

    def test_retryable_prose_loses_to_a_disagreeing_type(self, handler):
        """A message that matches a phrase AND carries a contradicting type.

        The provider says 400 (permanent) while its body quotes "503". The
        declaration must win; matching the body would burn four attempts on a
        request that fails identically every time.
        """
        from faultmaven.exceptions import LLMException

        error = LLMException(
            "upstream gateway reported 503 over capacity", status_code=400
        )
        assert handler.is_retryable_error(error) is False

    def test_cyclic_cause_chain_terminates(self, handler):
        """A cycle must not hang the classifier."""
        a = Exception("a")
        b = Exception("b")
        a.__cause__ = b
        b.__cause__ = a
        assert handler.is_retryable_error(a) is False

    def test_the_fix_was_not_a_respelling(self):
        """``"timed out"`` must never be added to the phrase list.

        Structural, and deliberately paired with the behavioural tests above —
        on its own it would restate the patch. Its job is to fail if someone
        "fixes" a future timeout mismatch by adding the missing spelling instead
        of typing the raise site.

        ``"timeout"`` itself STAYS and is asserted present. Deleting it was a
        regression: the type rule only reaches exceptions inheriting from the
        builtin ``TimeoutError``, and ``httpx``/``redis`` timeouts do not — see
        ``test_third_party_timeouts_keep_their_phrase_fallback``.
        """
        cfg = RetryConfig()
        assert "timeout" in cfg.retryable_patterns
        assert "timed out" not in cfg.retryable_patterns

    def test_third_party_timeouts_keep_their_phrase_fallback(self, handler):
        """A timeout class that does NOT inherit from builtin ``TimeoutError``
        still has to classify as retryable.

        The type rule (tier 3) is not a superset of the phrase it replaced.
        ``aiohttp.ServerTimeoutError`` does subclass the builtin, but
        ``httpx.ReadTimeout`` and ``redis.exceptions.TimeoutError`` inherit from
        their own bases and declare no ``retryable`` — so with ``"timeout"``
        deleted they went from retryable to PERMANENT. Written against the real
        classes rather than a stand-in, because the whole finding is about what
        those libraries actually inherit from.
        """
        third_party = []
        try:
            import httpx

            third_party.append(httpx.ReadTimeout("Read timeout"))
        except ImportError:  # pragma: no cover - httpx is a hard dependency
            pass
        try:
            import redis.exceptions as redis_exceptions

            third_party.append(
                redis_exceptions.TimeoutError("Timeout reading from socket")
            )
        except ImportError:  # pragma: no cover - redis is a hard dependency
            pass

        # A universal over an empty set proves nothing.
        assert third_party, "neither httpx nor redis importable; cannot ask"

        for error in third_party:
            assert not isinstance(error, TimeoutError), (
                f"{type(error).__name__} now subclasses the builtin "
                f"TimeoutError, so this test no longer exercises the gap it "
                f"was written for"
            )
            assert handler.is_retryable_error(error) is True, type(error).__name__


class TestDeclarationOutranksProse:
    """fm#1287 follow-up — the rule has to hold in ``handle_error``, not only
    in ``is_retryable_error``.

    ``handle_error`` reaches three SUBSTRING classifiers before it ever asks
    about retryability: ``is_auth_error`` ("401"/"403"), then
    ``is_model_not_found_error`` ("404"), then ``is_token_limit_error``. Typing
    the provider transport raise sites made every provider fold aiohttp's raw
    text into the message — and that text carries ``host:port``.

    Measured before the gate existed, with ``retryable=True`` declared on every
    one of them::

        Cannot connect to host localhost:4040  -> ESCALATE MODEL_NOT_FOUND
        Cannot connect to host 10.0.0.5:8404   -> ESCALATE MODEL_NOT_FOUND
        Cannot connect to host proxy:8401      -> ESCALATE AUTH_FAILED

    So the PR that removed the ladder's dependence on prose is also what routed
    attacker-of-opportunity text INTO prose one layer up. Any local model server
    or proxy on a port containing 404/401/403 became permanently unavailable on
    its first connect failure.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "port",
        [
            "4040",  # contains 404 -> is_model_not_found_error
            "8404",  # contains 404, not at the start
            "8401",  # contains 401 -> is_auth_error
            "4030",  # contains 403 -> is_auth_error
        ],
    )
    async def test_declared_transient_survives_the_prose_classifiers(
        self, fast_handler, port
    ):
        """A declared-retryable transport error must reach the ladder."""
        from faultmaven.exceptions import LLMException

        error = LLMException(
            f"Gemini connection error: Cannot connect to host svc:{port} "
            f"ssl:default [Connect call failed]",
            retryable=True,
        )
        result = await fast_handler.handle_error(error, retry_count=0)
        assert result.action == ErrorAction.RETRY, (
            f"port {port} was matched as a status code and the declaration "
            f"discarded: {result.error_code}"
        )

    @pytest.mark.asyncio
    async def test_control_the_prose_classifiers_still_fire(self, fast_handler):
        """POSITIVE CONTROL. The gate must not disable the classifiers it
        precedes — otherwise the parametrized test above would pass simply
        because nothing classifies anything any more.

        Same three conditions, UNDECLARED, so prose is still allowed to decide.
        """
        cases = [
            ("model gpt-x not found (404)", "MODEL_NOT_FOUND"),
            ("401 unauthorized: invalid api key", "AUTH_FAILED"),
        ]
        for message, expected in cases:
            result = await fast_handler.handle_error(Exception(message), 0)
            assert result.error_code == expected, (message, result.error_code)

        overflow = await fast_handler.handle_error(
            Exception("prompt is too long: 250000 > 200000"), 0
        )
        assert overflow.action == ErrorAction.COMPRESS_MEMORY

    @pytest.mark.asyncio
    async def test_declared_permanent_still_falls_through_to_the_classifiers(
        self, fast_handler
    ):
        """Only ``True`` short-circuits.

        A declared NON-retryable error keeps flowing to the classifiers, because
        they add SEMANTICS the flag does not carry: a 404 is MODEL_NOT_FOUND, a
        401 is AUTH_FAILED. Short-circuiting on any declaration would flatten
        both into UNKNOWN_ERROR.
        """
        from faultmaven.exceptions import LLMException

        result = await fast_handler.handle_error(
            LLMException(
                "Gemini API error 404: model not found or inaccessible",
                status_code=404,
            ),
            0,
        )
        assert result.error_code == "MODEL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_billing_still_outranks_the_declaration_gate(self, fast_handler):
        """Quota exhaustion is permanent whatever the transport declared, and
        its check sits ahead of the gate. A 429 body naming billing must still
        ESCALATE rather than be retried into the ground."""
        from faultmaven.exceptions import QUOTA_EXHAUSTED, LLMException

        result = await fast_handler.handle_error(
            LLMException(
                "429 - You exceeded your current quota, please check your plan "
                "and billing details",
                status_code=429,
            ),
            0,
        )
        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == QUOTA_EXHAUSTED


class TestConfigErrorClassification:
    """fm#1287 follow-up — the empty-chain guard has to reach the user as a
    configuration error, not as "LLM error (LLMProviderError): …".

    Raising a described exception was only half the fix: ``LLMProviderError``
    is a different family from ``LLMException``, declares no ``retryable`` and
    matches no phrase, so it still landed on the UNKNOWN_ERROR tail — which the
    HTTP boundary maps to a RETRYABLE 503 with a Retry-After, for a condition
    that will never clear until an operator edits the environment.
    """

    @pytest.mark.asyncio
    async def test_config_error_escalates_with_its_own_code(self, fast_handler):
        from faultmaven.exceptions import LLM_CONFIG_ERROR
        from faultmaven.models.exceptions import LLMProviderError

        result = await fast_handler.handle_error(
            LLMProviderError(
                "No LLM provider is routable: the fallback chain is empty.",
                error_code=LLM_CONFIG_ERROR,
            ),
            0,
        )
        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == LLM_CONFIG_ERROR
        assert "not configured" in result.message

    @pytest.mark.asyncio
    async def test_control_an_unrelated_error_code_is_not_swallowed(self, fast_handler):
        """POSITIVE CONTROL: the branch keys on the specific code, not on
        merely having one."""
        err = Exception("something else entirely")
        err.error_code = "SOMETHING_ELSE"
        result = await fast_handler.handle_error(err, 0)
        assert result.error_code == "UNKNOWN_ERROR"


class TestCircuitBreakerClassification:
    """fm#1287 — an open breaker is a KNOWN failure, not an unknown one."""

    @pytest.mark.asyncio
    async def test_open_breaker_reports_its_own_code(self, fast_handler):
        """Not UNKNOWN_ERROR. The message ("Circuit breaker is open for
        LLM_Providers") matches no retry phrase and carries no provider status,
        so it fell to the unclassified tail — telling an operator "unknown"
        about the one failure the system understands completely.
        """
        from faultmaven.exceptions import PROVIDER_CIRCUIT_OPEN
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        result = await fast_handler.handle_error(
            CircuitBreakerError("Circuit breaker is open for LLM_Providers"),
            retry_count=0,
        )
        assert result.action == ErrorAction.FAIL
        assert result.error_code == PROVIDER_CIRCUIT_OPEN

    @pytest.mark.asyncio
    async def test_open_breaker_is_not_retried(self, fast_handler):
        """The breaker's recovery window (30s) outlasts the whole backoff
        ladder (2+4+8 = 14s), so a retry is guaranteed to meet it still open."""
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        result = await fast_handler.handle_error(
            CircuitBreakerError("Circuit breaker is open for LLM_Providers"),
            retry_count=0,
        )
        assert result.action != ErrorAction.RETRY

    @pytest.mark.asyncio
    async def test_open_breaker_keeps_a_latched_quota_code(self, fast_handler):
        """The billing check runs FIRST and must keep winning: a quota-latched
        breaker still escalates as QUOTA_EXHAUSTED (the case_b639fac38fe0
        chain), not as a transient circuit-open."""
        from faultmaven.exceptions import QUOTA_EXHAUSTED
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        result = await fast_handler.handle_error(
            CircuitBreakerError(
                "Circuit breaker is open for LLM_Providers",
                error_code=QUOTA_EXHAUSTED,
            ),
            retry_count=0,
        )
        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == QUOTA_EXHAUSTED

    @pytest.mark.asyncio
    async def test_open_breaker_keeps_a_latched_auth_code(self, fast_handler):
        """A latched credential rejection is permanent until an operator
        rotates the key, so it must route to the terminal AUTH_FAILED rather
        than to a transient code that invites the user to resend. The message
        contains no "auth", so the prose classifier could not see it."""
        from faultmaven.exceptions import PROVIDER_AUTH_FAILED
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        result = await fast_handler.handle_error(
            CircuitBreakerError(
                "Circuit breaker is open for LLM_Providers",
                error_code=PROVIDER_AUTH_FAILED,
            ),
            retry_count=0,
        )
        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == "AUTH_FAILED"

    @pytest.mark.asyncio
    async def test_another_services_breaker_is_not_reported_as_the_ai_provider(
        self, fast_handler
    ):
        """``CircuitBreakerError`` is raised by EVERY ``BaseExternalClient``.

        ChromaDB, Redis, Presidio and the runbook KB all use the same class, so
        an unscoped ``isinstance`` would answer a ChromaDB outage with "The AI
        provider failed repeatedly" — wrong, and unactionable.
        """
        from faultmaven.infrastructure.base_client import CircuitBreakerError

        result = await fast_handler.handle_error(
            CircuitBreakerError(
                "Circuit breaker is open for ChromaDB", service="ChromaDB"
            ),
            retry_count=0,
        )
        assert "AI provider failed repeatedly" not in result.message

    @pytest.mark.asyncio
    async def test_non_breaker_error_is_untouched(self, fast_handler):
        """POSITIVE CONTROL: the new branch must not swallow everything. A
        plain unclassifiable error still reaches the UNKNOWN_ERROR tail."""
        result = await fast_handler.handle_error(
            Exception("something wholly unclassifiable"), retry_count=0
        )
        assert result.action == ErrorAction.FAIL
        assert result.error_code == "UNKNOWN_ERROR"
