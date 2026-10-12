"""DEEP_ANALYSIS_BACKEND admits ``local`` or ``disabled``, checked at settings load (#1957).

The ``external`` backend (raw evidence uploaded to an operator-supplied URL,
unredacted and outside the LLM-spend census) and the ``basic`` keyword backend
(a duplicate of ``search_file``) were retired. A deployment still naming either
must fail when its settings load, naming the variable and the values it
admits, rather than boot into a configuration the code no longer has.

Pins:
  1. the default is ``local``, and ``local`` / ``disabled`` both load;
  2. ``external``, ``basic`` and an arbitrary value fail at settings load, for
     ``DeepAnalysisSettings`` and for the whole ``FaultMavenSettings``;
  3. the container builds ``LocalTier2Service`` on the router for ``local`` and
     nothing for ``disabled``.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import (
    DeepAnalysisBackend,
    DeepAnalysisSettings,
    FaultMavenSettings,
)
from faultmaven.container.providers.infrastructure import create_deep_analysis_service
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service
from tests.utils import delenv_every_spelling

pytestmark = pytest.mark.unit

RETIRED = ["external", "basic"]


@pytest.fixture
def backend_env(monkeypatch):
    """Set DEEP_ANALYSIS_BACKEND with no other spelling of it in the environment."""
    delenv_every_spelling(monkeypatch, "DEEP_ANALYSIS_BACKEND")

    def _set(value):
        monkeypatch.setenv("DEEP_ANALYSIS_BACKEND", value)

    return _set


def test_default_is_local(backend_env):
    assert DeepAnalysisSettings().backend == DeepAnalysisBackend.LOCAL


@pytest.mark.parametrize("value", ["local", "disabled"])
def test_admitted_values_load(backend_env, value):
    backend_env(value)
    assert DeepAnalysisSettings().backend.value == value


@pytest.mark.parametrize("value", [*RETIRED, "quantum"])
def test_any_other_value_fails_at_settings_load(backend_env, value):
    backend_env(value)

    with pytest.raises(ValidationError) as excinfo:
        DeepAnalysisSettings()

    message = str(excinfo.value)
    assert "DEEP_ANALYSIS_BACKEND" in message
    assert "'local' or 'disabled'" in message
    assert value in message


@pytest.mark.parametrize("value", RETIRED)
def test_a_retired_backend_fails_the_whole_settings_load(backend_env, value):
    """The application's settings object, not only the section, refuses it."""
    backend_env(value)

    with pytest.raises(ValidationError, match="DEEP_ANALYSIS_BACKEND"):
        FaultMavenSettings(_env_file=None)


def _settings(backend, max_tokens=3000):
    settings = MagicMock()
    settings.deep_analysis.backend = backend
    settings.deep_analysis.max_tokens = max_tokens
    return settings


def test_local_builds_the_service_on_the_router():
    router, storage = MagicMock(), MagicMock()

    service = create_deep_analysis_service(
        _settings(DeepAnalysisBackend.LOCAL), router, storage
    )

    assert isinstance(service, LocalTier2Service)
    assert service.llm_client is router
    assert service.storage_service is storage
    assert service.max_tokens == 3000


def test_disabled_builds_nothing():
    service = create_deep_analysis_service(
        _settings(DeepAnalysisBackend.DISABLED), MagicMock(), MagicMock()
    )

    assert service is None
