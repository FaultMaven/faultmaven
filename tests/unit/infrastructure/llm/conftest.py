"""Shared doubles for the LLM provider/router tests.

``_mock_aiohttp_session`` had been copy-pasted into eight test modules under
this directory, AST-identical after docstring stripping. It encodes the
aiohttp double-async-context-manager protocol —

    async with aiohttp.ClientSession() as session:
        async with session.post(...) as response:

— so an aiohttp upgrade, or a move to a shared client session, has to land in
every copy. A partial edit leaves the un-updated modules green against a stale
mock that no longer resembles the real client, which is the failure mode worth
preventing: tests that pass because they are testing the mock.

There was no ``conftest.py`` anywhere under ``tests/unit/infrastructure/llm/``
before this file, so there was nothing to reuse. New tests in this tree should
take the ``mock_aiohttp_session`` fixture rather than adding copy nine.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def make_mock_aiohttp_session(response_data: dict, status: int = 200):
    """Build a mock ``aiohttp.ClientSession`` that returns *response_data*.

    Patch it in with ``patch("aiohttp.ClientSession", return_value=session)``;
    read what the provider sent with ``session.post.call_args``.

    Both context-manager layers are mocked because the providers use both: the
    session is entered, then the response returned by ``post()`` is entered.
    ``__aexit__`` returns False so exceptions raised inside the block are not
    swallowed — a mock that returned True would silently pass tests asserting
    that a provider raises.
    """
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.fixture
def mock_aiohttp_session():
    """The factory above, as a fixture: ``mock_aiohttp_session(payload)``."""
    return make_mock_aiohttp_session
