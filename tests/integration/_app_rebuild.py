"""Rebuild the application under mutated environment without publishing it.

The OAuth integration modules need an app built while ``OAUTH_ENABLED`` is set,
and ``faultmaven.main`` holds the app as a module-level singleton, so the only
way to get one is to drop the module and import it again.

The part that has to be undone is leaving the rebuilt module in ``sys.modules``.
pytest imports *every* test module during collection, so a rebuild performed at
module scope does not stay local to the module that asked for it: from that
point on, every later importer of ``faultmaven.main`` — and every module that
had not yet been collected — receives the rebuilt app instead of the one the
earlier modules are holding.

That mattered because these rebuilds run under ``SKIP_SERVICE_CHECKS=true``,
which ``main.setup_middleware`` read to skip the protection stack and still reads
to skip idempotency. On a bare local run the suite therefore went split-brain:
the modules collected before these ones held a protected app, and everything
collected afterwards — including ``tests/integration/test_main_app.py``, whose
subject *is* the real application — held one with no rate limiting, no
deduplication and no idempotency (fm#990).

Two things that reading is easy to overstate, so they are named here. In CI the
flag is already set in the workflow ``env`` before collection, so there was no
split-brain there — the app was uniformly unprotected, which is why removing that
gate, not this restore, is what fixed CI. And ``RequestIdMiddleware`` is absent
from any pytest-built app either way: it is independently gated on
``_is_test_environment()``, and restoring the published module does not bring it
back.

So the rebuilt module is handed back as an object and the previous one is
restored. The caller keeps the app it asked for; nobody else is affected.

``rebuild_app`` leaves the environment to its caller. ``rebuild_app_under`` sets
an environment for the build and puts it back after: the OAuth integration
modules used to set theirs at import, where pytest's collection made it every
later test's (#2291). A module whose app is built that way and whose tests need
the same environment while they run asks for the ``oauth_environment`` fixture
(``tests/integration/modules/auth/conftest.py``), which sets it for that
module's tests only.
"""

import sys

import pytest

#: Every app ``rebuild_app`` handed out, so the integration conftest can end
#: their protection middleware's service after each test (they are served
#: without a lifespan, and that middleware may build its own Redis client).
_REBUILT_APPS: list = []


def rebuilt_apps() -> tuple:
    """The apps ``rebuild_app`` has handed out in this process."""
    return tuple(_REBUILT_APPS)


def rebuild_app():
    """Import a fresh ``faultmaven.main`` and return its ``app``.

    Callers are expected to have already set the environment they want the app
    built under, and to have cleared the settings singleton with
    ``reset_settings()`` so the rebuild reads it.
    """
    previous = sys.modules.pop("faultmaven.main", None)
    try:
        import faultmaven.main

        _REBUILT_APPS.append(faultmaven.main.app)
        return faultmaven.main.app
    finally:
        # Restore whatever was published before, including "nothing" — a caller
        # that ran before anything imported the app must not leave one behind.
        #
        # The parent package attribute has to be restored alongside the
        # ``sys.modules`` entry, because ``import faultmaven.main`` above bound
        # the rebuilt module onto the ``faultmaven`` package object and a later
        # import of an already-published module never re-binds it. Restoring
        # only one of the two would leave ``from faultmaven.main import app``
        # and ``faultmaven.main.app`` resolving to *different* applications —
        # a smaller edition of the split-brain this module exists to prevent.
        package = sys.modules.get("faultmaven")
        if previous is not None:
            sys.modules["faultmaven.main"] = previous
            if package is not None:
                package.main = previous
        else:
            sys.modules.pop("faultmaven.main", None)
            if package is not None:
                # Not ``delattr``: the attribute is absent whenever the import
                # above failed before binding it, and an AttributeError raised
                # from this ``finally`` would mask the real one.
                package.__dict__.pop("main", None)


def rebuild_app_under(environment):
    """``rebuild_app`` with ``environment`` set for the build, then put back.

    The settings singleton is cleared on both sides, so the build reads
    ``environment`` and nothing after it reads a settings object built from it.
    Clear the singleton, never drop ``faultmaven.config.settings`` from
    ``sys.modules``: a second module object would keep its own singleton, and
    every module that already imported from the first would read a stale one.
    """
    from faultmaven.config.settings import reset_settings

    with pytest.MonkeyPatch.context() as patch:
        for name, value in environment.items():
            patch.setenv(name, value)
        reset_settings()
        app = rebuild_app()
    reset_settings()
    return app
