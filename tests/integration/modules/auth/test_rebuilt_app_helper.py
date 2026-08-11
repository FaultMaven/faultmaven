"""``rebuild_app`` must hand back a new app without publishing it (fm#990).

The OAuth modules in this package rebuild ``faultmaven.main`` to get an app built
while ``OAUTH_ENABLED`` is set. Doing that used to leave the rebuilt module
published, and because pytest imports every test module during collection, every
module collected afterwards silently received that app instead of the one the
earlier modules were holding — two applications in one process, differing in
their middleware stack.

The regression is invisible from the outside: both objects serve requests. So it
is asserted here on the two pieces of process-wide state a rebuild touches, and
on the rebuild actually having happened — a helper that quietly returned the
existing app would satisfy the restore assertions while doing nothing.
"""

import sys

import pytest

from tests.integration.modules.auth._rebuilt_app import rebuild_app

pytestmark = [pytest.mark.integration]


def test_rebuild_returns_a_new_app_without_publishing_it():
    published_module = sys.modules["faultmaven.main"]
    published_app = published_module.app

    rebuilt = rebuild_app()

    # It really rebuilt: otherwise the restore assertions below are vacuous.
    assert rebuilt is not published_app

    # ``sys.modules`` — what ``from faultmaven.main import app`` resolves through.
    assert sys.modules["faultmaven.main"] is published_module
    assert sys.modules["faultmaven.main"].app is published_app

    # The parent package attribute — what ``faultmaven.main.app`` resolves
    # through. Importing an already-published module does not re-bind this, so
    # restoring only ``sys.modules`` would leave the two spellings disagreeing.
    import faultmaven

    assert faultmaven.main is published_module
    assert faultmaven.main.app is published_app
