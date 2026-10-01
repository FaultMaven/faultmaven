"""The runtime check's other side: a module that names no borrow (fm#1628).

No test here takes ``booted_app_client`` by name, so the declaration rule in
``tests/conftest.py`` never applies here, and a real boot needs no declaration.
The check must let it through, from the test body and from a function-scoped
fixture alike: those run inside the test's own scope. A check keyed on "the
real app was entered" alone would fail every lifespan-subject test in a module
like this.

One test borrows the shared boot by ``request.getfixturevalue``, which names
nothing the declaration rule can read. The live count is what catches an
overlap there. That test stands the shared boot down when it ends, so the
others never run beside it, whatever the order.
"""

import re

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

#: What the live count's failure says, as ``tests/conftest.py`` words it.
OVERLAP = "while another lifespan on it is live"


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


def test_a_borrow_by_getfixturevalue_is_guarded_by_the_live_count(request):
    """Neither a second lifespan nor a re-entry of the lent client gets past it."""
    from faultmaven.main import app

    overlap = re.escape(request.node.nodeid) + ".*" + re.escape(OVERLAP)
    lent = request.getfixturevalue("booted_app_client")
    try:
        with pytest.raises(pytest.fail.Exception, match=overlap):
            with TestClient(app):
                pass
        with pytest.raises(pytest.fail.Exception, match=overlap):
            with lent:
                pass
    finally:
        request.getfixturevalue("_real_app_boot").release()
