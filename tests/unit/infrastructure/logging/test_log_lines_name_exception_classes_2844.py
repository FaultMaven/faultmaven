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
from faultmaven.exceptions import LLMException, ServiceError, ValidationException
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
from faultmaven.services.base import BaseService

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


def _json_lines(rendered):
    return [json.loads(line) for line in rendered.getvalue().splitlines()]


class _Client(BaseExternalClient):
    def __init__(self):
        super().__init__("test_client", "TestService", enable_circuit_breaker=False)


@pytest.fixture(params=[False, True], ids=["no-context", "request"])
def request_context(request):
    coordinator = LoggingCoordinator()
    if request.param:
        coordinator.start_request()
    yield
    if request.param:
        coordinator.end_request()


@pytest.mark.usefixtures("request_context")
class TestCallExternal:
    async def test_a_non_retryable_error(self, rendered):
        error = _backend_error(retryable=False)
        with pytest.raises(BackendError):
            await _Client().call_external("op", AsyncMock(side_effect=error))
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))

    async def test_a_providers_codes_stay_as_fields(self, rendered):
        """The fail-fast line and the attempt keep what the text used to carry."""
        error = _backend_error(LLMException, status_code=400)
        with pytest.raises(LLMException):
            await _Client().call_external("op", AsyncMock(side_effect=error))
        if rendered.getvalue().startswith("{"):
            lines = _json_lines(rendered)
            fail_fast = [e for e in lines if e["event"].startswith("Non-retryable")]
            assert fail_fast[0]["status_code"] == 400
            failed = [e for e in lines if e.get("event_type") == "operation_error"]
            assert failed[0]["error_attempt_0"]["status_code"] == 400
        _named(rendered, "LLMException <- ConnectionError", _old_structlog(error))

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
        if rendered.getvalue().startswith("{"):
            (retry,) = [e for e in _json_lines(rendered) if "will retry" in e["event"]]
            assert (retry["error_type"], retry["error_chain"]) == (
                type(error).__name__,
                chain,
            )
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

    @pytest.mark.parametrize(
        "case", ["retried", "non-retryable", "validate_response", "transform_response"]
    )
    def test_the_sync_path(self, rendered, case):
        error = _backend_error(retryable=False if case == "non-retryable" else None)
        call, hooks = MagicMock(side_effect=error), {}
        if case.endswith("_response"):
            call, hooks = MagicMock(return_value="ok"), {
                case: MagicMock(side_effect=error)
            }
        try:
            _Client().call_external_sync("op", call, retries=1, retry_delay=0, **hooks)
        except (BackendError, RuntimeError) as raised:
            assert SENTINEL not in str(raised) or raised is error
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


class _Service(BaseService):
    pass


@pytest.mark.usefixtures("request_context")
@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
class TestExecuteOperation:
    async def _run(self, sync, operation, **hooks):
        service = _Service("test_service")
        with pytest.raises(Exception):
            if sync:
                service.execute_operation_sync("op", operation, **hooks)
            else:
                await service.execute_operation("op", operation, **hooks)

    @pytest.mark.parametrize("cls", [BackendError, ServiceError])
    async def test_a_failed_operation(self, rendered, sync, cls):
        error = _backend_error(cls)
        await self._run(sync, MagicMock(side_effect=error))
        chain = f"{cls.__name__} <- ConnectionError"
        _named(rendered, chain, _old_structlog(error))

    @pytest.mark.parametrize(
        "cls", [BackendError, ValidationException, FileNotFoundError]
    )
    async def test_a_failed_validation(self, rendered, sync, cls):
        error = _backend_error(cls)
        hook = {"validate_inputs": MagicMock(side_effect=error)}
        await self._run(sync, MagicMock(return_value="ok"), **hook)
        chain = f"{cls.__name__} <- ConnectionError"
        _named(rendered, chain, _old_structlog(error))


def _backend(**methods):
    backend = MagicMock(**methods)
    backend.get_storage_type.return_value.value = "local"
    return backend


class TestFileStorageService:
    async def test_store_and_sidecar_failures_name_the_class(self, rendered):
        error = _backend_error()
        service = FileStorageService(
            backend=_backend(
                store_file=AsyncMock(side_effect=error),
                retrieve_file=AsyncMock(side_effect=error),
            )
        )
        with pytest.raises(ServiceError) as stored:
            await service.store_file(b"x", "a.log", "ent", "case", "text/plain")
        with pytest.raises(ServiceError) as read:
            await service.read_sidecar("evidence/a.log")

        chain = "BackendError <- ConnectionError"
        assert str(stored.value) == f"Failed to store file: {chain}"
        assert str(read.value) == f"Failed to read sidecar: {chain}"
        _named(rendered, f"read_sidecar failed: {chain}", _old_structlog(error))

    async def test_a_logged_chain_keeps_its_implicit_links(self, rendered):
        """``mark_linked`` logs a ``ServiceError`` raised while handling the
        backend's: the frames name both, as a traceback would, without text."""
        error = _backend_error()
        backend = _backend(retrieve_file=AsyncMock(side_effect=error))
        assert (
            await FileStorageService(backend=backend).mark_linked("ev/a.log") is False
        )
        output = rendered.getvalue()
        at = output.index("mark_linked failed")
        line = output[output.rfind("\n", 0, at) + 1 :]  # and the console's stack
        for link in ("ServiceError:", "BackendError:", "ConnectionError:"):
            assert link in line
        _named(rendered, "mark_linked failed: ServiceError", _old_structlog(error))

    async def test_a_backend_failure_is_logged_and_raised_by_class(self, rendered):
        error = _backend_error()
        backend = _backend(retrieve_file=AsyncMock(side_effect=error))

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
        backend = _backend(delete_file=AsyncMock(side_effect=error))

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
    # The review of #2864: a repr's escaped quotes, cookie pairs, any auth
    # scheme and its parameters, and a bracketed value.
    ("E('b: {\"d\": 1}, h: {\\'set-cookie\\': \\'a=VALUE; P=/\\'}')", "VALUE"),
    ("cookie=sid=x; tok=VALUE next=1", "VALUE"),
    ("authorization=Splunk VALUE next=1", "VALUE"),
    ("authorization=ApiKey VALUE", "VALUE"),
    ("authorization=AWS4-HMAC-SHA256 Credential=A/x, Signature=VALUE", "VALUE"),
    ('authorization=Digest username="u", realm="r", response="VALUE"', "VALUE"),
    ("{'set-cookie': ['a=VALUE', 'b=x'], 'x': 1}", "VALUE"),
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

    @pytest.mark.parametrize(
        "fields",
        [
            {"headers": [("authorization", "Bearer VALUE"), ("cookie", "sid=x")]},
            {"headers": [(b"authorization", b"Bearer VALUE")]},
            {"headers": {"set-cookie": ["a=VALUE", "b=x"]}},
            {"headers": {"cookie": {"sid": "VALUE"}}},
            {"cookie": {"sid": "VALUE"}},
        ],
    )
    def test_a_structured_value_is_blanked_and_later_fields_kept(
        self, rendered, fields
    ):
        get_logger("faultmaven.p").error("headers", **fields, later="kept")
        output = rendered.getvalue()
        assert "VALUE" not in output
        assert "kept" in output
        if output.startswith("{"):
            assert json.loads(output)["later"] == "kept"

    @pytest.mark.parametrize(
        "message",
        [
            'authorization="Bearer VALUE"',
            'cookie="sid=VALUE"',
            "{'Authorization': \"it's VALUE\"}",
            "Set-Cookie: sid=VALUE\nthe next line is kept",
        ],
    )
    def test_the_jobs_json_template_is_redacted_as_text(self, monkeypatch, message):
        """The jobs' JSON template interpolates the raw message, so the line is
        not JSON and is read as text."""
        from faultmaven.config import settings
        from faultmaven.jobs.run import _job_log_format

        json_setting = types.SimpleNamespace(log_output_format="json")
        monkeypatch.setattr(
            settings,
            "get_settings",
            lambda: types.SimpleNamespace(logging=json_setting),
        )
        record = logging.LogRecord("job", logging.ERROR, __file__, 1, message, (), None)
        output = RedactingFormatter(_job_log_format()).format(record)
        assert output.startswith('{"timestamp"')
        assert "VALUE" not in output
        assert "<redacted>" in output
        assert "the next line is kept" in output or "\n" not in message

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
