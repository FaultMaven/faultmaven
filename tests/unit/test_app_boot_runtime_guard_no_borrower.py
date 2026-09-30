"""The runtime check's other side: a module that never borrows (fm#1628).

No test here takes ``booted_app_client``, so no shared boot is ever live on the
app object while one of them runs, and a real boot needs no declaration. The
check in ``tests/conftest.py`` must let it through, from the test body and from
a function-scoped fixture alike: those run inside the test's own scope. A check
keyed on "the real app was entered" alone would fail every lifespan-subject
test in a module like this.
"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def test_a_module_that_never_borrows_may_boot_the_real_app():
    from faultmaven.main import app

    with TestClient(app) as client:
        assert client.app is app


@pytest.fixture
def function_scoped_real_boot():
    from faultmaven.main import app

    with TestClient(app) as client:
        yield client


def test_a_function_scoped_fixture_may_boot_the_real_app(function_scoped_real_boot):
    """Set up after the test's context is recorded, so it is inside its scope."""
    import faultmaven.main

    assert function_scoped_real_boot.app is faultmaven.main.app
