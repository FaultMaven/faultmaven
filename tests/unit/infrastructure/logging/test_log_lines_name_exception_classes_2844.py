"""A log line names a third party's exception by class chain, never its text (#2844).

A client's or a backend's exception text can carry its output: a cookie, a
credential, a response body. Every site the ruling lists, and every emitter of
``UnifiedLogger``, names the class chain instead. Each case asserts on the line
the real ``configure_structlog`` chain renders (JSON, and console with colour
forced), and shows the capture live: the same exception logged the old way does
reach it. The shared redactor also blanks four credential headers' values on
every rendered line, as defence in depth.
"""

import asyncio
import io
import json
import logging
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

import faultmaven.infrastructure.logging.config as logging_config
from faultmaven.exceptions import LLMException, ServiceError
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.concurrency import inflight, owned_claim
from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    get_logger,
)
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
from faultmaven.infrastructure.logging.unified import (
    clear_logger_cache,
    get_unified_logger,
)
from faultmaven.infrastructure.logging.url_redaction import (
    RedactingFormatter,
    redact_urls,
    redact_urls_bytes,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

SENTINEL = "SECRET-SENTINEL"


class BackendError(Exception):
    """A third party's exception, its text the backend's output."""


def _backend_error(cls=BackendError, retryable=None, **kwargs):
    try:
        try:
            raise ConnectionError(f"cause {SENTINEL}")
        except ConnectionError as cause:
            raise cls(f"{SENTINEL} set-cookie: sid=S", **kwargs) from cause
    except cls as exc:
        if retryable is not None:
            exc.retryable = retryable
        return exc


@pytest.fixture(params=["json", "console"])
def rendered(request, monkeypatch):
    """What the real handler writes, every global restored (as ``written`` in
    ``test_url_query_redaction.py``)."""
    root = logging.getLogger()
    saved_singleton = logging_config._logger_config
    get_logger("faultmaven.test.warmup")
    saved = structlog.get_config(), root.level, list(root.handlers)
    if request.param == "console":
        tty = types.SimpleNamespace(isatty=lambda: True)
        monkeypatch.setattr(logging_config, "sys", types.SimpleNamespace(stderr=tty))
    config = LoggingConfig()
    config.LOG_FORMAT, config.LOG_HUMAN_READABLE = request.param, False
    FaultMavenLogger(config).configure_structlog()
    clear_logger_cache()
    root.setLevel(logging.DEBUG)
    buffer = io.StringIO()
    marker = FaultMavenLogger._ROOT_HANDLER_MARKER
    next(h for h in root.handlers if getattr(h, marker, False)).setStream(buffer)
    try:
        yield buffer
        if request.param == "console" and buffer.getvalue():
            assert "\x1b[" in buffer.getvalue(), "the console case was not coloured"
    finally:
        structlog.configure(**saved[0])
        root.setLevel(saved[1])
        root.handlers[:] = saved[2]
        logging_config._logger_config = saved_singleton
        clear_logger_cache()


def _named(rendered, chain, old_way):
    """The sentinel is absent and ``chain`` present; then the positive control:
    the exception logged the old way reaches the same capture."""
    output = rendered.getvalue()
    assert SENTINEL not in output
    assert chain in output
    old_way()
    assert SENTINEL in rendered.getvalue()[len(output) :]


def _old_structlog(exc):
    return lambda: get_logger("faultmaven.control").error("old", error=exc)


def _old_stdlib(exc):
    return lambda: logging.getLogger("faultmaven.control").warning("%r", exc)


class _Client(BaseExternalClient):
    def __init__(self):
        super().__init__("test_client", "TestService", enable_circuit_breaker=False)


@pytest.mark.parametrize("in_request", [False, True], ids=["no-context", "request"])
class TestCallExternal:
    @pytest.fixture(autouse=True)
    def _request_context(self, in_request):
        coordinator = LoggingCoordinator()
        if in_request:
            coordinator.start_request()
        yield
        if in_request:
            coordinator.end_request()

    async def test_a_non_retryable_error(self, rendered):
        error = _backend_error(retryable=False)
        with pytest.raises(BackendError):
            await _Client().call_external("op", AsyncMock(side_effect=error))
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))

    @pytest.mark.parametrize(
        "error",
        [
            _backend_error(),
            _backend_error(LLMException, status_code=429),  # throttled (#2793)
        ],
        ids=["retryable", "throttled-429"],
    )
    async def test_a_retried_error_that_fails_at_last(self, rendered, error):
        call = AsyncMock(side_effect=error)
        with pytest.raises(type(error)):
            await _Client().call_external("op", call, retries=1, retry_delay=0)
        assert call.await_count == 2
        chain = f"{type(error).__name__} <- ConnectionError"
        _named(rendered, chain, _old_structlog(error))

    @pytest.mark.parametrize("hook", ["validate_response", "transform_response"])
    async def test_a_raising_validator_or_transformer(self, rendered, hook):
        error = _backend_error()
        client = _Client()
        try:
            await client.call_external(
                "op",
                AsyncMock(return_value="ok"),
                **{hook: MagicMock(side_effect=error)},
            )
        except RuntimeError as raised:
            assert SENTINEL not in str(raised)
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))

    def test_the_sync_path(self, rendered):
        error = _backend_error()
        with pytest.raises(BackendError):
            _Client().call_external_sync(
                "op", MagicMock(side_effect=error), retries=1, retry_delay=0
            )
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))

    async def test_each_unified_emitter(self, rendered):
        # A layer per emitter: in a request, only a layer's first error logs.
        def logger(layer):
            return get_unified_logger(f"faultmaven.test.{layer}", layer)

        error = _backend_error()
        with pytest.raises(BackendError):
            async with logger("service").operation("async_op"):
                raise error
        with pytest.raises(BackendError):
            with logger("core").operation_sync("sync_op"):
                raise error
        logger("infrastructure").error("err", error=error)
        logger("api").critical("crit", error=error)
        output = rendered.getvalue()
        for event in ("async_op", "sync_op", "err", "crit"):
            assert event in output
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))


class TestFileStorageService:
    async def test_a_backend_failure_is_logged_and_raised_by_class(self, rendered):
        error = _backend_error()
        backend = MagicMock()
        backend.get_storage_type.return_value.value = "local"
        backend.retrieve_file = AsyncMock(side_effect=error)

        with pytest.raises(ServiceError) as raised:
            await FileStorageService(backend=backend).retrieve_file("evidence/a.log")

        assert (
            str(raised.value)
            == "Failed to retrieve file: BackendError <- ConnectionError"
        )
        assert raised.value.__cause__ is error  # the chain is unchanged
        output = rendered.getvalue()
        assert "Traceback" not in output
        assert "test_log_lines_name_exception_classes_2844.py" in output  # frames
        _named(rendered, "retrieve_file failed: BackendError", _old_structlog(error))

    async def test_a_failed_delete_names_the_class(self, rendered):
        error = _backend_error()
        backend = MagicMock()
        backend.get_storage_type.return_value.value = "local"
        backend.delete_file = AsyncMock(side_effect=error)

        with pytest.raises(ServiceError) as raised:
            await FileStorageService(backend=backend).delete_file("evidence/a.log")

        assert SENTINEL not in str(raised.value)
        assert raised.value.__context__ is error  # the chain is unchanged
        _named(rendered, "delete_file failed: BackendError", _old_structlog(error))


class TestClaimsAndLeases:
    @pytest.fixture(autouse=True)
    def _fresh_once_per_reason(self, monkeypatch):
        monkeypatch.setattr(inflight, "_REPORTED", set())
        monkeypatch.setattr(owned_claim, "_REPORTED", set())

    def test_the_once_per_reason_lines(self, rendered):
        error = _backend_error()
        inflight._unguarded("store_error", error)
        inflight._tombstone_unread("store_error", error)
        for line in ("lease unavailable", "tombstone unreadable"):
            assert f"{line} (store_error: BackendError <- " in rendered.getvalue()
        _named(rendered, "BackendError <- ConnectionError", _old_stdlib(error))

    async def test_take_claim_with_the_store_failing(self, rendered):
        error = _backend_error()
        redis = MagicMock(set=AsyncMock(side_effect=error))
        claim = await owned_claim.take_claim(
            redis, "claim", ttl_seconds=5, purpose="keyed turns", backstop="the TTL"
        )
        assert claim is None
        _named(
            rendered,
            "unavailable (BackendError <- ConnectionError)",
            _old_stdlib(error),
        )

    @pytest.mark.parametrize(
        "step", ["renew", "rewrite", "rewrite-no-error", "release"]
    )
    async def test_a_lease_whose_store_fails(self, rendered, monkeypatch, step):
        monkeypatch.setattr(inflight, "LEASE_RENEW_SECONDS", 0)
        error = _backend_error()
        stop = asyncio.CancelledError()
        redis = MagicMock()
        lease = inflight._CaseLease(("ent", "case"), redis, asyncio.get_running_loop())
        if step == "release":
            redis.zrem = AsyncMock(side_effect=error)
            await lease.give_up()
        else:
            redis.eval = AsyncMock(
                side_effect={
                    "renew": [error, stop],
                    "rewrite": [0, error, stop],
                    "rewrite-no-error": [0, stop],
                }[step]
            )
            if step == "rewrite-no-error":
                lease.write = AsyncMock(return_value=False)
            with pytest.raises(asyncio.CancelledError):
                await lease._renew()
        if step == "rewrite-no-error":
            assert "Could not rewrite" in rendered.getvalue()
            assert "no error recorded" in rendered.getvalue()
            return
        _named(rendered, "BackendError <- ConnectionError", _old_stdlib(error))


# The redactor's matrix (#2844 A6): (input line, the value that must go).
POSITIVES = [
    ("headers: {'date': 'x', 'set-cookie': 'AWSALB=VALUE; Path=/'}", "VALUE"),
    ("Headers({'authorization': 'Bearer VALUE', 'cookie': 'sid=x'})", "VALUE"),
    ("Headers({'authorization': 'Bearer x', 'cookie': 'sid=VALUE'})", "VALUE"),
    ("HTTP/1.1 401\r\nSet-Cookie: sid=VALUE; HttpOnly\r\nX: kept", "VALUE"),
    ('failed: {"Authorization": "Bearer t\\\\VALUE", "x": 1}\nnext', "VALUE"),
    ("authorization=Basic VALUE retry=3", "VALUE"),
    ("{'Authorization': b'Bearer VALUE'}", "VALUE"),
    ("[(b'authorization', b'Bearer VALUE')]", "VALUE"),
    ("('cookie', 'sid=VALUE')", "VALUE"),
    ('Set-Cookie: sid="x"; tok=VALUE', "VALUE"),
    ("{'cookie': \"it's VALUE\", 'x': 1}", "VALUE"),
    ("Proxy-Authorization: Basic VALUE", "VALUE"),
]
NEGATIVES = [
    "Authorization failed for user 42: missing scope cases:write",
    "user cleared the cookie banner; retrying",
    "exchanged authorization_code=abc for a token",
    "kubectl: error: Unauthorized; authorization mode is RBAC",
    "the request lacked a Cookie header",
]


class TestCredentialHeaders:
    @pytest.mark.parametrize("line, value", POSITIVES)
    def test_each_value_is_blanked_in_every_renderer(self, rendered, line, value):
        get_logger("faultmaven.p").error(line, field=line)
        output = rendered.getvalue()
        assert value not in output
        assert output.count("<redacted>") >= 2  # the message and the field
        for row in output.splitlines():
            if row.startswith('{"'):
                json.loads(row)

    def test_the_formatter_and_the_bytes_form(self):
        for line, value in POSITIVES:
            record = logging.LogRecord(
                "job", logging.ERROR, __file__, 1, line, (), None
            )
            assert value not in RedactingFormatter("%(message)s").format(record)
            assert redact_urls_bytes(line.encode()) == redact_urls(line).encode()

    @pytest.mark.parametrize("line", NEGATIVES)
    def test_prose_using_the_words_is_unchanged(self, rendered, line):
        get_logger("faultmaven.p").info(line)
        assert line in rendered.getvalue()
        assert redact_urls(line) == line

    def test_later_fields_and_lines_are_kept(self):
        assert redact_urls("authorization=rbac case_id=c1") == (
            "authorization=<redacted> case_id=c1"
        )
        assert redact_urls("Set-Cookie: sid=S\r\nX: kept").endswith("\r\nX: kept")
        coloured = "\x1b[36mauthorization\x1b[0m=\x1b[35mtokS\x1b[0m \x1b[36mn\x1b[0m=1"
        assert redact_urls(coloured) == coloured.replace("tokS", "<redacted>")

    @pytest.mark.parametrize("tail", ["\\", "\\\\", "'", '\\"', "\\n"])
    def test_a_json_line_stays_valid(self, tail):
        for message in (f"cookie: abc{tail}", f"{{'cookie': 'abc{tail}'}}"):
            line = json.dumps({"event": message, "cookie": f"v{tail}", "n": 5})
            redacted = redact_urls(line)
            assert "abc" not in redacted
            assert json.loads(redacted)["n"] == 5

    def test_a_url_and_a_cookie_on_one_line(self):
        assert redact_urls("GET https://h/p?key=K failed; cookie=S retry") == (
            "GET https://h/p?<redacted> failed; cookie=<redacted> retry"
        )
