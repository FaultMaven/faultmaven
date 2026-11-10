"""One record decides every trace write, the eraser, the status and the
middleware (#2616 part (c)).

``opik_sdk.configure`` writes the record once per process, before composition.
Here the settings stay the same in both arms (tracing enabled, a backend named)
and only the record flips: each reader follows the record, none reads the
setting. Before #2620 the call sites gated on ``OPIK_ENABLED`` and the
middleware on the SDK's presence, so the "unconfigured" arm traced.

The middleware arm shows only that the branch reads the record. In the API,
``setup_middleware`` runs when ``faultmaven.main`` is imported, before the
lifespan configures, so the branch reads the unconfigured placeholder and
mounts nothing there. Here the record is set before the app is rebuilt.
"""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.api.routes.admin_config import _llm_tracing_is_effective
from faultmaven.container.providers.services import llm_trace_eraser
from faultmaven.infrastructure.llm.router import _opik_track_llm
from faultmaven.infrastructure.observability import opik_sdk
from faultmaven.infrastructure.observability.trace_erasure import (
    LLMTraceEraser,
    UnavailableTraceEraser,
)
from faultmaven.modules.preprocessing.classifier import _opik_track_classifier
from tests.integration._app_rebuild import rebuild_app_under

pytestmark = pytest.mark.integration

SETTINGS = SimpleNamespace(
    observability=SimpleNamespace(
        opik_enabled=True, opik_use_local=True, opik_url_override=None
    )
)


class _OpikMiddleware:
    """An SDK that ships the FastAPI integration (1.11.14 does not)."""

    def __init__(self, app):
        self.app = app


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in SDK, switched on, whose ``track`` marks what it wrapped."""
    fake = types.ModuleType("opik")
    fake.is_tracing_active = lambda: True
    monkeypatch.setitem(sys.modules, "opik", fake)
    monkeypatch.setattr(opik_sdk, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(opik_sdk, "SPAN_TAGGING_AVAILABLE", True)
    monkeypatch.setattr(opik_sdk, "fastapi_middleware", lambda: _OpikMiddleware)

    def track(**_options):
        def decorate(func):
            if asyncio.iscoroutinefunction(func):

                async def traced(*args, **kwargs):
                    return "traced", await func(*args, **kwargs)

            else:

                def traced(*args, **kwargs):
                    return "traced", func(*args, **kwargs)

            return traced

        return decorate

    monkeypatch.setattr(opik_sdk, "track", track)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "not"])
async def test_flipping_the_record_flips_every_reader(sdk, monkeypatch, configured):
    # Content capture on in both arms, so only ``configured`` flips the
    # middleware; without it the middleware stays off (#2616 K0, the next test).
    record = opik_sdk.TraceBackend(
        sdk_available=True,
        enabled=True,
        configured=configured,
        capture_content=True,
        project="fm-project" if configured else None,
        reason=None if configured else "no backend URL is set",
    )
    monkeypatch.setattr(opik_sdk, "_record", record)

    async def routed():
        return "raw"

    classified = _opik_track_classifier(lambda: "raw")
    with patch(
        "faultmaven.bootstrap.middleware._is_test_environment", return_value=False
    ):
        app = rebuild_app_under({"SKIP_SERVICE_CHECKS": "false"})
    eraser = llm_trace_eraser(SETTINGS)

    traced = configured
    expected = ("traced", "raw") if traced else "raw"
    assert await _opik_track_llm("probe")(routed)() == expected
    assert classified() == expected
    assert _llm_tracing_is_effective() is traced
    assert (_OpikMiddleware in [m.cls for m in app.user_middleware]) is traced
    if configured:
        assert isinstance(eraser, LLMTraceEraser)
        assert eraser.project_name == "fm-project"
    else:
        assert isinstance(eraser, UnavailableTraceEraser)
        assert eraser.reason == "no backend URL is set"


@pytest.mark.parametrize(
    "configured, capture",
    [(True, False), (False, True)],
    ids=["configured-capture-off", "capture-on-not-configured"],
)
async def test_the_middleware_needs_both_a_backend_and_content_capture(
    sdk, monkeypatch, configured, capture
):
    """#2616 K0: an SDK middleware could record request data, so it mounts only
    with a configured backend and content capture on, each on its own."""
    record = opik_sdk.TraceBackend(
        sdk_available=True,
        enabled=True,
        configured=configured,
        capture_content=capture,
        reason=None if configured else "no backend URL is set",
    )
    monkeypatch.setattr(opik_sdk, "_record", record)

    with patch(
        "faultmaven.bootstrap.middleware._is_test_environment", return_value=False
    ):
        app = rebuild_app_under({"SKIP_SERVICE_CHECKS": "false"})

    assert _llm_tracing_is_effective() is configured
    assert _OpikMiddleware not in [m.cls for m in app.user_middleware]
