"""A getter on an uninitialized container never composes it (#2249).

Four getters used to initialize the container themselves:
``asyncio.run(initialize())`` with no loop running, which bound the whole
composition (the Redis pool included) to a loop that closed as the getter
returned, and ``create_task(initialize())`` with one running, which answered
from a half-composed container while nothing awaited the task. A getter now
answers from what is composed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.unit.container.conftest import make_private_container

pytestmark = pytest.mark.unit

_GETTERS = (
    "get_agent_service",
    "get_data_service",
    "get_knowledge_service",
    "get_oauth_service",
)

#: The base class's getters initialize lazily, by calling ``initialize()``
#: without awaiting it. DIContainer's initialize() is a coroutine, so each is
#: overridden there; one dropped override would bring the lazy path back.
_BASE_LAZY_GETTERS = (
    "get_agent_service",
    "get_llm_provider",
    "get_sanitizer",
    "get_tracer",
    "get_tools",
)


@pytest.fixture
def container(monkeypatch):
    """An uninitialized container of this test's own, its initialize() a spy."""
    instance = make_private_container()
    monkeypatch.setattr(instance, "initialize", AsyncMock())
    return instance


@pytest.mark.parametrize("getter", _GETTERS)
def test_a_getter_without_a_running_loop_composes_nothing(container, getter):
    assert getattr(container, getter)() is None

    container.initialize.assert_not_called()
    assert not container.is_initialized


@pytest.mark.parametrize("getter", _GETTERS)
async def test_a_getter_inside_a_running_loop_schedules_nothing(container, getter):
    tasks_before = asyncio.all_tasks()

    assert getattr(container, getter)() is None

    container.initialize.assert_not_called()
    assert asyncio.all_tasks() == tasks_before
    assert not container.is_initialized


@pytest.mark.parametrize("getter", _GETTERS)
def test_a_getter_answers_from_what_is_composed(container, getter):
    """Positive control: the spy and the probe see a composed service."""
    service = object()
    setattr(container, getter.removeprefix("get_"), service)

    assert getattr(container, getter)() is service
    container.initialize.assert_not_called()


@pytest.mark.parametrize("getter", _BASE_LAZY_GETTERS)
def test_no_getter_the_base_class_composes_from_does_so_here(container, getter):
    if getter == "get_llm_provider":
        # It refuses when no provider is composed; it must not compose one.
        with pytest.raises(RuntimeError, match="LLM provider not initialized"):
            container.get_llm_provider()
    else:
        getattr(container, getter)()

    container.initialize.assert_not_called()
