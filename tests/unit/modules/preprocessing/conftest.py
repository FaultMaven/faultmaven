"""Fixtures for the preprocessing service's tests."""

import asyncio

import pytest


@pytest.fixture
def spawned(monkeypatch):
    """Every Tier 1 worker the test starts, to read how many ran and how each
    ended (#2637)."""
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        processes.append(await spawn(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    return processes
