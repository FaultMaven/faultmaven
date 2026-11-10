"""The K0 switch (#2669, #2616 K0): no exception text crosses ``opik_sdk.track``,
and ``OPIK_CAPTURE_CONTENT=true`` stops every process at ``configure``.

The guard is driven through a stand-in SDK whose ``track`` records what its
wrapper saw, so it runs with or without opik installed. The real SDK on the
wire is ``test_trace_erasure_loopback_2102.py::TestNoContentByDefault``.
"""

import asyncio
import gc
import inspect
import sys
import types
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from faultmaven.infrastructure.observability import opik_sdk

pytestmark = pytest.mark.unit

SECRET = "the provider's response body, quoting the case"


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in ``opik``: its ``track`` records each outcome its wrapper saw,
    and its span writer records each write."""
    seen: list = []
    spans: list = []
    options: list = []
    #: Set to an exception the wrapper raises after the call, as a broken SDK.
    broken = SimpleNamespace(error=None)

    def track(**given):
        options.append(given)

        def decorate(func):
            if asyncio.iscoroutinefunction(func):

                async def wrapper(*args, **kwargs):
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as e:
                        seen.append(("raised", e))
                        raise
                    seen.append(("returned", result))
                    if broken.error:
                        raise broken.error
                    return result

            else:

                def wrapper(*args, **kwargs):
                    try:
                        result = func(*args, **kwargs)
                    except BaseException as e:
                        seen.append(("raised", e))
                        raise
                    seen.append(("returned", result))
                    if broken.error:
                        raise broken.error
                    return result

            return wrapper

        return decorate

    fake = types.ModuleType("opik")
    fake.track = track
    fake.opik_context = SimpleNamespace(
        update_current_span=lambda **fields: spans.append(fields)
    )
    monkeypatch.setitem(sys.modules, "opik", fake)
    return SimpleNamespace(
        seen=seen, spans=spans, options=options, broken=broken, module=fake
    )


def _sync_failing(error):
    def call():
        raise error

    return call


def _async_failing(error):
    async def call():
        await asyncio.sleep(0)
        raise error

    return call


async def _invoke(tracked):
    result = tracked()
    return await result if asyncio.iscoroutine(result) else result


class TestTheGuard:
    @pytest.mark.parametrize("build", [_sync_failing, _async_failing])
    async def test_the_caller_gets_the_exception_and_the_sdk_a_return(self, sdk, build):
        error = ValueError(SECRET)
        tracked = opik_sdk.track(name="probe")(build(error))

        with pytest.raises(ValueError) as raised:
            await _invoke(tracked)

        assert raised.value is error
        assert sdk.seen == [("returned", None)]
        assert sdk.spans == [{"metadata": {"error_class": "ValueError"}}]

    @pytest.mark.parametrize("build", [_sync_failing, _async_failing])
    async def test_a_span_write_that_raises_keeps_the_original(self, sdk, build):
        """``update_current_span`` raises ``OpikException`` with no span."""

        def no_span(**_fields):
            raise RuntimeError("There is no span in the context.")

        sdk.module.opik_context.update_current_span = no_span
        error = KeyError(SECRET)
        tracked = opik_sdk.track(name="probe")(build(error))

        with pytest.raises(KeyError) as raised:
            await _invoke(tracked)

        assert raised.value is error

    async def test_a_success_returns_its_value(self, sdk):
        async def answer():
            return "the answer"

        assert await opik_sdk.track(name="probe")(answer)() == "the answer"
        assert sdk.seen == [("returned", "the answer")] and sdk.spans == []

    async def test_a_cancellation_passes_through(self, sdk):
        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await opik_sdk.track(name="probe")(cancelled)()

        assert [kind for kind, _ in sdk.seen] == ["raised"] and sdk.spans == []

    def test_generators_are_refused(self, sdk):
        def generator():
            yield 1

        async def async_generator():
            yield 1

        for func in (generator, async_generator):
            with pytest.raises(TypeError, match="does not trace generators"):
                opik_sdk.track(name="probe")(func)

    def test_content_options_are_forced_off_and_trace_shapes_refused(self, sdk):
        """The SDK's defaults record a call's arguments and return value."""
        opik_sdk.track(name="probe", capture_input=True)(lambda: None)

        assert sdk.options == [
            {"name": "probe", "capture_input": False, "capture_output": False}
        ]
        for option in ({"create_duplicate_root_span": False}, {"entrypoint": True}):
            with pytest.raises(TypeError, match="not traced while content"):
                opik_sdk.track(name="probe", **option)

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    async def test_a_failed_call_is_freed_without_the_cyclic_collector(
        self, sdk, is_async
    ):
        """The held exception must not form a cycle with the frame holding the
        call's arguments (a prompt), or they wait for the cyclic GC."""

        class Prompt:
            pass

        def call(prompt):
            raise ValueError(SECRET)

        async def async_call(prompt):
            raise ValueError(SECRET)

        tracked = opik_sdk.track(name="probe")(async_call if is_async else call)
        prompt = Prompt()
        freed = weakref.ref(prompt)
        gc.disable()
        try:
            try:
                result = tracked(prompt)
                if is_async:
                    await result
            except ValueError:
                pass
            del prompt
            assert freed() is None
        finally:
            gc.enable()

    @pytest.mark.parametrize("build", [_sync_failing, _async_failing])
    async def test_an_sdk_failure_after_the_call_keeps_the_original(self, sdk, build):
        sdk.broken.error = RuntimeError("the SDK's own failure")
        error = ValueError(SECRET)

        with pytest.raises(ValueError) as raised:
            await _invoke(opik_sdk.track(name="probe")(build(error)))

        assert raised.value is error
        assert raised.value.__context__ is sdk.broken.error

    async def test_concurrent_calls_each_get_their_own(self, sdk):
        """The slot is per call: interleaved failures do not cross."""
        release = asyncio.Event()

        async def failing(error):
            await release.wait()
            raise error

        tracked = opik_sdk.track(name="probe")(failing)
        errors = [ValueError("first"), ValueError("second")]
        calls = [asyncio.ensure_future(tracked(error)) for error in errors]
        await asyncio.sleep(0)
        release.set()

        outcomes = await asyncio.gather(*calls, return_exceptions=True)

        assert outcomes[0] is errors[0] and outcomes[1] is errors[1]


def _observability(capture, enabled=True):
    return SimpleNamespace(
        opik_capture_content=capture,
        opik_enabled=enabled,
        opik_use_local=True,
        opik_url_override=None,
        opik_local_url="http://localhost:5173",
        opik_project_name="faultmaven",
        opik_api_key=None,
        comet_workspace=None,
    )


def _refused_settings():
    return SimpleNamespace(observability=_observability(True))


class TestTheRefusal:
    def test_the_reason_names_the_knob_and_the_missing_bound(self):
        reason = opik_sdk.content_capture_refusal(_observability(True))

        assert "OPIK_CAPTURE_CONTENT" in reason and "retention bound" in reason
        assert opik_sdk.content_capture_refusal(_observability(False)) is None

    def test_the_setting_defaults_to_off(self, monkeypatch):
        from faultmaven.config.settings import ObservabilitySettings

        monkeypatch.delenv("OPIK_CAPTURE_CONTENT", raising=False)
        assert ObservabilitySettings().opik_capture_content is False
        monkeypatch.setenv("OPIK_CAPTURE_CONTENT", "true")
        assert ObservabilitySettings().opik_capture_content is True

    @pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
    def test_configure_refuses_before_it_touches_anything(self, monkeypatch, enabled):
        before = opik_sdk.TraceBackend()
        monkeypatch.setattr(opik_sdk, "_record", before)
        settings = SimpleNamespace(observability=_observability(True, enabled))

        with patch.object(opik_sdk, "_configure") as applied:
            with pytest.raises(
                opik_sdk.TraceContentCaptureRefused, match="retention bound"
            ):
                opik_sdk.configure(settings)

        applied.assert_not_called()
        assert opik_sdk.backend() is before


class TestEveryEntrypointStops:
    """The lifespan, every ``fm-*`` command and every job configure the SDK
    before composition, so each stops there."""

    async def test_the_api_lifespan(self):
        from faultmaven.bootstrap.lifespan import lifespan

        composed = AsyncMock()
        with (
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=_refused_settings(),
            ),
            patch("faultmaven.bootstrap.configuration_gates.validate_configuration"),
            patch("faultmaven.bootstrap.lifespan.compose_application", composed),
            pytest.raises(opik_sdk.TraceContentCaptureRefused, match="retention"),
        ):
            async with lifespan(FastAPI()):
                pytest.fail("the lifespan must not start")

        composed.assert_not_awaited()

    @pytest.mark.parametrize("runner", ["run_with_container", "run_command"])
    def test_an_fm_command(self, runner):
        """Both runners, and the command closed unstarted: no never-awaited
        warning."""
        from faultmaven.cli import _container

        async def command():
            return 0

        main = command()
        with (
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=_refused_settings(),
            ),
            pytest.raises(opik_sdk.TraceContentCaptureRefused, match="retention"),
        ):
            getattr(_container, runner)(main)
        assert inspect.getcoroutinestate(main) == inspect.CORO_CLOSED

    async def test_a_job(self, monkeypatch):
        from faultmaven.jobs.run import run_job

        job = types.ModuleType("tests_fake_job_2669")
        job.run = AsyncMock()
        monkeypatch.setitem(sys.modules, job.__name__, job)
        container = SimpleNamespace(initialize=AsyncMock(), shutdown=AsyncMock())
        settings = _refused_settings()
        settings.database = SimpleNamespace(
            database_url="sqlite+aiosqlite:///./data/faultmaven.db"
        )
        with (
            patch("faultmaven.config.settings.get_settings", return_value=settings),
            patch("faultmaven.container.container", container),
            patch(
                "faultmaven.config.deployment_coherence.validate_deployment_coherence"
            ),
            patch(
                "faultmaven.providers.tenancy.factory.requested_tenant_provider",
                return_value="single",
            ),
            patch.dict("faultmaven.jobs.run.AVAILABLE_JOBS", {"fake": job.__name__}),
            pytest.raises(opik_sdk.TraceContentCaptureRefused, match="retention"),
        ):
            await run_job("fake")

        container.initialize.assert_not_awaited()
        job.run.assert_not_awaited()
