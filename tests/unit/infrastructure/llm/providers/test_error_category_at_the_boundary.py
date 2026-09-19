"""#509 — an LLM failure is classified where the provider's answer is in hand.

Recovery used to be selected by matching substrings of provider prose, in two
places that had to be kept in step by importing one tuple from the other
(``CONTEXT_OVERFLOW_PHRASES``), plus a third list of sentences for
retryability (``RetryConfig.retryable_patterns``). Nine providers word errors
differently and may reword them at any release — and, worse, those lists were
applied to EVERY exception that reached the engine, including ones the engine
composed itself and ones no provider raised.

What ships instead: the provider boundary derives an ``LLMErrorCategory`` from
the ``status_code`` and the machine-readable error code the provider publishes,
falling back to that provider's own wording only where the provider publishes
nothing narrower. Downstream asks WHAT the failure was.

These tests pin the three halves of that:

* :func:`extract_provider_error_code` — the code really is read out of each
  provider's body shape, including the shapes where there is none.
* :func:`classify_llm_error` — the precedence, especially the two orderings
  that are load-bearing: a machine code outranks wording, and wording outranks
  the HTTP status.
* Every provider actually wires the extractor into its non-200 raise — derived
  from ``PROVIDER_SCHEMA`` so a provider added later is checked without anyone
  remembering to add it here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import (
    LLMErrorCategory,
    LLMException,
    classify_llm_error,
    declared_llm_category,
)
from faultmaven.infrastructure.llm.providers.base import (
    ProviderConfig,
    extract_provider_error_code,
)
from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA

pytestmark = [pytest.mark.unit, pytest.mark.llm]


# ---------------------------------------------------------------------------
# extract_provider_error_code — the real body shapes
# ---------------------------------------------------------------------------

_BODIES = [
    # OpenAI / Groq / Fireworks / OpenRouter / vLLM-OAI: nested ``error.code``.
    (
        "openai",
        '{"error":{"message":"This model\'s maximum context length is 8192 '
        'tokens","type":"invalid_request_error","param":"messages",'
        '"code":"context_length_exceeded"}}',
        "context_length_exceeded",
    ),
    # Anthropic: no ``code`` at all, only the coarse ``type``.
    (
        "anthropic",
        '{"type":"error","error":{"type":"invalid_request_error",'
        '"message":"prompt is too long: 250000 tokens > 200000 maximum"}}',
        "invalid_request_error",
    ),
    # Google/Gemini: ``code`` is the HTTP STATUS as an integer, and the real
    # signal is ``status``. Taking the integer would let "400" answer for every
    # 400 there is, which is why numbers are skipped.
    (
        "gemini",
        '{"error":{"code":400,"message":"The input token count (1200000) '
        'exceeds the maximum number of tokens allowed (1048576).",'
        '"status":"INVALID_ARGUMENT"}}',
        "INVALID_ARGUMENT",
    ),
    # vLLM's OpenAI-compatible error shape: flat, and ``code`` is again an int.
    (
        "vllm",
        '{"object":"error","message":"This model\'s maximum context length is '
        '4096 tokens","type":"BadRequestError","code":400}',
        "BadRequestError",
    ),
    # Cohere: a bare message. No machine signal at all — which is what makes
    # the wording tier reachable rather than dead code.
    ("cohere", '{"message":"too many tokens"}', None),
    # Not JSON (an nginx 502 page, a gateway's plain text).
    ("html", "<html><body>502 Bad Gateway</body></html>", None),
    # JSON, but not an object.
    ("json_list", "[1, 2, 3]", None),
    ("empty", "", None),
]


@pytest.mark.parametrize(
    "label,body,expected", _BODIES, ids=[row[0] for row in _BODIES]
)
def test_extracts_the_code_from_each_provider_body_shape(label, body, expected):
    assert extract_provider_error_code(body) == expected, label


def test_a_numeric_code_is_never_returned_as_a_code():
    """The Gemini/vLLM trap, asserted on its own.

    Both nest the HTTP status under the key ``code``. Stringifying it would
    hand ``classify_llm_error`` the token ``"400"`` for every bad request those
    providers ever make — a code that means nothing, arriving in the tier that
    outranks everything else.
    """
    assert extract_provider_error_code('{"error":{"code":400}}') is None
    assert extract_provider_error_code('{"code":503}') is None
    # A bool is an int subclass; same reasoning.
    assert extract_provider_error_code('{"error":{"code":true}}') is None


# ---------------------------------------------------------------------------
# classify_llm_error — the precedence
# ---------------------------------------------------------------------------


def test_a_machine_code_outranks_the_wording():
    """The bug in the issue, inverted into a test.

    A 400 whose MESSAGE reads like a context overflow but whose CODE says the
    request carried an unsupported parameter is a config error. Reading the
    sentence is what masked OpenAI's "Unsupported parameter: 'max_tokens'" as
    "Context too large" and sent the engine into a futile compression loop.
    """
    assert (
        classify_llm_error(
            status_code=400,
            provider_error_code="unsupported_parameter",
            message="maximum context length is 8192 tokens",
        )
        is LLMErrorCategory.REQUEST_REJECTED
    )


def test_wording_outranks_the_status():
    """A 5xx that reports an overflow in its body is still an overflow.

    A gateway can answer 503 with a provider's context-length rejection in the
    body. Classifying that as merely transient would spend every retry
    re-sending the same oversized prompt and then fail the turn, instead of
    degrading to the minimal prompt (#662's NO-COLLAPSE guarantee).
    """
    assert (
        classify_llm_error(status_code=503, message="upstream: context length exceeded")
        is LLMErrorCategory.CONTEXT_OVERFLOW
    )
    # ...and with nothing recognisable in the body, the same 503 is transient.
    assert (
        classify_llm_error(status_code=503, message="Service Unavailable")
        is LLMErrorCategory.TRANSIENT
    )


def test_input_overflow_outranks_output_truncation():
    """One message, both vocabularies. The PROMPT not fitting is the reading
    the recovery is designed for; raising the generation cap cannot help it."""
    assert (
        classify_llm_error(
            status_code=400, message="input truncated: context length exceeded"
        )
        is LLMErrorCategory.CONTEXT_OVERFLOW
    )


def test_a_generic_400_family_code_does_not_decide():
    """``invalid_request_error`` / ``INVALID_ARGUMENT`` cover an overflow AND a
    bad parameter, so mapping them would assert a distinction the provider did
    not make. They must fall through to the wording tier — which is the only
    reason Anthropic's and Gemini's overflows are recognised at all."""
    assert (
        classify_llm_error(
            status_code=400,
            provider_error_code="invalid_request_error",
            message="prompt is too long: 250000 tokens > 200000 maximum",
        )
        is LLMErrorCategory.CONTEXT_OVERFLOW
    )
    assert (
        classify_llm_error(
            status_code=400,
            provider_error_code="INVALID_ARGUMENT",
            message="The input token count (1200000) exceeds the maximum "
            "number of tokens allowed (1048576).",
        )
        is LLMErrorCategory.CONTEXT_OVERFLOW
    )


def test_nothing_authoritative_is_unknown_not_a_guess():
    """ "No signal" is its own answer. Collapsing it into any of the others is
    how a classifier fails open — the same reasoning as ``StopReason.UNKNOWN``.
    """
    assert classify_llm_error() is LLMErrorCategory.UNKNOWN
    assert (
        classify_llm_error(message="something went wrong") is LLMErrorCategory.UNKNOWN
    )


def test_bare_token_words_are_not_an_overflow():
    """The wording tier stays as narrow as the tuple it replaced: no bare
    "token", no bare "too long". Both fire on ordinary request validation."""
    for message in (
        "Invalid authentication token",
        "Your max_tokens value must be a positive integer",
        "value too long for type character varying(255)",
        "string too long",
    ):
        assert (
            classify_llm_error(status_code=400, message=message)
            is LLMErrorCategory.REQUEST_REJECTED
        ), message


def test_an_explicit_category_wins_over_every_derivation():
    """A provider that observed the failure itself — Gemini on
    ``finishReason=MAX_TOKENS``, where there is no HTTP error and no body to
    read — states what it saw, and nothing re-derives it from the sentence it
    happened to write."""
    error = LLMException(
        "Response truncated due to token limit. Increase max_tokens parameter "
        "or simplify prompt.",
        retryable=True,
        category=LLMErrorCategory.OUTPUT_TRUNCATION,
    )
    assert error.category is LLMErrorCategory.OUTPUT_TRUNCATION


def test_declared_category_walks_the_cause_chain_and_rejects_impostors():
    """Service code wraps provider failures as ``raise X(...) from e``."""
    inner = LLMException("context length exceeded", status_code=400)
    try:
        raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert declared_llm_category(outer) is LLMErrorCategory.CONTEXT_OVERFLOW

    # Nobody classified this one. ``None``, never ``UNKNOWN``: "not an LLM
    # failure" and "an LLM failure of unknown kind" are different answers.
    assert declared_llm_category(Exception("context length exceeded")) is None

    # A ``Mock``'s auto-attribute is truthy and equal to nothing, and a bare
    # string would silently never match an ``is`` comparison downstream.
    mocked = Exception("boom")
    mocked.category = MagicMock()
    assert declared_llm_category(mocked) is None
    stringy = Exception("boom")
    stringy.category = "context_overflow"
    assert declared_llm_category(stringy) is None


def test_a_cyclic_cause_chain_terminates():
    a = Exception("a")
    b = Exception("b")
    a.__cause__ = b
    b.__cause__ = a
    assert declared_llm_category(a) is None


# ---------------------------------------------------------------------------
# Every provider wires the extractor into its non-200 raise
# ---------------------------------------------------------------------------

# Derived from the registry, not hand-listed — same convention as
# ``test_provider_error_retryable.py`` and ``test_reasoning_intent.py``, and
# for the same reason: "every provider classifies its errors" is a
# cross-provider invariant, so the set it is checked over has to be the set of
# providers that exists, or the NEXT provider is silently exempt.
#
# ``local`` is excluded here and covered in ``test_local_transport_errors.py``
# instead: it picks between THREE wire transports at call time, so a single
# ``generate("hello")`` cannot exercise it the way this harness assumes.
_SCHEMA_PROVIDERS = [
    pytest.param(
        schema["provider_class"],
        f"https://{name}.test.invalid/v1",
        schema["default_model"],
        id=name,
    )
    for name, schema in sorted(PROVIDER_SCHEMA.items())
    if name != "local"
]

# An OpenAI-shaped overflow body. Synthetic for the providers that would never
# emit this exact shape — the invariant under test is that the ADAPTER hands
# its body to the extractor, which is the step a new provider forgets.
_OVERFLOW_BODY = (
    '{"error":{"message":"This model\'s maximum context length is 8192 '
    'tokens","type":"invalid_request_error","code":"context_length_exceeded"}}'
)

_PARAM_ERROR_BODY = (
    '{"error":{"message":"Unsupported parameter: \'max_tokens\' is not '
    "supported with this model. Use 'max_completion_tokens' instead.\","
    '"type":"invalid_request_error","param":"max_tokens",'
    '"code":"unsupported_parameter"}}'
)


def _config(name, base_url, model):
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
    )


def _mock_error_session(status: int, body: str):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value={})
    response.text = AsyncMock(return_value=body)
    response.headers = {}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# The one failure no error body reports: a cut ANSWER
# ---------------------------------------------------------------------------


def _mock_ok_session(payload: dict):
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value=payload)
    response.text = AsyncMock(return_value="")
    response.headers = {}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_gemini_declares_output_truncation_on_its_own_max_tokens_stop():
    """A cut answer arrives as an ordinary HTTP 200 with a short body, so there
    is no error body to classify and no status to read. Gemini is the one
    provider that notices and raises, and it must SAY what it saw.

    That declaration is the whole signal the max_tokens ladder reads
    (``is_output_truncation_error``). It used to be the literal
    "finishreason=max_tokens" in the sentence the adapter writes, which the
    adapter's own comment had to warn people not to reword; a category cannot
    be reworded.
    """
    from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider

    provider = GeminiProvider(
        _config(
            "gemini",
            "https://generativelanguage.test.invalid/v1beta",
            "gemini-3.7-flash",
        )
    )
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": '{"agent_response": "the kub'}]},
                "finishReason": "MAX_TOKENS",
            }
        ]
    }
    with patch("aiohttp.ClientSession", return_value=_mock_ok_session(payload)):
        with pytest.raises(LLMException) as exc_info:
            # Structured: a cut JSON body is unusable, which is what makes the
            # adapter raise rather than return the partial.
            await provider.generate(
                "why is node-3 NotReady?",
                response_format={"type": "json_object"},
            )

    assert exc_info.value.category is LLMErrorCategory.OUTPUT_TRUNCATION


def test_no_error_body_is_ever_read_as_a_cut_answer():
    """The discriminating half of the test above.

    Without it, that test would pass on a derivation that stamped
    OUTPUT_TRUNCATION on any body containing the word "truncated" — which is
    what the deleted ``_OUTPUT_TRUNCATION_PHRASES`` tuple did, and which routes
    a 4xx "request truncated" into a max_tokens ladder that cannot help it.
    """
    assert (
        classify_llm_error(
            status_code=400,
            message="Response truncated due to token limit "
            "(finishReason=MAX_TOKENS).",
        )
        is LLMErrorCategory.REQUEST_REJECTED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _SCHEMA_PROVIDERS)
async def test_every_provider_classifies_an_overflow_from_its_body(
    provider_class, base_url, model
):
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch(
        "aiohttp.ClientSession",
        return_value=_mock_error_session(400, _OVERFLOW_BODY),
    ):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    assert exc_info.value.provider_error_code == "context_length_exceeded"
    assert exc_info.value.category is LLMErrorCategory.CONTEXT_OVERFLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _SCHEMA_PROVIDERS)
async def test_every_provider_separates_a_rejected_parameter_from_an_overflow(
    provider_class, base_url, model
):
    """The discriminating half. Without it the test above would pass on a
    provider that stamped CONTEXT_OVERFLOW on every 400 it ever made."""
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch(
        "aiohttp.ClientSession",
        return_value=_mock_error_session(400, _PARAM_ERROR_BODY),
    ):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    assert exc_info.value.provider_error_code == "unsupported_parameter"
    assert exc_info.value.category is LLMErrorCategory.REQUEST_REJECTED
