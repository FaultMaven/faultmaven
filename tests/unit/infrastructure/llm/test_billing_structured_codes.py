"""Billing/quota exhaustion is classified from the provider's CODE, not its English (#548).

``is_billing_quota_error`` used to have two tiers: HTTP 402, and a tuple of
English markers matched against the message. That made the single load-bearing
billing classifier depend on nine providers' prose — a rewording, a
localization, or a provider whose sentence nobody had seen regressed the
``case_b639fac38fe0`` failure, where a permanent billing condition was retried
as a transient rate limit.

The machine-readable code was already in hand and already ignored. Every
adapter reads its own body shape once through
:func:`extract_provider_error_code` and passes the result to
``LLMException(provider_error_code=...)``, where it decided the *category* and
nothing else. #548 adds a third tier IN FRONT of the markers, keyed on that
same code.

What these tests hold:

* the three arms — **structured present** classifies with the marker tuple
  emptied; **structured absent** still classifies from the markers; **neither**
  stays retryable, which is the arm the incident was about;
* the codes deliberately kept OUT, each of which would turn a transient rate
  limit permanent.
"""

import pytest

import faultmaven.exceptions as exceptions_module
from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    LLMException,
    is_billing_error,
    is_billing_quota_error,
)
from faultmaven.infrastructure.llm.providers.base import extract_provider_error_code

pytestmark = [pytest.mark.unit, pytest.mark.llm]


# ---------------------------------------------------------------------------
# Representative error bodies, per provider
# ---------------------------------------------------------------------------
#
# Each row is (label, http status, body, expected extracted code, is billing?).
# Verified shapes:
#   openai      — ``error.code``/``error.type`` == "insufficient_quota". The
#                 installed SDK (openai 2.x) maps exceptions by HTTP STATUS
#                 only and enumerates no code strings, so the code is the
#                 server-side contract, not an SDK constant.
#   anthropic   — ``error.type`` == "billing_error", a member of the error
#                 discriminated union in the installed SDK
#                 (``anthropic.types.BetaBillingError``,
#                 ``type: Literal["billing_error"]``), a sibling of
#                 ``BetaRateLimitError``. Also carried here in its COARSE form,
#                 where the type is the undifferentiated
#                 ``invalid_request_error`` and only the prose says billing.
#   gemini      — ``error.status``. ``RESOURCE_EXHAUSTED`` covers a transient
#                 per-minute limit AND a hard quota; ``PERMISSION_DENIED``
#                 covers billing-disabled AND a mis-scoped key. Neither is
#                 admissible, so Gemini rides the marker tier.
#   openrouter  — numeric ``error.code`` (the HTTP status), deliberately
#                 skipped by the extractor; billing is the 402 itself.
#   huggingface — ``{"error": "<string>"}``: ``error`` is not an object, so
#                 there is no code to read; billing is the 402 itself.
#   cohere      — a bare ``{"message": ...}``, the repository's standing
#                 example of "no machine signal".
#   groq /
#   fireworks   — OpenAI-compatible envelopes with no billing-specific code of
#                 their own; they inherit the OpenAI family's if they emit it.
#   local       — self-hosted Ollama/vLLM/llama.cpp. There is no billing.
_BODIES = [
    (
        "openai_quota",
        429,
        '{"error":{"message":"You exceeded your current quota, please check '
        'your plan and billing details.","type":"insufficient_quota",'
        '"param":null,"code":"insufficient_quota"}}',
        "insufficient_quota",
        True,
    ),
    (
        "anthropic_billing_error",
        400,
        '{"type":"error","error":{"type":"billing_error","message":"Your '
        "credit balance is too low to access the Anthropic API. Please go to "
        'Plans & Billing to upgrade or purchase credits."}}',
        "billing_error",
        True,
    ),
    (
        # The same failure at the status the incident had, to show the code
        # tier does not depend on which status the provider chose.
        "anthropic_billing_error_429",
        429,
        '{"type":"error","error":{"type":"billing_error","message":"Your '
        'credit balance is too low."}}',
        "billing_error",
        True,
    ),
    (
        "anthropic_billing_coarse",
        400,
        '{"type":"error","error":{"type":"invalid_request_error","message":'
        '"Your credit balance is too low to access the Anthropic API."}}',
        "invalid_request_error",
        True,
    ),
    (
        "gemini_billing_disabled",
        403,
        '{"error":{"code":403,"message":"Gemini API has not been used in '
        "project 1234 before or it is disabled. Billing has not been enabled "
        'for this project.","status":"PERMISSION_DENIED"}}',
        "PERMISSION_DENIED",
        True,
    ),
    (
        "openrouter_insufficient_credits",
        402,
        '{"error":{"code":402,"message":"This request requires more credits, '
        'or fewer max_tokens."}}',
        None,
        True,
    ),
    (
        "huggingface_credits",
        402,
        '{"error":"You have exceeded your monthly included credits for '
        'Inference Providers."}',
        None,
        True,
    ),
    # --- transient rate limits: none of these is billing ---------------------
    (
        "gemini_rate_limit",
        429,
        '{"error":{"code":429,"message":"Quota exceeded for quota metric '
        '\'Generate Content API requests per minute\'","status":'
        '"RESOURCE_EXHAUSTED"}}',
        "RESOURCE_EXHAUSTED",
        False,
    ),
    (
        "groq_rate_limit",
        429,
        '{"error":{"message":"Rate limit reached for model on tokens per '
        'minute (TPM)","type":"tokens","code":"rate_limit_exceeded"}}',
        "rate_limit_exceeded",
        False,
    ),
    (
        "fireworks_rate_limit",
        429,
        '{"error":{"object":"error","type":"invalid_request_error","message":'
        '"rate limit exceeded"}}',
        "invalid_request_error",
        False,
    ),
    (
        "cohere_trial_key",
        429,
        '{"message":"You are using a Trial key, which is limited to 40 API '
        'calls / minute."}',
        None,
        False,
    ),
    (
        "local_out_of_memory",
        500,
        '{"error":"model requires more system memory than is available"}',
        None,
        False,
    ),
]

_IDS = [row[0] for row in _BODIES]


@pytest.mark.parametrize("label,status,body,code,billing", _BODIES, ids=_IDS)
def test_each_captured_body_classifies_as_expected(label, status, body, code, billing):
    """End to end over the real path: extract the code, raise as an adapter
    does, and read the classification off the exception."""
    assert extract_provider_error_code(body) == code, label

    error = LLMException(
        f"{label} API error {status}: {body}",
        status_code=status,
        provider_error_code=extract_provider_error_code(body),
    )

    assert (error.error_code == QUOTA_EXHAUSTED) is billing, label
    assert is_billing_error(error) is billing, label
    if billing:
        # Permanent, whatever the status said. Waiting cannot add credits.
        assert error.retryable is False, label


# ---------------------------------------------------------------------------
# Arm 1 — a structured signal classifies WITHOUT the substring path
# ---------------------------------------------------------------------------

_STRUCTURED = [
    ("openai", 429, "insufficient_quota"),
    ("anthropic", 400, "billing_error"),
    ("anthropic_at_429", 429, "billing_error"),
]


@pytest.mark.parametrize(
    "label,status,code", _STRUCTURED, ids=[r[0] for r in _STRUCTURED]
)
def test_structured_code_classifies_with_the_markers_emptied(
    monkeypatch, label, status, code
):
    """The point of the tier: with ``_BILLING_ERROR_MARKERS`` emptied there is
    no English left to match, and the classification still holds.

    Emptying the tuple rather than choosing a marker-free sentence is what
    makes this unfailable by accident — a message that happens to contain a
    marker would pass either way, and the test would be measuring nothing.
    """
    monkeypatch.setattr(exceptions_module, "_BILLING_ERROR_MARKERS", ())

    # Prose deliberately in a language the marker list has never seen, to
    # stand in for the rewording/localization this tier exists to survive.
    message = f"{label} API error {status}: Solde de crédit insuffisant."

    error = LLMException(message, status_code=status, provider_error_code=code)
    assert error.error_code == QUOTA_EXHAUSTED
    assert error.retryable is False
    assert is_billing_error(error) is True

    # And the helper directly, so the tier is pinned independently of the
    # exception's own precedence rules.
    assert is_billing_quota_error(message, status, code) is True


def test_a_structured_code_survives_a_status_that_says_transient(monkeypatch):
    """429 is the status the incident turned on: retryable by derivation, and
    the billing code has to beat it."""
    monkeypatch.setattr(exceptions_module, "_BILLING_ERROR_MARKERS", ())
    error = LLMException(
        "Anthropic API error 429: <no recognisable wording>",
        status_code=429,
        provider_error_code="billing_error",
    )
    assert error.retryable is False
    assert error.error_code == QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# Arm 2 — no structured signal, the substring fallback still classifies
# ---------------------------------------------------------------------------

_FALLBACK = [
    # Gemini's billing-disabled 403. Its only machine signal, PERMISSION_DENIED,
    # is inadmissible, so the markers are the whole of the detection.
    (
        "gemini_billing_disabled",
        403,
        "PERMISSION_DENIED",
        "Gemini API error 403: Billing has not been enabled for this project.",
    ),
    # Cohere publishes no code at all.
    (
        "cohere_no_code",
        429,
        None,
        "Cohere API error 429: your account is out of credits",
    ),
    # Anthropic's coarse body: the type is the undifferentiated 400 family.
    (
        "anthropic_coarse",
        400,
        "invalid_request_error",
        "Anthropic API error 400: Your credit balance is too low to access "
        "the Anthropic API.",
    ),
]


@pytest.mark.parametrize(
    "label,status,code,message", _FALLBACK, ids=[r[0] for r in _FALLBACK]
)
def test_marker_fallback_still_classifies_without_a_structured_code(
    monkeypatch, label, status, code, message
):
    error = LLMException(message, status_code=status, provider_error_code=code)
    assert error.error_code == QUOTA_EXHAUSTED, label
    assert error.retryable is False, label

    # ...and it really was the marker tier that answered, not the code tier
    # quietly covering for it: empty the markers and the classification goes.
    monkeypatch.setattr(exceptions_module, "_BILLING_ERROR_MARKERS", ())
    assert is_billing_quota_error(message, status, code) is False, label


def test_the_fallback_is_not_narrowed_by_the_new_tier():
    """Every marker that classified before #548 still classifies.

    The failure mode this forbids is trading breadth for precision: seven of
    the nine providers have no structured billing code, so shrinking this list
    to "what the code tier misses" would shrink detection for most of the fleet.
    """
    for marker in exceptions_module._BILLING_ERROR_MARKERS:
        assert is_billing_quota_error(f"Provider error: {marker}") is True, marker


# ---------------------------------------------------------------------------
# Arm 3 — neither tier fires: a transient rate limit stays retryable
# ---------------------------------------------------------------------------

_TRANSIENT = [
    (
        "gemini",
        429,
        "RESOURCE_EXHAUSTED",
        "Gemini API error 429: Quota exceeded "
        "for quota metric 'Generate Content API requests per minute'.",
    ),
    (
        "groq",
        429,
        "rate_limit_exceeded",
        "Groq API error 429: Rate limit reached "
        "for model on tokens per minute (TPM).",
    ),
    (
        "fireworks",
        429,
        "invalid_request_error",
        "Fireworks API error 429: rate " "limit exceeded",
    ),
    (
        "cohere",
        429,
        None,
        "Cohere API error 429: You are using a Trial key, "
        "which is limited to 40 API calls / minute.",
    ),
    (
        "openai_server",
        503,
        "server_error",
        "OpenAI API error 503: The server had "
        "an error while processing your request.",
    ),
    (
        "anthropic_overloaded",
        529,
        "overloaded_error",
        "Anthropic API error 529: " "Overloaded",
    ),
]


@pytest.mark.parametrize(
    "label,status,code,message", _TRANSIENT, ids=[r[0] for r in _TRANSIENT]
)
def test_transient_failures_are_never_read_as_billing(label, status, code, message):
    """The arm the incident was about, in the direction that costs a turn.

    Reading any of these as billing would FAIL, as QUOTA_EXHAUSTED, a failure
    that clears by itself, telling an operator to top up an account that is
    not empty.
    """
    error = LLMException(message, status_code=status, provider_error_code=code)
    assert error.error_code is None, label
    assert is_billing_error(error) is False, label
    assert error.retryable is True, label


@pytest.mark.parametrize(
    "code",
    [
        # Gemini: a per-minute rate limit AND a hard quota wear the same status.
        "RESOURCE_EXHAUSTED",
        "resource_exhausted",
        # Gemini: billing-disabled AND a key that lacks the API.
        "PERMISSION_DENIED",
        # The coarse families. Each covers billing and not-billing.
        "rate_limit_exceeded",
        "invalid_request_error",
        "api_error",
        "overloaded_error",
        "BadRequestError",
        # Not a code at all.
        "",
        None,
    ],
)
def test_ambiguous_codes_are_not_admitted_to_the_billing_set(code):
    """Admitting any of these would make the false direction permanent: a
    transient rate limit reported as exhausted credits, on every turn."""
    assert (
        is_billing_quota_error("an opaque provider failure", 429, code) is False
    ), code


def test_an_empty_code_does_not_match_the_frozenset():
    """``"" in frozenset`` is False, but the normalisation runs first — pinned
    so a later refactor cannot turn a missing code into a match."""
    assert "" not in exceptions_module._BILLING_PROVIDER_ERROR_CODES
    assert is_billing_quota_error("nothing to see", None, "   ") is False


def test_the_code_tier_matches_a_value_not_a_substring():
    """The code tier compares whole values. A message that merely CONTAINS one
    of the words cannot fire it, and a longer code that contains one is not a
    match either — which is the difference between this tier and the markers.
    """
    assert is_billing_quota_error("handled by is_billing_error()", 500, None) is False
    assert is_billing_quota_error("x", 500, "not_insufficient_quota_at_all") is False
