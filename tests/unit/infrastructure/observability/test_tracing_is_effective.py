"""``tracing_is_effective`` reports what ``opik_sdk.configure`` actually did.

The question ``/admin/config/status`` asks, pinned HERE against the real
``configure`` rather than only through the endpoint. The endpoint's own tests
patch the record directly — which is right for testing the endpoint, and would
keep passing if configuration stopped recording its outcome at all. These drive
the function and let it write the record itself.

Why this exists (#1234, round 2): the first fix answered ``opik_enabled and
OPIK_AVAILABLE``, mirroring two of initialisation's gates. The third — no
backend URL configured — disables the SDK and returns, so an install WITH the
SDK and ``OPIK_ENABLED=true`` traced nothing while the endpoint reported
tracing on. Mirroring gates is what failed; the flag is recorded where the
decision is made so a new bail-out cannot be missed.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.observability import opik_sdk


@pytest.fixture
def opik_stand_in(monkeypatch):
    """A stand-in ``opik`` plus containment for the module's global state.

    Installed for BOTH environments this suite runs in: this repo's venv has no
    ``opik``, while CI's Test Cloud job installs the ``[cloud]`` extra and does.
    Patching it in means these assertions mean the same thing in both, rather
    than one of them passing by accident of what happens to be installed.

    ``set_tracing_active`` / ``reset_tracing_to_config_default`` mirror the real
    SDK's contract closely enough for the switch to be observable: the kill
    switch turns it off, and the reset re-derives from ``OPIK_TRACK_DISABLE``
    exactly as ``_enable_sdk_tracing`` relies on. ``configure`` writes the
    environment variables the SDK reads; each is put back after the test.
    """
    import os

    state = {"active": True}
    fake = types.ModuleType("opik")
    fake.__file__ = "/stand-in/opik/__init__.py"
    fake.set_tracing_active = lambda v: state.__setitem__("active", bool(v))
    fake.is_tracing_active = lambda: state["active"]
    fake.reset_tracing_to_config_default = lambda: state.__setitem__(
        "active", os.environ.get("OPIK_TRACK_DISABLE") != "true"
    )
    config = types.ModuleType("opik.config")
    config.update_session_config = lambda *a, **k: None
    fake.config = config

    monkeypatch.setitem(sys.modules, "opik", fake)
    monkeypatch.setitem(sys.modules, "opik.config", config)
    monkeypatch.setattr(opik_sdk, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(opik_sdk, "SPAN_TAGGING_AVAILABLE", True)
    monkeypatch.setattr(opik_sdk, "_record", opik_sdk.TraceBackend())
    monkeypatch.setattr(
        opik_sdk, "_track_disable_prior_value", opik_sdk._TRACK_DISABLE_UNRECORDED
    )
    for name in (
        "OPIK_TRACK_DISABLE",
        "OPIK_URL_OVERRIDE",
        "OPIK_PROJECT_NAME",
        "OPIK_WORKSPACE",
        "OPIK_API_KEY",
        "OPIK_CONNECTION_MONITOR_PING_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)
    return state


def _settings(**observability):
    settings = MagicMock()
    obs = settings.observability
    obs.opik_enabled = observability.get("opik_enabled", True)
    obs.opik_use_local = observability.get("opik_use_local", False)
    obs.opik_url_override = observability.get("opik_url_override", None)
    obs.opik_local_url = "http://localhost:5173"
    obs.opik_project_name = "faultmaven"
    obs.opik_api_key = None
    obs.comet_workspace = None
    return settings


@pytest.mark.unit
def test_enabled_without_a_backend_url_is_not_effective(opik_stand_in):
    """#1234 surviving its own first fix.

    ``OPIK_ENABLED=true`` and neither ``OPIK_USE_LOCAL`` nor
    ``OPIK_URL_OVERRIDE`` — the shape an operator reaches by flipping the
    documented knob and nothing else — logs "Tracing will be disabled" and
    returns. The SDK is present here, so a predicate of ``opik_enabled and
    OPIK_AVAILABLE`` reports True.
    """
    opik_sdk.configure(_settings(opik_enabled=True))

    assert opik_sdk.trace_backend_configured() is False
    assert opik_sdk.tracing_is_effective() is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "backend",
    [
        {"opik_use_local": True},
        {"opik_url_override": "https://opik.example/api"},
    ],
    ids=["use-local", "url-override"],
)
def test_a_configured_backend_is_effective(opik_stand_in, backend):
    """The positive control, and both ways of supplying a backend.

    Without it every assertion here is satisfied by a function that returns
    False unconditionally.
    """
    opik_sdk.configure(_settings(opik_enabled=True, **backend))

    assert opik_sdk.trace_backend_configured() is True
    assert opik_sdk.tracing_is_effective() is True


@pytest.mark.unit
def test_disabled_is_not_effective(opik_stand_in):
    opik_sdk.configure(_settings(opik_enabled=False))

    assert opik_sdk.tracing_is_effective() is False


@pytest.mark.unit
def test_the_operator_kill_switch_is_visible(opik_stand_in, monkeypatch):
    """``OPIK_TRACK_DISABLE=true`` suppresses spans while leaving a backend
    configured — a documented way to stop tracing without unconfiguring it.

    Initialisation therefore SUCCEEDS and records its flag, and only the SDK's
    live switch shows that nothing is recorded. This is why the answer is not
    the recorded flag alone.
    """
    monkeypatch.setenv("OPIK_TRACK_DISABLE", "true")

    opik_sdk.configure(_settings(opik_enabled=True, opik_use_local=True))

    assert opik_sdk.trace_backend_configured() is True
    assert opik_stand_in["active"] is False
    assert opik_sdk.tracing_is_effective() is False


@pytest.mark.unit
def test_a_failed_initialisation_is_not_effective(opik_stand_in):
    """A configuration exception leaves the call sites live and recording
    nothing, so it must not report itself as tracing.

    ``opik_local_url`` is made to raise on use; ``configure`` catches it, logs
    "Continuing without tracing" and records the class as the reason, so a
    case delete refuses with it rather than skipping the traces.
    """
    settings = _settings(opik_enabled=True, opik_use_local=True)
    settings.observability.opik_local_url = MagicMock(
        rstrip=MagicMock(side_effect=RuntimeError("boom"))
    )

    record = opik_sdk.configure(settings)

    assert opik_sdk.trace_backend_configured() is False
    assert opik_sdk.tracing_is_effective() is False
    assert record.reason == "configuring the trace backend raised RuntimeError"


@pytest.mark.unit
def test_a_previous_success_is_not_inherited(opik_stand_in):
    """Re-initialising into a broken configuration must clear the flag.

    The reset happens at entry precisely so a second call cannot report the
    first call's backend.
    """
    opik_sdk.configure(_settings(opik_enabled=True, opik_use_local=True))
    assert opik_sdk.tracing_is_effective() is True

    opik_sdk.configure(_settings(opik_enabled=True))

    assert opik_sdk.trace_backend_configured() is False
    assert opik_sdk.tracing_is_effective() is False


@pytest.mark.unit
def test_an_absent_sdk_is_not_effective(opik_stand_in, monkeypatch):
    monkeypatch.setattr(opik_sdk, "OPIK_AVAILABLE", False)
    monkeypatch.setattr(opik_sdk, "SPAN_TAGGING_AVAILABLE", False)

    opik_sdk.configure(_settings(opik_enabled=True, opik_use_local=True))

    assert opik_sdk.tracing_is_effective() is False


@pytest.mark.unit
def test_the_record_resolves_the_project_once(opik_stand_in):
    """One default, the setting's: an empty name is not a second one (#2588)."""
    settings = _settings(opik_enabled=True, opik_use_local=True)

    named = opik_sdk.configure(settings)
    settings.observability.opik_project_name = ""
    unnamed = opik_sdk.configure(settings)

    assert (named.project, unnamed.project) == ("faultmaven", "faultmaven")
    assert named.url == "http://localhost:5173/api/"
    assert named.ping_interval_s == opik_sdk.PING_INTERVAL_S == 999999
    assert opik_sdk.backend() is unnamed


@pytest.mark.unit
@pytest.mark.parametrize(
    "observability, sdk, reason",
    [
        ({"opik_enabled": False}, True, "OPIK_ENABLED is false"),
        ({"opik_enabled": True}, True, "no backend URL is set"),
        ({"opik_use_local": True}, False, "the Opik SDK is not installed"),
    ],
    ids=["disabled", "no-url", "no-sdk"],
)
def test_each_bail_out_records_its_reason(
    opik_stand_in, monkeypatch, observability, sdk, reason
):
    monkeypatch.setattr(opik_sdk, "OPIK_AVAILABLE", sdk)
    monkeypatch.setattr(opik_sdk, "SPAN_TAGGING_AVAILABLE", sdk)

    record = opik_sdk.configure(_settings(**observability))

    assert (record.configured, record.reason, record.ping_interval_s) == (
        False,
        reason,
        None,
    )
