"""The DI container is read off ``app.state``, never ``app.extra`` (#2170).

FastAPI 0.145.0 removed ``FastAPI.extra``. Every reader below swallowed the
``AttributeError`` that raised, so a reader left on ``app.extra`` would serve
``None`` with nothing to say so. Each is pinned on both sides: the container
published on ``app.state`` answers, and a missing one answers ``None`` as
before.
"""

from types import SimpleNamespace

import pytest
from starlette.datastructures import State

from faultmaven.api.v1.dependencies import (
    get_case_repository,
    get_case_vector_store,
    get_llm_provider,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_runbook_kb_dependency,
)

pytestmark = pytest.mark.unit

READERS = [
    (get_case_repository, "case_repository"),
    (get_case_vector_store, "case_vector_store"),
    (get_llm_provider, "llm_provider"),
    (_di_get_runbook_kb_dependency, "runbook_kb"),
]


def _request(**published) -> SimpleNamespace:
    state = State()
    for name, value in published.items():
        setattr(state, name, value)
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.parametrize("reader, attribute", READERS)
async def test_each_reader_resolves_the_container_from_app_state(reader, attribute):
    component = object()
    container = SimpleNamespace(**{attribute: component})

    assert await reader(_request(di_container=container)) is component


@pytest.mark.parametrize("reader, attribute", READERS)
async def test_each_reader_answers_none_without_a_container(reader, attribute):
    assert await reader(_request()) is None


async def test_health_reports_the_container_published_on_app_state(monkeypatch):
    """``/health``'s container block, on its fallback arm (no component monitor)."""
    from faultmaven import main
    from faultmaven.infrastructure.health.component_monitor import component_monitor

    async def _unavailable():
        raise RuntimeError("component monitor unavailable")

    monkeypatch.setattr(component_monitor, "check_all_components", _unavailable)
    container = SimpleNamespace(
        health_check=lambda: {"status": "healthy", "components": {"db": "ok"}},
        _initialized=True,
        _initializing=False,
    )
    monkeypatch.setattr(main.app.state, "di_container", container, raising=False)

    reported = await main.health_check()

    assert reported["services"]["di_container"] == "healthy"
    assert reported["container_components"] == {"db": "ok"}
    assert reported["container_initialized"] is True

    monkeypatch.delattr(main.app.state, "di_container")
    reported = await main.health_check()
    assert "di_container" not in reported["services"]
    assert "container_components" not in reported
