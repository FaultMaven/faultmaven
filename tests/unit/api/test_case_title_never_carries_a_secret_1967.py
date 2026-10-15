"""#1967: a case title never carries a secret, on any path.

A case title is shown to every reader of the case, in every list, and is never
reversed (#1956). Every title path used to end in text that only
``_sanitize_title_content`` had scrubbed — email, phone, IPv4, URL, path — and
nothing in the sanitizer's secret set: the smart-extractive path built
"Db Login Fails with Password=hunter2 on Replica" from a problem statement and
``is_title_valid`` accepted it; the fallback took ``case.description`` with no
scrub at all; and under the default ``SANITIZE_PII=false`` the LLM's prompt is
not redacted, so a value the model copied into its title passed too.

The invariant pinned here: no title is persisted — smart-extractive, LLM or
fallback (description included) — that contains a span the deployment's
sanitizer detects (its regex patterns always, Presidio's entities when the
analyzer is available) or a redaction placeholder in any letter case, whatever
``SANITIZE_PII`` says; and no candidate that fails is logged verbatim. A
sanitizer that cannot answer titles nothing (fail closed).

Every sanitizer here is a real ``DataSanitizer`` (regex half; Presidio stubbed
at its HTTP seam where a test needs the analyzer), and every model is a double:
no network, no live LLM call.
"""

from __future__ import annotations

import logging
import os
import random
import string
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.config.settings import FaultMavenSettings, env_var_spellings
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.security.pseudonym_key import (
    PseudonymKeyUnavailableError,
)
from faultmaven.infrastructure.security.redaction import (
    DataSanitizer,
    RedactionUnavailableError,
)
from faultmaven.modules.case.api import title_generation
from faultmaven.modules.case.api.title_generation import (
    _auto_title_case_if_default,
    _generate_and_persist_title,
    _generate_smart_extractive_title,
    _generate_title_with_llm,
    _scrub_title_source,
    _TitleDisclosureGateUnavailable,
    _TitleSubstanceTooThin,
    get_extractive_fallback_title,
    is_title_valid,
)
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models.case import Case
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned

pytestmark = pytest.mark.unit

SANITIZER = regex_only_sanitizer()
SECRET = "hunter2"
API_KEY = "sk-" + "a1b2c3d4" * 5
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
CARD = "4111 1111 1111 1111"
PLACEHOLDER = "<IP_ADDRESS_1a2b3c4d5e6f7a8b>"
_LOGGER = "faultmaven.modules.case.api.title_generation"


def _case(statement: str | None = None, **overrides) -> Case:
    fields = {
        "case_id": f"case_{uuid4().hex[:12]}",
        "title": "Case-260101-1",
        "description": "",
        "user_id": "user_123",
        "enterprise_id": "org_1967",
        "state": CaseState.INQUIRY,
    }
    fields.update(overrides)
    case = Case(**fields)
    if statement:
        case.inquiry.proposed_problem_statement = statement
    return case


def _service(case: Case, context: str = "") -> AsyncMock:
    """Serves ``case`` and ``context``; a title write really lands on it."""
    service = AsyncMock()
    service.get_case = AsyncMock(return_value=case)
    service.get_case_conversation_context = AsyncMock(return_value=context)

    async def _update(case_id, updates, user_id=None):
        case.title = updates["title"]
        return True

    service.update_case = AsyncMock(side_effect=_update)
    return service


def _model(content: str) -> MagicMock:
    """A model double answering ``content`` (no network, no live call)."""
    provider = MagicMock()
    provider.route = AsyncMock(
        return_value=LLMResponse(
            content=content,
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=8,
            response_time_ms=1,
        )
    )
    return provider


async def _name(case, service, *, llm=None, sanitizer=SANITIZER):
    return await _generate_and_persist_title(
        case=case,
        case_id=case.case_id,
        user_id="user_123",
        case_service=service,
        llm_provider=llm,
        max_words=8,
        hint=None,
        correlation_id="c-1967",
        sanitizer=sanitizer,
        redis_client=None,
    )


async def _auto(case, service, *, llm=None, sanitizer=SANITIZER):
    await _auto_title_case_if_default(
        case_id=case.case_id,
        user_id="user_123",
        case_service=service,
        llm_provider=llm,
        sanitizer=sanitizer,
        redis_client=None,
    )


def _assert_never_logged(caplog, *values: str) -> None:
    """No record — message or any extra field — carries any of ``values``."""
    assert caplog.records, "the probe captured no records at all"
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.__dict__!r}"
        for value in values:
            assert value not in rendered, (record.getMessage(), value)


def _gate_failures(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.getMessage() == "Title disclosure gate unavailable; no title is made"
    ]


# ---------------------------------------------------------------------------
# The smart-extractive path (T1, T2, T4, T5, T12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement, expected",
    [
        pytest.param(
            f"db login fails with password={SECRET} on replica",
            # The key stays in clear: a keyed secret's span is its value
            # (#1980 review ruling, value-only substitution).
            "Db Login Fails with Password=[password] on Replica",
            id="T1-password",
        ),
        pytest.param(
            f"api calls fail with key {API_KEY} since deploy",
            "Api Calls Fail with Key [api_key] Since Deploy",
            id="T2-api-key",
        ),
        pytest.param(
            f"auth rejects token {JWT} on every request",
            "Auth Rejects Token [jwt_token] on Every Request",
            id="T5-jwt",
        ),
        pytest.param(
            # The word alone is not a secret: nothing is taken out.
            "db login fails on replica after password change",
            "Db Login Fails on Replica After Password Change",
            id="T12-the-word-password",
        ),
    ],
)
async def test_the_extractive_title_names_the_type_never_the_value(
    statement, expected, caplog
):
    case = _case(statement)
    service = _service(case)
    model = _model("unused")

    with caplog.at_level(logging.DEBUG):
        title, source, _ = await _name(case, service, llm=model)

    assert (title, source) == (expected, "extractive")
    service.update_case.assert_awaited_once_with(
        case.case_id, {"title": expected}, "user_123"
    )
    model.route.assert_not_awaited()
    _assert_never_logged(caplog, SECRET, API_KEY, JWT)  # T11


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "connection refused for postgresql://u:p@db:5432/x from app pods",
            id="T4-url-with-a-path",
        ),
        pytest.param(
            "replica sync fails for postgresql://u:p@db:5432 from app pods",
            id="T4-url-without-a-path",
        ),
    ],
)
async def test_database_url_credentials_never_reach_the_title(statement):
    case = _case(statement)
    service = _service(case)

    title, _, _ = await _name(case, service, llm=_model("unused"))

    assert "u:p" not in title and "@" not in title
    assert case.title == title


async def test_an_aws_key_in_the_chat_never_reaches_the_title():
    """T3: the key arrives in the CHAT signals, with no problem statement."""
    chat = (
        f"User: s3 uploads are denied for {AWS_KEY} after the key rotation\n"
        "Assistant: which bucket?\n"
        "User: every bucket in the eu-west account returns access denied for "
        "the uploader role since the rotation finished this morning at nine\n"
        "User: the uploader pods were restarted twice and nothing changed for "
        "the role either"
    )
    case = _case()
    service = _service(case, chat)

    title, source, substance = await _name(case, service, llm=_model("unused"))

    assert substance >= title_generation.MIN_CONTENT_LENGTH_FOR_TITLE
    assert source == "extractive"
    assert title == "S3 Uploads Are Denied for [aws_access_key]"
    assert AWS_KEY.lower() not in title.lower()


# ---------------------------------------------------------------------------
# The description fallback (T6)
# ---------------------------------------------------------------------------


def test_the_description_is_scrubbed_before_a_title_is_built_from_it():
    """T6: the last fallback source used to reach the title with no scrub."""
    case = SimpleNamespace(
        description=f"login fails with password={SECRET} for every replica"
    )

    title = get_extractive_fallback_title("", "", case, 8, sanitizer=SANITIZER)

    assert title == "Login Fails with Password=[password] for Every Replica"


async def test_the_no_model_path_gates_the_description_too():
    case = SimpleNamespace(
        title="Case-260101-1",
        description=f"login fails with password={SECRET} for every replica",
    )

    title, source = await _generate_title_with_llm(
        "", case, 8, None, "", None, redaction_ctx=None, sanitizer=SANITIZER
    )

    assert (title, source) == (
        "Login Fails with Password=[password] for Every Replica",
        "extractive",
    )


# ---------------------------------------------------------------------------
# The LLM path (T7) and the backstop
# ---------------------------------------------------------------------------

# Over EXTRACTIVE_MAX_CONTENT_LENGTH of chat, so the case takes the model path.
_LONG_CHAT = "\n".join(
    [
        f"User: replica logins fail for the app service account, password={SECRET}",
        "User: the failures started right after the maintenance window on the "
        "primary database cluster last night and have not stopped since then",
        "User: restarting the connection pooler did not help and the replica "
        "lag graph looks normal, so this is not a replication delay at all",
    ]
)
_T7_FALLBACK = "Replica Logins Fail for the App Service Account"


@pytest.mark.parametrize("sanitize_pii", [False, True])
async def test_a_model_title_carrying_a_secret_gets_the_scrubbed_fallback(
    sanitize_pii, caplog
):
    """T7: the model copies a secret into its title. Under SANITIZE_PII=false
    its prompt is not redacted at all; the gate does not depend on it."""
    assert len(_LONG_CHAT) >= title_generation.EXTRACTIVE_MAX_CONTENT_LENGTH
    case = _case()
    service = _service(case, _LONG_CHAT)
    model = _model(f"Fix password={SECRET} Login")

    with sanitize_pii_pinned(sanitize_pii), caplog.at_level(logging.DEBUG):
        title, source, _ = await _name(case, service, llm=model)

    model.route.assert_awaited_once()
    prompt = model.route.await_args.kwargs["prompt"]
    assert SECRET not in prompt and "[password]" in prompt  # sources scrubbed
    assert (title, source) == (_T7_FALLBACK, "fallback")
    service.update_case.assert_awaited_once_with(
        case.case_id, {"title": _T7_FALLBACK}, "user_123"
    )
    _assert_never_logged(caplog, SECRET)  # T11


async def test_the_backstop_persists_nothing_it_does_not_pass(caplog):
    """A dirty candidate that got past the first gate is refused at the write."""
    case = _case("db login fails on the replica after the maintenance window")
    service = _service(case)
    dirty = f"Fix Password={SECRET} Login"

    with (
        patch.object(
            title_generation,
            "_generate_title_with_llm",
            new=AsyncMock(return_value=(dirty, "llm")),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        with pytest.raises(ValidationException):
            await _name(case, service, llm=_model("unused"))

    service.update_case.assert_not_awaited()
    assert case.title == "Case-260101-1"
    _assert_never_logged(caplog, SECRET)


@pytest.mark.parametrize(
    "dirty",
    [
        pytest.param(f"Fix Password={SECRET} Login", id="detected-span"),
        pytest.param(f"Pods Crash on {PLACEHOLDER} Hosts", id="upper-placeholder"),
        pytest.param(
            f"Pods Crash on {PLACEHOLDER.lower()} Hosts", id="lower-placeholder"
        ),
    ],
)
async def test_the_backstop_refuses_every_kind_of_dirty_title(dirty):
    case = _case("db login fails on the replica after the maintenance window")
    service = _service(case)

    with patch.object(
        title_generation,
        "_generate_title_with_llm",
        new=AsyncMock(return_value=(dirty, "llm")),
    ):
        with pytest.raises(ValidationException):
            await _name(case, service, llm=_model("unused"))

    service.update_case.assert_not_awaited()


# ---------------------------------------------------------------------------
# Secret-only cases (T8, ruling A6)
# ---------------------------------------------------------------------------


async def test_a_secret_only_case_is_too_thin_and_costs_no_model_call():
    """T8 / A6: the statement is measured after the gate. Several secrets
    still leave nothing to name, so the case refuses before any model call."""
    case = _case(f"password={SECRET}{SECRET} {AWS_KEY} {API_KEY}")
    service = _service(case, f"User: here it is {API_KEY} {AWS_KEY} ok")
    model = _model("unused")

    with patch.object(
        title_generation, "_generate_title_with_llm", new=AsyncMock()
    ) as titler:
        with pytest.raises(_TitleSubstanceTooThin):
            await _name(case, service, llm=model)

    titler.assert_not_awaited()
    model.route.assert_not_awaited()
    service.update_case.assert_not_awaited()


async def test_a_secret_only_case_keeps_its_placeholder_through_the_turn_hook():
    case = _case(f"password={SECRET}{SECRET} {AWS_KEY}")
    service = _service(case)

    await _auto(case, service, llm=_model("unused"))

    assert case.title == "Case-260101-1"
    service.update_case.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fail closed (T9 and one test per failure class, ruling A2)
# ---------------------------------------------------------------------------


async def test_no_sanitizer_titles_nothing(caplog):
    """T9: production always injects one; without it nothing is safe to show."""
    case = _case("checkout api returns 502 for a third of all requests")
    service = _service(case)

    with caplog.at_level(logging.WARNING):
        await _auto(case, service, sanitizer=None)

    assert case.title == "Case-260101-1"
    service.update_case.assert_not_awaited()
    (failure,) = _gate_failures(caplog)
    assert failure.reason == "no_sanitizer"
    assert any("Auto-titling failed" in r.getMessage() for r in caplog.records)


def _cloud_sanitizer_without_a_key() -> DataSanitizer:
    """A real sanitizer for a cloud deployment that has no pseudonym key."""
    with pytest.MonkeyPatch.context() as mp:
        for key in env_var_spellings(os.environ, "REDACTION_PSEUDONYM_KEY"):
            mp.delenv(key)
        mp.setenv("DEPLOYMENT_MODE", "cloud")
        mp.setenv("SKIP_SERVICE_CHECKS", "true")
        settings = FaultMavenSettings()
    assert settings.is_cloud and settings.protection.pseudonym_key is None
    return DataSanitizer(settings=settings)


async def test_a_missing_pseudonym_key_titles_nothing(caplog):
    """``PseudonymKeyUnavailableError`` from the source scrub: no title."""
    sanitizer = _cloud_sanitizer_without_a_key()
    with pytest.raises(PseudonymKeyUnavailableError):
        sanitizer.apply_regex_redaction(f"password={SECRET}", {})
    case = _case(f"db login fails with password={SECRET} on replica")
    service = _service(case)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(_TitleDisclosureGateUnavailable):
            await _name(case, service, llm=_model("unused"), sanitizer=sanitizer)

    service.update_case.assert_not_awaited()
    (failure,) = _gate_failures(caplog)
    assert (failure.gate_stage, failure.reason) == (
        "source",
        "PseudonymKeyUnavailableError",
    )
    _assert_never_logged(caplog, SECRET)


async def test_a_missing_pseudonym_key_does_not_block_a_case_with_no_secret():
    """Positive control: the key is needed only to write a placeholder, so a
    case with nothing to redact is detected clean and named."""
    case = _case("checkout api returns 502 for a third of all requests")
    service = _service(case)

    title, _, _ = await _name(
        case, service, llm=_model("unused"), sanitizer=_cloud_sanitizer_without_a_key()
    )

    assert title == "Checkout Api Returns 502 for a Third"


def _analyzer_down(sanitizer):
    """The analyzer errors on every call."""

    def post(*args, **kwargs):
        raise ConnectionError("analyzer down")

    return patch.object(sanitizer.session, "post", side_effect=post)


def _breaker_open(sanitizer):
    """The circuit breaker in front of the analyzer is open."""
    for _ in range(sanitizer.circuit_breaker.failure_threshold + 1):
        sanitizer.circuit_breaker.record_failure(ConnectionError("analyzer down"))
    assert not sanitizer.circuit_breaker.can_execute()
    return patch.object(sanitizer.session, "post", side_effect=AssertionError)


@pytest.mark.parametrize("outage", [_analyzer_down, _breaker_open])
async def test_the_fail_closed_posture_writes_nothing_while_the_analyzer_errors(
    outage, caplog
):
    """``RedactionUnavailableError`` from the full detection under the
    fail-closed posture (``SANITIZE_PII=true``, ``PROTECTION_FAIL_OPEN=false``):
    no title, and no model call after it — the failure is on the extractive
    candidate. (A fail-open posture accepts regex-only detection while the
    analyzer errors, as every other redaction path does: the invariant's stated
    scope, not this gate's to override.)"""
    sanitizer = regex_only_sanitizer(SANITIZE_PII="true", PROTECTION_FAIL_OPEN="false")
    sanitizer.analyzer_available = True
    case = _case(f"card {CARD} declined at the checkout gateway")
    service = _service(case)
    model = _model("unused")

    with outage(sanitizer), caplog.at_level(logging.WARNING):
        with pytest.raises(RedactionUnavailableError):
            sanitizer.sanitize_text_with_registry("anything at all", {})
        with pytest.raises(_TitleDisclosureGateUnavailable):
            await _name(case, service, llm=model, sanitizer=sanitizer)

    model.route.assert_not_awaited()
    service.update_case.assert_not_awaited()
    (failure,) = _gate_failures(caplog)
    assert (failure.gate_stage, failure.reason) == (
        "candidate",
        "RedactionUnavailableError",
    )


class _ExplodingDetection:
    """Real regex half; the full detection raises."""

    def apply_regex_redaction(self, text, registry):
        return SANITIZER.apply_regex_redaction(text, registry)

    def sanitize_text_with_registry(self, text, registry):
        raise RuntimeError("detector crashed")


async def test_any_other_sanitizer_exception_titles_nothing(caplog):
    case = _case("checkout api returns 502 for a third of all requests")
    service = _service(case)

    with caplog.at_level(logging.WARNING):
        await _auto(case, service, sanitizer=_ExplodingDetection())

    assert case.title == "Case-260101-1"
    service.update_case.assert_not_awaited()
    (failure,) = _gate_failures(caplog)
    assert (failure.gate_stage, failure.reason) == ("candidate", "RuntimeError")


class _NonTextDetection:
    """Real regex half; the full detection answers with something not text."""

    def apply_regex_redaction(self, text, registry):
        return SANITIZER.apply_regex_redaction(text, registry)

    def sanitize_text_with_registry(self, text, registry):
        return None


@pytest.mark.parametrize(
    "sanitizer, stage",
    [
        # The minimal DI container's stand-in: every call answers a Mock.
        pytest.param(MagicMock(), "source", id="mock-sanitizer"),
        pytest.param(_NonTextDetection(), "candidate", id="none-from-detection"),
    ],
)
async def test_a_non_text_answer_titles_nothing(sanitizer, stage, caplog):
    case = _case("checkout api returns 502 for a third of all requests")
    service = _service(case)

    with caplog.at_level(logging.WARNING):
        await _auto(case, service, sanitizer=sanitizer)

    assert case.title == "Case-260101-1"
    service.update_case.assert_not_awaited()
    (failure,) = _gate_failures(caplog)
    assert (failure.gate_stage, failure.reason) == (stage, "non_text_result")


# ---------------------------------------------------------------------------
# Presidio's entities count when the analyzer is available (T10)
# ---------------------------------------------------------------------------


def _analyzer_finding(value: str):
    """``session.post`` for the analyzer: reports ``value`` wherever it occurs."""
    seen: list[str] = []

    def post(url, json=None, timeout=None):
        text = json["text"]
        seen.append(text)
        start = text.find(value)
        results = (
            []
            if start < 0
            else [
                {
                    "start": start,
                    "end": start + len(value),
                    "entity_type": "CREDIT_CARD",
                    "score": 1.0,
                }
            ]
        )
        return SimpleNamespace(status_code=200, json=lambda: results)

    return post, seen


@pytest.mark.parametrize("reported", [CARD, "no-such-value"])
async def test_an_analyzer_entity_in_the_candidate_titles_nothing(reported, caplog):
    """T10: the card number is no regex pattern's; only the analyzer knows it.
    The control (the analyzer reports nothing) is titled, so the refusal is
    the entity's doing."""
    sanitizer = regex_only_sanitizer()
    sanitizer.analyzer_available = True
    post, seen = _analyzer_finding(reported)
    case = _case(f"card {CARD} declined at the checkout gateway")
    service = _service(case)

    with (
        patch.object(sanitizer.session, "post", side_effect=post),
        caplog.at_level(logging.DEBUG),
    ):
        await _auto(case, service, sanitizer=sanitizer)

    assert any(CARD in text for text in seen), "the analyzer was never consulted"
    if reported == CARD:
        assert case.title == "Case-260101-1"
        service.update_case.assert_not_awaited()
        _assert_never_logged(caplog, CARD)
    else:
        assert case.title == f"Card {CARD} Declined"


# ---------------------------------------------------------------------------
# The scrub order (re-ruled after the review of #1993) and placeholders in any
# letter case (D1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # The sanitizer reads the RAW text first. This pin is inverted from
        # the PR's first head, which ran ``_sanitize_title_content`` first to
        # keep ``[ip]`` and to stop the database-URL pattern from reaching a
        # later email: that order cut credentials into fragments the sanitizer
        # no longer recognised (the fragment tests below). With type markers a
        # private address keeps a marker of its own...
        (
            "10.0.0.5 unreachable from the bastion host",
            "[ip_address] unreachable from the bastion host",
        ),
        # ...a public one, which the sanitizer does not cover, still gets
        # ``_sanitize_title_content``'s, which runs after...
        (
            "8.8.8.8 unreachable from the bastion host",
            "[ip] unreachable from the bastion host",
        ),
        # ...and a database URL with no credentials is no secret: its user
        # part never runs across whitespace to a later ``@`` (#1980), so the
        # URL stays and the email gets ``_sanitize_title_content``'s marker.
        (
            "postgresql://db:5432 is down, email me@example.com when fixed",
            "postgresql://db:5432 is down, email [email] when fixed",
        ),
        # A placeholder already in a source, in any letter case, becomes the
        # type marker.
        (
            f"pods crash on {PLACEHOLDER.lower()} after the deploy",
            "pods crash on [ip_address] after the deploy",
        ),
    ],
)
def test_the_source_scrub_order(text, expected):
    assert _scrub_title_source(text, SANITIZER) == expected


# The review's fragment cases (p1): spans the sanitizer detects whole on the
# raw text, which ``_sanitize_title_content`` running first cut into pieces the
# sanitizer no longer matched — and which then passed the candidate gate too.
_FRAGMENT_CASES = [
    pytest.param(
        "uploads fail with aws_secret_access_key="
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY on the s3 role",
        ["wJalrXUtnFEMI", "K7MDENG", "bPxRfiCYEXAMPLEKEY"],
        id="aws-secret-with-slashes",
    ),
    pytest.param(
        "replica sync fails for "
        "postgresql://app:Winter2024!x@db.prod.example.com:5432 from app pods",
        ["Winter2024", "app:"],
        id="db-url-bang-dotted-host",
    ),
    pytest.param(
        "replica sync fails for postgresql://app:Winter2024!x@db.internal:5432 now",
        ["Winter2024"],
        id="db-url-bang-internal-host",
    ),
    pytest.param(
        "mysql://root:S3cr3t#1@db.internal.net refuses connections from batch",
        ["S3cr3t", "root:"],
        id="db-url-hash-in-password",
    ),
    pytest.param(
        "replica sync fails for postgresql://u:ab/cd/ef@db:5432 from app pods",
        ["ab/cd", "cd/ef", "ef@db"],
        id="db-url-slashes-in-password",
    ),
    pytest.param(
        "replica sync fails for "
        "postgresql://admin:hunter2@db.prod.example.com:5432 from app pods",
        ["hunter2", "admin:"],
        id="db-url-plain-password-dotted-host",
    ),
]


@pytest.mark.parametrize("text, fragments", _FRAGMENT_CASES)
async def test_no_fragment_of_a_detected_secret_survives_the_source_scrub(
    text, fragments
):
    assert SANITIZER.apply_regex_redaction(text, {}) != text, "control: detected"

    scrubbed = _scrub_title_source(text, SANITIZER)
    title = _generate_smart_extractive_title(scrubbed, 12)

    for fragment in fragments:
        assert fragment.lower() not in scrubbed.lower(), (fragment, scrubbed)
        assert title is None or fragment.lower() not in title.lower()
    if title:
        assert await title_generation._passes_disclosure_gate(title, SANITIZER)


@pytest.mark.parametrize("sanitize_pii", [False, True])
async def test_no_fragment_reaches_the_title_prompt(sanitize_pii):
    """p8: the prompt is built from the scrubbed sources, so a fragment cut
    out of a credential would reach the provider under either posture."""
    chat = "\n".join(
        [_LONG_CHAT.replace(f", password={SECRET}", "")]
        + [f"User: and then {text}" for text, _ in (c.values for c in _FRAGMENT_CASES)]
    )
    case = _case()
    service = _service(case, chat)
    model = _model("Replica Login Failures After Maintenance")

    with sanitize_pii_pinned(sanitize_pii):
        await _name(case, service, llm=model)

    prompt = model.route.await_args.kwargs["prompt"]
    for _, fragments in (c.values for c in _FRAGMENT_CASES):
        for fragment in fragments:
            assert fragment.lower() not in prompt.lower(), fragment


# The review's fuzz (p2), bounded and seeded: per class the sanitizer detects,
# no run of 6+ characters of the secret survives the source scrub. The
# sanitizer misses no trial on the raw text either: its pattern set (#1980)
# detects every one of these classes whole.
_FUZZ_TRIALS = 250
_B64 = string.ascii_letters + string.digits + "/+"
_PW = string.ascii_letters + string.digits + "!#$%&*?=^~"


def _aws(rng):
    secret = "".join(rng.choice(_B64) for _ in range(40))
    return secret, f"uploads fail with aws_secret_access_key={secret} on the role"


def _db_dotted(rng):
    pw = "".join(rng.choice(_PW) for _ in range(12))
    return pw, f"replica fails for postgresql://app:{pw}@db.prod.example.com:5432 now"


def _db_dotted_path(rng):
    pw = "".join(rng.choice(_PW) for _ in range(12))
    return pw, f"replica fails for postgresql://app:{pw}@db.example.com:5432/app now"


def _db_bare_host(rng):
    pw = "".join(rng.choice(_PW + "/") for _ in range(12))
    return pw, f"replica fails for mysql://app:{pw}@db:3306 from pods"


def _password_kv(rng):
    pw = "".join(rng.choice(_PW + "/@.") for _ in range(12))
    return pw, f"db login fails with password={pw} on replica"


def _leaks(secret: str, text: str, run: int = 6) -> bool:
    secret, text = secret.lower(), text.lower()
    return any(secret[i : i + run] in text for i in range(len(secret) - run + 1))


@pytest.mark.parametrize(
    "make", [_aws, _db_dotted, _db_dotted_path, _db_bare_host, _password_kv]
)
def test_no_run_of_a_detected_secret_survives_the_source_scrub_fuzz(make):
    rng = random.Random(f"1967-{make.__name__}")
    missed, leaked = [], []
    for _ in range(_FUZZ_TRIALS):
        secret, text = make(rng)
        if _leaks(secret, SANITIZER.apply_regex_redaction(text, {})):
            missed.append(text)
            continue
        scrubbed = _scrub_title_source(text, SANITIZER)
        if _leaks(secret, scrubbed):
            leaked.append((text, scrubbed))
    assert missed == [], missed[:3]
    assert leaked == [], leaked[:3]


def test_the_extractive_path_refuses_a_placeholder_before_title_casing():
    """D1, first half: casing turned ``<IP_ADDRESS_…>`` into
    ``<ip_address_…>``, and the cased title was what got validated."""
    assert (
        _generate_smart_extractive_title(
            f"pods crash on {PLACEHOLDER} after the deploy", 8
        )
        is None
    )


def test_a_lower_case_placeholder_is_not_a_title():
    """D1, second half: validation matches a placeholder in any letter case."""
    assert (
        is_title_valid(f"Pods Crash on {PLACEHOLDER.lower()} After the Deploy") is False
    )
    assert is_title_valid("Pods Crash on [ip_address] After the Deploy") is True


# ---------------------------------------------------------------------------
# One full detection per distinct candidate (the verdict memo)
# ---------------------------------------------------------------------------


class _CountingSanitizer:
    """A real regex-only sanitizer that records every full detection."""

    def __init__(self) -> None:
        self.inner = regex_only_sanitizer()
        self.detections: list[str] = []

    def apply_regex_redaction(self, text, registry):
        return self.inner.apply_regex_redaction(text, registry)

    def sanitize_text_with_registry(self, text, registry):
        self.detections.append(text)
        return self.inner.sanitize_text_with_registry(text, registry)


_CLEAN_LONG_CHAT = _LONG_CHAT.replace(f", password={SECRET}", " since the window")


@pytest.mark.parametrize(
    "statement, chat, model_title, expected_source, detections",
    [
        pytest.param(
            "checkout api returns 502 for a third of all requests",
            "",
            "unused",
            "extractive",
            1,
            id="extractive",
        ),
        pytest.param(
            None,
            _CLEAN_LONG_CHAT,
            "Replica Login Failures After Maintenance",
            "llm",
            1,
            id="llm",
        ),
        pytest.param(
            None,
            _CLEAN_LONG_CHAT,
            f"Fix password={SECRET} Login",
            "fallback",
            2,
            id="llm-refused-then-fallback",
        ),
    ],
)
async def test_a_candidate_costs_one_full_detection_backstop_included(
    statement, chat, model_title, expected_source, detections
):
    """The backstop re-checks the very string the validation point passed;
    the generation's memo answers it, so the analyzer is asked once per
    distinct candidate. Prompt redaction is pinned off so only the gate's
    detections are counted."""
    sanitizer = _CountingSanitizer()
    case = _case(statement)
    service = _service(case, chat)

    with sanitize_pii_pinned(False):
        title, source, _ = await _name(
            case, service, llm=_model(model_title), sanitizer=sanitizer
        )

    assert source == expected_source
    assert sanitizer.detections[-1] == title
    assert len(sanitizer.detections) == detections, sanitizer.detections
    assert len(set(sanitizer.detections)) == len(sanitizer.detections)


# ---------------------------------------------------------------------------
# The regenerate route's hint is a title source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [False, True])
async def test_the_hint_is_scrubbed_before_it_reaches_the_prompt(sanitize_pii):
    case = _case()
    service = _service(case, _CLEAN_LONG_CHAT)
    model = _model("Replica Login Failures After Maintenance")

    with sanitize_pii_pinned(sanitize_pii):
        await _generate_and_persist_title(
            case=case,
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=model,
            max_words=8,
            hint=f"the db password={SECRET} is the one being rejected",
            correlation_id="c-1967",
            sanitizer=SANITIZER,
            redis_client=None,
        )

    prompt = model.route.await_args.kwargs["prompt"]
    assert SECRET not in prompt
    assert "the db password=[password] is the one being rejected" in prompt


# ---------------------------------------------------------------------------
# POST /cases/{case_id}/title answers an unavailable gate 503
# ---------------------------------------------------------------------------


class _DetectionRaisesWithTheText:
    """Real regex half; the full detection raises with the text in its message,
    which must not reach the response."""

    def apply_regex_redaction(self, text, registry):
        return SANITIZER.apply_regex_redaction(text, registry)

    def sanitize_text_with_registry(self, text, registry):
        raise RuntimeError(f"analyzer exploded on: {text}")


def _title_route(case, sanitizer):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.contracts import UserDTO
    from faultmaven.modules.case.api.routes.dependencies import (
        _di_get_case_service_dependency,
    )
    from faultmaven.modules.case.api.routes.router import router

    app = FastAPI()
    for exc, handler in get_exception_handlers().items():
        app.add_exception_handler(exc, handler)
    app.include_router(router, prefix="/api/v1")
    service = _service(case)
    app.state.llm_provider = None
    app.state.sanitizer = sanitizer
    app.state.redis_client = None
    user = UserDTO(
        user_id="user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        is_active=True,
    )
    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
    return TestClient(app, raise_server_exceptions=False), service


@pytest.mark.parametrize(
    "sanitizer",
    [
        pytest.param(None, id="no-sanitizer"),
        pytest.param(MagicMock(), id="mock-sanitizer"),
        pytest.param(_DetectionRaisesWithTheText(), id="detection-raises"),
    ],
)
def test_the_title_route_answers_an_unavailable_gate_503(sanitizer):
    case = _case(f"db login fails with password={SECRET} on replica")
    client, service = _title_route(case, sanitizer)

    with client:
        response = client.post(f"/api/v1/cases/{case.case_id}/title", json={})

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Title generation is temporarily unavailable"
    assert response.headers["x-correlation-id"]
    assert SECRET not in response.text and "password" not in response.text
    service.update_case.assert_not_awaited()
    assert case.title == "Case-260101-1"


def test_the_title_route_control_names_the_case():
    case = _case(f"db login fails with password={SECRET} on replica")
    client, service = _title_route(case, SANITIZER)

    with client:
        response = client.post(f"/api/v1/cases/{case.case_id}/title", json={})

    assert response.status_code == 200, response.text
    assert (
        response.json()["title"] == "Db Login Fails with Password=[password] on Replica"
    )
    assert case.title == response.json()["title"]
    service.update_case.assert_awaited_once()
