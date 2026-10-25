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

import os
import sys

import pytest

from tests.integration._app_rebuild import rebuild_app

pytestmark = [pytest.mark.integration]


def test_rebuild_returns_a_new_app_without_publishing_it():
    # Establish the baseline rather than assume one. ``rebuild_app`` restores
    # "nothing was published" faithfully when that is what it found, so a test
    # that read ``sys.modules`` directly would raise ``KeyError`` in any run
    # where the OAuth modules were collected before this file.
    import faultmaven.main  # noqa: F401

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


def test_rebuild_under_an_environment_builds_with_it_and_puts_it_back(monkeypatch):
    """The OAuth modules set ``OAUTH_ENABLED`` at import and never put it back,
    so every test collected with them ran with it (#2291). ``rebuild_app_under``
    builds with the environment it is given, on settings read from it, and
    leaves the process's own environment and settings after."""
    from faultmaven.config.settings import get_settings
    from tests.integration import _app_rebuild

    monkeypatch.delenv("OAUTH_ENABLED", raising=False)
    assert get_settings().auth.oauth_enabled is False  # cached, without it
    seen = {}

    def build():
        seen["environment"] = os.environ.get("OAUTH_ENABLED")
        seen["settings"] = get_settings().auth.oauth_enabled
        return "the app"

    monkeypatch.setattr(_app_rebuild, "rebuild_app", build)

    assert _app_rebuild.rebuild_app_under({"OAUTH_ENABLED": "true"}) == "the app"
    assert seen == {"environment": "true", "settings": True}
    assert "OAUTH_ENABLED" not in os.environ
    assert get_settings().auth.oauth_enabled is False
