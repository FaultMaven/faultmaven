"""Fixtures for the knowledge module's unit tests."""

import os

import pytest

from tests import offline_tiktoken


@pytest.fixture(scope="session", autouse=True)
def _tiktoken_cache_for_the_preparation_worker(tmp_path_factory):
    """The preparation worker is a child process (#2622): it inherits the
    environment, not ``offline_tiktoken.install``'s patch, and the network guard
    does not reach it. So the encoding is written where tiktoken's own cache
    lookup finds it, and ``TIKTOKEN_CACHE_DIR`` names that directory for every
    worker this session starts: none of them asks the network."""
    directory = tmp_path_factory.mktemp("tiktoken")
    offline_tiktoken.seed_cache(directory)
    previous = os.environ.get("TIKTOKEN_CACHE_DIR")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(directory)
    yield directory
    if previous is None:
        os.environ.pop("TIKTOKEN_CACHE_DIR", None)
    else:
        os.environ["TIKTOKEN_CACHE_DIR"] = previous
