"""Every async test under this directory runs on a ``VirtualTimeLoop`` (#1579).

pytest-asyncio consults ``pytest_asyncio_loop_factories`` for every async test
under the conftest that implements it, and a hook that answers must answer for
all of them — which is why the deadline tests have this directory to
themselves. See ``tests/wallclock/virtual_time.py``.
"""

from tests.wallclock import virtual_time_loop_factories


def pytest_asyncio_loop_factories(config, item):
    return virtual_time_loop_factories()
