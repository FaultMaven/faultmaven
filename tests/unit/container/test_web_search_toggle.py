"""ENABLE_WEB_SEARCH actually gates web-search registration (#1234).

The knob is documented in ``.env.example``, ``docs/getting-started/quickstart.md``
and ``CLAUDE.md`` as the web-search toggle, and until this change **nothing in
``faultmaven/`` read it**: the registry composed the tool from provider keys
alone, so a deployment that set ``ENABLE_WEB_SEARCH=false`` still handed the
model a tool that sends investigation text to a third party, and
``/admin/config/status`` was the only reader — reporting an intent the runtime
ignored.

Pinned at the registry because that is the decision the knob is supposed to
make, and because ``/admin/config/status`` reports this same composed tool.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.container.providers.tools import create_registry_tools
from faultmaven.modules.agent.tools.web_search import WebSearchTool


def _settings(enable_web_search):
    """Settings whose ONLY relevant difference is the toggle.

    A Tavily key is always present, so a tool would compose on provider grounds
    in both cases — which is what makes the toggle the sole cause of the
    difference these tests measure.
    """
    settings = MagicMock()
    key = MagicMock()
    key.get_secret_value.return_value = "tvly-test-key"
    settings.knowledge.tavily_api_key = key
    settings.knowledge.enable_web_search = enable_web_search
    return settings


def _web_search_tools(settings):
    return [t for t in create_registry_tools(settings) if isinstance(t, WebSearchTool)]


@pytest.mark.unit
def test_web_search_is_registered_when_enabled():
    """The positive control.

    Without it, a registry that never registered web search at all would
    satisfy the disabled case below and read as a working toggle.
    """
    assert len(_web_search_tools(_settings(True))) == 1


@pytest.mark.unit
def test_web_search_is_not_registered_when_disabled():
    """The knob's whole purpose, and what it did not do before #1234.

    The Tavily key is still configured here — the tool would compose — so this
    fails against any implementation that gates on provider keys alone.
    """
    assert _web_search_tools(_settings(False)) == []


@pytest.mark.unit
def test_an_absent_toggle_leaves_web_search_registered():
    """Absence is not "off".

    The setting defaults to True, and a settings object that does not carry it
    at all (older config, or a partial double) must not silently disable a
    capability — ``getattr(..., True)`` rather than ``getattr(..., False)``.
    """
    settings = _settings(True)
    del settings.knowledge.enable_web_search

    assert len(_web_search_tools(settings)) == 1
