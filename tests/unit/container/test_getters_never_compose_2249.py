"""A getter on an uninitialized container never composes it (#2249).

Four getters used to initialize the container themselves:
``asyncio.run(initialize())`` with no loop running, which bound the whole
composition (the Redis pool included) to a loop that closed as the getter
returned, and ``create_task(initialize())`` with one running, which answered
from a half-composed container while nothing awaited the task. A getter now
answers from what is composed, as every other getter does.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from faultmaven.container import DIContainer

pytestmark = pytest.mark.unit

_GETTERS = (
    "get_agent_service",
    "get_data_service",
    "get_knowledge_service",
    "get_oauth_service",
)


@pytest.fixture
def container(monkeypatch):
    """An uninitialized container of this test's own, its initialize() a spy."""
    instance = object.__new__(DIContainer)
    for name, factory in DIContainer._CONSTRUCTED_STATE:
        setattr(instance, name, factory())
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
