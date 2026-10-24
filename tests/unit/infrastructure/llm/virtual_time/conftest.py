"""Every async test under this directory runs on a ``VirtualTimeLoop`` (#1579).

pytest-asyncio consults ``pytest_asyncio_loop_factories`` for every async test
under the conftest that implements it, and a hook that answers must answer for
all of them — which is why the deadline tests have this directory to
themselves. See ``tests/wallclock/virtual_time.py``.

The hook's presence is global at setup, though: once any implementation is
registered, every async test in the run resolves pytest-asyncio's
session-scoped ``_asyncio_loop_factory``, whose parameter changes around the
tests here. A runner wider than function scope, and the async fixtures on it,
is therefore torn down and rebuilt around them. Nothing in the suite uses one
today.
"""

from tests.wallclock import virtual_time_loop_factories


def pytest_asyncio_loop_factories(config, item):
    return virtual_time_loop_factories()
