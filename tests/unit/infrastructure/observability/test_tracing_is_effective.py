"""``tracing_is_effective`` reports what ``init_opik_tracing`` actually did.

The question ``/admin/config/status`` asks, pinned HERE against the real
``init_opik_tracing`` rather than only through the endpoint. The endpoint's own
tests patch ``_tracing_configured`` directly — which is right for testing the
endpoint, and would keep passing if initialisation stopped recording the flag
at all. These drive the function and let it set the flag itself.

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

from faultmaven.infrastructure.observability import tracing


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
    exactly as ``_enable_sdk_tracing`` relies on.
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
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(tracing, "_tracing_configured", False)
    monkeypatch.setattr(
        tracing, "_track_disable_prior_value", tracing._TRACK_DISABLE_UNRECORDED
    )
    monkeypatch.delenv("OPIK_TRACK_DISABLE", raising=False)
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
    tracing.init_opik_tracing(settings=_settings(opik_enabled=True))

    assert tracing._tracing_configured is False
    assert tracing.tracing_is_effective() is False


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
    tracing.init_opik_tracing(settings=_settings(opik_enabled=True, **backend))

    assert tracing._tracing_configured is True
    assert tracing.tracing_is_effective() is True


@pytest.mark.unit
def test_disabled_is_not_effective(opik_stand_in):
    tracing.init_opik_tracing(settings=_settings(opik_enabled=False))

    assert tracing.tracing_is_effective() is False


@pytest.mark.unit
def test_the_operator_kill_switch_is_visible(opik_stand_in, monkeypatch):
    """``OPIK_TRACK_DISABLE=true`` suppresses spans while leaving a backend
    configured — a documented way to stop tracing without unconfiguring it.

    Initialisation therefore SUCCEEDS and records its flag, and only the SDK's
    live switch shows that nothing is recorded. This is why the answer is not
    the recorded flag alone.
    """
    monkeypatch.setenv("OPIK_TRACK_DISABLE", "true")

    tracing.init_opik_tracing(
        settings=_settings(opik_enabled=True, opik_use_local=True)
    )

    assert tracing._tracing_configured is True
    assert opik_stand_in["active"] is False
    assert tracing.tracing_is_effective() is False


@pytest.mark.unit
def test_a_failed_initialisation_is_not_effective(opik_stand_in):
    """A configuration exception leaves the call sites live and recording
    nothing, so it must not report itself as tracing.

    ``opik_local_url`` is made to raise on use; initialisation catches it and
    logs "Continuing without tracing".
    """
    settings = _settings(opik_enabled=True, opik_use_local=True)
    settings.observability.opik_local_url = MagicMock(
        rstrip=MagicMock(side_effect=RuntimeError("boom"))
    )

    tracing.init_opik_tracing(settings=settings)

    assert tracing._tracing_configured is False
    assert tracing.tracing_is_effective() is False


@pytest.mark.unit
def test_a_previous_success_is_not_inherited(opik_stand_in):
    """Re-initialising into a broken configuration must clear the flag.

    The reset happens at entry precisely so a second call cannot report the
    first call's backend.
    """
    tracing.init_opik_tracing(
        settings=_settings(opik_enabled=True, opik_use_local=True)
    )
    assert tracing.tracing_is_effective() is True

    tracing.init_opik_tracing(settings=_settings(opik_enabled=True))

    assert tracing._tracing_configured is False
    assert tracing.tracing_is_effective() is False


@pytest.mark.unit
def test_an_absent_sdk_is_not_effective(opik_stand_in, monkeypatch):
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", False)

    tracing.init_opik_tracing(
        settings=_settings(opik_enabled=True, opik_use_local=True)
    )

    assert tracing.tracing_is_effective() is False
