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
which ``main.setup_middleware`` reads to skip the entire protection stack. The
published app therefore carried no rate limiting, no deduplication, no
idempotency and no request-id middleware, and the suite ran split-brain: the
four modules collected before these ones exercised a protected app, everything
collected afterwards — including ``tests/integration/test_main_app.py``, whose
subject *is* the real application — exercised an unprotected one (fm#990).

So the rebuilt module is handed back as an object and the previous one is
restored. The caller keeps the app it asked for; nobody else is affected.

The environment variables are deliberately *not* restored here: the modules that
call this set them before importing anything and rely on the settings singleton
built from them for the rest of their run. That leak is real but bounded — a
later importer of ``faultmaven.main`` rebuilds under it — and it is not this
helper's to fix, because its callers depend on the mutated values. What *is*
fixed here is the published module object, which no caller asked to change.
"""

import sys


def rebuild_app():
    """Import a fresh ``faultmaven.main`` and return its ``app``.

    Callers are expected to have already set the environment they want the app
    built under, and to have cleared the settings singleton with
    ``reset_settings()`` so the rebuild reads it.
    """
    previous = sys.modules.pop("faultmaven.main", None)
    try:
        import faultmaven.main

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
