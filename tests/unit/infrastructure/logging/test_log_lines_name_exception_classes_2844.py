"""A log line names a third party's exception by class chain, never its text (#2844).

A client's or a backend's exception text can carry its output: a cookie, a
credential, a response body. Every site the ruling lists, and every emitter of
``UnifiedLogger``, names the class chain instead, and so do the messages
``execute_operation`` raises. Each logging case asserts on the line the real
``configure_structlog`` chain renders (JSON, and console with colour forced),
and shows the capture live: the same exception logged the old way does reach
it.
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
from faultmaven.exceptions import (
    LLM_CONFIG_ERROR,
    LLMException,
    ServiceError,
    ValidationException,
    walk_cause_chain,
)
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
from faultmaven.models.exceptions import LLMProviderError
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.services.base import BaseService

pytestmark = [pytest.mark.unit, pytest.mark.security]

SENTINEL = "SECRET-SENTINEL"


class BackendError(Exception):
    """A third party's exception, its text the backend's output."""


class FalsyError(BackendError):
    """An exception whose truth value is False."""

    def __bool__(self):
        return False


class UnaskableError(BackendError):
    """An exception whose truth value raises, carrying the sentinel."""

    def __bool__(self):
        raise RuntimeError(SENTINEL)


def _backend_error(cls=BackendError, retryable=None, **kwargs):
    """A chained backend error whose text carries the sentinel and, as a
    realistic carrier would (an HTTP client's error quoting its response
    headers), a ``set-cookie`` value."""
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


async def _call(sync, error):
    if sync:
        return _Client().call_external_sync("op", MagicMock(side_effect=error))
    return await _Client().call_external("op", AsyncMock(side_effect=error))


def _fail_fast_and_attempt(rendered):
    lines = _json_lines(rendered)
    (fail_fast,) = [e for e in lines if e["event"].startswith("Non-retryable")]
    (failed,) = [e for e in lines if e.get("event_type") == "operation_error"]
    return fail_fast, failed["error_attempt_0"]


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

    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_a_providers_codes_stay_as_fields(self, rendered, sync):
        """The fail-fast line and the attempt keep what the text used to carry."""
        error = _backend_error(LLMException, status_code=400, error_code="E_SHAPE")
        with pytest.raises(LLMException):
            if sync:
                _Client().call_external_sync("op", MagicMock(side_effect=error))
            else:
                await _Client().call_external("op", AsyncMock(side_effect=error))
        if rendered.getvalue().startswith("{"):
            codes = {"status_code": 400, "error_code": "E_SHAPE"}
            lines = _json_lines(rendered)
            (fail_fast,) = [e for e in lines if e["event"].startswith("Non-retryable")]
            assert codes.items() <= fail_fast.items()
            (failed,) = [e for e in lines if e.get("event_type") == "operation_error"]
            assert codes.items() <= failed["error_attempt_0"].items()
        _named(rendered, "LLMException <- ConnectionError", _old_structlog(error))

    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_a_models_error_keeps_its_code(self, rendered, sync):
        """The other first-party hierarchy: an ``LLMProviderError``'s code is one
        the breaker latches."""
        error = _backend_error(
            LLMProviderError, retryable=False, error_code=LLM_CONFIG_ERROR
        )
        with pytest.raises(LLMProviderError):
            await _call(sync, error)
        if rendered.getvalue().startswith("{"):
            fail_fast, attempt = _fail_fast_and_attempt(rendered)
            assert fail_fast["error_code"] == attempt["error_code"] == LLM_CONFIG_ERROR
        _named(rendered, "LLMProviderError <- ConnectionError", _old_structlog(error))

    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_a_third_partys_codes_are_not_read(self, rendered, sync):
        """Attributes of the same names on a third party's exception are its
        own wording, not first-party fields."""
        error = _backend_error(retryable=False)
        error.status_code, error.error_code = 418, SENTINEL
        with pytest.raises(BackendError):
            await _call(sync, error)
        if rendered.getvalue().startswith("{"):
            for line in _fail_fast_and_attempt(rendered):
                assert "status_code" not in line and "error_code" not in line
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
        if rendered.getvalue().startswith("{"):
            (retry,) = [e for e in _json_lines(rendered) if "will retry" in e["event"]]
            assert (retry["error_type"], retry["error_chain"]) == (
                type(error).__name__,
                chain,
            )
        _named(rendered, chain, _old_structlog(error))

    @pytest.mark.parametrize("hook", ["validate_response", "transform_response"])
    async def test_a_raising_validator_or_transformer(self, rendered, hook):
        """A failed validation raises by class; a failed transformation
        returns the untransformed result."""
        error = _backend_error()
        call = _Client().call_external(
            "op", AsyncMock(return_value="ok"), **{hook: MagicMock(side_effect=error)}
        )
        if hook == "validate_response":
            with pytest.raises(RuntimeError) as raised:
                await call
            assert str(raised.value) == (
                "Response validation failed: BackendError <- ConnectionError"
            )
            assert raised.value.__cause__ is error
        else:
            assert await call == "ok"
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

        def run():
            return _Client().call_external_sync(
                "op", call, retries=1, retry_delay=0, **hooks
            )

        if case == "transform_response":
            assert run() == "ok"
        else:
            raising = RuntimeError if case == "validate_response" else BackendError
            with pytest.raises(raising) as raised:
                run()
            assert raised.value is error or SENTINEL not in str(raised.value)
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
        logger("infrastructure").error("the error line", error=error)
        logger("api").critical("the critical line", error=error)
        chain = "BackendError <- ConnectionError"
        rows = rendered.getvalue().splitlines()
        for own in (
            "Operation failed: async_op",
            "Operation failed: sync_op",
            "the error line",
            "the critical line",
        ):
            (row,) = [row for row in rows if own in row]  # the emitter's own line
            assert SENTINEL not in row
            assert chain in row
            if row.startswith("{"):
                assert json.loads(row)["error_message"] == chain
        _named(rendered, chain, _old_structlog(error))

    @pytest.mark.parametrize("cls", [FalsyError, UnaskableError])
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_an_exceptions_truth_value_is_never_read(self, rendered, cls, sync):
        """A falsy exception is still named, and one whose truth value raises
        does not turn a logging call into a raise."""
        error = _backend_error(cls)
        get_unified_logger("faultmaven.test.service", "service").error(
            "the error line", error=error
        )
        get_unified_logger("faultmaven.test.api", "api").critical(
            "the critical line", error=error
        )
        call = (MagicMock if sync else AsyncMock)(side_effect=error)
        with pytest.raises(cls):
            if sync:
                _Client().call_external_sync("op", call, retries=1, retry_delay=0)
            else:
                await _Client().call_external("op", call, retries=1, retry_delay=0)
        chain = f"{cls.__name__} <- ConnectionError"
        rows = rendered.getvalue().splitlines()
        for own in ("the error line", "the critical line"):
            (row,) = [row for row in rows if own in row]
            assert chain in row
        if rows[0].startswith("{"):
            (failed,) = [
                e
                for e in _json_lines(rendered)
                if e.get("event_type") == "operation_error"
            ]
            assert failed["retry_attempt_1"]["previous_error"] == chain
        _named(rendered, chain, _old_structlog(error))

    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_a_retry_line_filters_the_class_name(self, rendered, sync):
        """A class's name is whatever its creator chose: the "will retry" line
        names it only when it is a plain identifier."""
        cls = type(f"{SENTINEL} x", (BackendError,), {})
        error = _backend_error(cls)
        call = (MagicMock if sync else AsyncMock)(side_effect=error)
        with pytest.raises(cls):
            if sync:
                _Client().call_external_sync("op", call, retries=1, retry_delay=0)
            else:
                await _Client().call_external("op", call, retries=1, retry_delay=0)
        (row,) = [
            row for row in rendered.getvalue().splitlines() if "will retry" in row
        ]
        assert SENTINEL not in row
        assert "Exception <- ConnectionError" in row
        if row.startswith("{"):
            assert json.loads(row)["error_type"] == "Exception"


class _Service(BaseService):
    pass


@pytest.mark.usefixtures("request_context")
@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
class TestExecuteOperation:
    async def _run(self, sync, operation, **hooks):
        service = _Service("test_service")
        with pytest.raises(Exception) as raised:
            if sync:
                service.execute_operation_sync("op", operation, **hooks)
            else:
                await service.execute_operation("op", operation, **hooks)
        return raised.value

    @pytest.mark.parametrize("hook", ["operation", "validate_inputs"])
    async def test_a_raise_names_the_class(self, sync, hook):
        """What ``execute_operation`` raises for a third party's failure names
        its class at every link, each keeping the one below as its cause. A
        failed validation's ``ValueError`` is wrapped like any failure."""
        error = OSError(SENTINEL)
        operation, hooks = MagicMock(side_effect=error), {}
        expected = [
            "RuntimeError: Service operation failed: test_service.op: OSError",
            "OSError",
        ]
        if hook == "validate_inputs":
            operation = MagicMock(return_value="ok")
            hooks = {hook: MagicMock(side_effect=error)}
            expected = [
                "RuntimeError: Service operation failed: test_service.op: "
                "ValueError <- OSError",
                "ValueError: Validation failed: OSError",
                "OSError",
            ]
        raised = await self._run(sync, operation, **hooks)
        links = list(walk_cause_chain(raised))
        assert links[-1] is error
        rendered_links = [f"{type(link).__name__}: {link}" for link in links[:-1]]
        assert rendered_links + ["OSError"] == expected

    async def test_a_failed_transformation(self, rendered, sync):
        """The result is returned untransformed; the failure line and the
        completed operation's context name the class."""
        error = _backend_error()
        service = _Service("test_service")
        hooks = {"transform_result": MagicMock(side_effect=error)}
        if sync:
            result = service.execute_operation_sync("op", lambda: "ok", **hooks)
        else:
            result = await service.execute_operation(
                "op", AsyncMock(return_value="ok"), **hooks
            )
        assert result == "ok"
        rows = rendered.getvalue().splitlines()
        for line in ("Result transformation failed", "Operation completed: op"):
            (row,) = [row for row in rows if line in row]
            assert "BackendError <- ConnectionError" in row
        _named(rendered, "BackendError <- ConnectionError", _old_structlog(error))

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

    async def test_a_corrupt_sidecar_names_the_class(self, rendered):
        """The decode error's text quotes the stored bytes; the raise and the
        line name the class instead."""
        backend = _backend(retrieve_file=AsyncMock(return_value=b"\xff"))
        with pytest.raises(ServiceError) as raised:
            await FileStorageService(backend=backend).read_sidecar("evidence/a.log")

        assert str(raised.value) == (
            "Corrupt sidecar for evidence/a.log: UnicodeDecodeError"
        )
        output = rendered.getvalue()
        assert "xff" not in output  # the byte, as its str or its repr shows it
        assert "read_sidecar failed: UnicodeDecodeError" in output
        logging.getLogger("faultmaven.control").warning("%s", raised.value.__context__)
        assert "0xff" in rendered.getvalue()[len(output) :]

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
