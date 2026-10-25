"""server.py

Purpose: the one launcher for the API process, and the one home of the
uvicorn options (``run_server``).

``python -m faultmaven.server`` is how the image and the no-Docker dev script
start the API. It exists as its own module so that **launching never executes
``faultmaven/main.py`` as ``__main__``**: ``python -m faultmaven.main`` ran
``main.py`` once as ``__main__`` (to call ``run_server``) and a second time when
uvicorn imported the ``"faultmaven.main:app"`` string, so every import-time side
effect ran twice (duplicate import-time logs, ``starlette.requests.Request.form``
wrapped twice, Prometheus scrape hooks registered twice) and the served app was
not the ``__main__`` one (#1985).

Invariant: every launch path executes ``faultmaven/main.py`` exactly once, in
the process that serves requests, when uvicorn imports the app string.

So ``__main__`` must be a module that is not an app module: this one, which
imports no app module at module level (a launcher for another app does not load
the core app as a side effect). ``main.py`` has one ``__main__`` guard, at the
top, that refuses: ``python -m faultmaven.main`` exits 1 with a message naming
this launcher, before any import-time work, instead of running the app module
as a script. ``python -m faultmaven.server`` only imports this module;
``faultmaven.main`` is imported once, by uvicorn.

A launcher for another app (the Cloud image's ``faultmaven_cloud.main:app``)
cannot use ``-m faultmaven.server``, which always serves the core app. It calls
the function instead, which imports this module and not the app's module::

    python -c "from faultmaven.server import run_server; \
run_server('faultmaven_cloud.main:app')"
"""

import re

# ``module.path:attribute`` -- the import-string form uvicorn resolves.
_APP_IMPORT_STRING_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
)


def run_server(app: str = "faultmaven.main:app") -> None:
    """Start uvicorn with the configured host, port, workers and keep-alive.

    ``app`` is the ASGI application as an import string (``module:attr``),
    never an app object: uvicorn refuses an object when ``workers > 1`` or
    ``reload`` is set, so a string is the only form that works for every
    setting. It is also what keeps the app's module out of this process until
    uvicorn imports it, once (see the module docstring). Every uvicorn option
    below is owned here, once, so any image that launches through this function
    inherits all of them. The Cloud image is to launch
    ``run_server("faultmaven_cloud.main:app")`` once faultmaven-cloud#25 bumps
    its core pin, rather than the ``uvicorn`` CLI, which cannot express
    ``log_config=None`` (it also must set ``PORT=8000 HOST=0.0.0.0
    RELOAD=false``, since host, port and reload come from settings, and must not
    launch as ``python -m faultmaven_cloud.main``: that module mounts its
    routers at import, so a ``__main__`` run followed by uvicorn's import of the
    string mounts them twice, #1985).

    Raises:
        ValueError: ``app`` is not a ``module:attr`` import string.
    """
    if not isinstance(app, str) or not _APP_IMPORT_STRING_RE.fullmatch(app):
        raise ValueError(
            f"run_server() needs the app as a 'module:attr' import string "
            f"(uvicorn refuses an app object when workers > 1 or reload is "
            f"set), got {app!r}"
        )

    import uvicorn

    # Configuration from unified settings
    from faultmaven.config.settings import get_settings
    from faultmaven.config.worker_stores import (
        refuse_configured_per_process_stores,
        serving_processes,
    )

    settings = get_settings()
    server = settings.server
    # Before uvicorn forks: several processes over stores the configuration
    # alone makes process-local are refused here, so the launcher exits with no
    # worker started. Refused in each worker's lifespan instead, uvicorn
    # restarts the worker forever (#2281). The lifespan still checks, for a
    # launch that does not come through here.
    refuse_configured_per_process_stores(settings)
    # Several workers on PostgreSQL: migrate once here, so they do not race on a
    # first boot; each worker's lifespan then finds the schema at head (#2281).
    if serving_processes(settings) > 1 and settings.run_startup_migrations:
        from faultmaven.bootstrap.data_init import migrate_before_workers

        migrate_before_workers()
    # Always passed: with ``workers`` unset uvicorn reads WEB_CONCURRENCY, and
    # the settings would no longer say how many processes serve (#2281).
    options: dict[str, int] = {"workers": server.workers}
    # access_log=False: uvicorn's plaintext access lines duplicate the
    # structured (JSON) request start/completion logs emitted by
    # LoggingMiddleware -- one access log, structured, with correlation IDs.
    uvicorn.run(
        app,
        host=server.host,
        port=server.port,
        reload=server.reload,
        log_level="info",
        access_log=False,
        # Idle keep-alive must outlast every client that reuses a connection:
        # ingress-nginx holds upstream connections 60 s (upstream-keepalive-timeout)
        # and slack-agent's httpx 5 s (keepalive_expiry). uvicorn's own default
        # of 5 s would close a connection nginx is about to reuse, and a POST
        # on it is not retried (502). uvicorn.run() does not read
        # UVICORN_TIMEOUT_KEEP_ALIVE, so the value is passed explicitly.
        timeout_keep_alive=server.timeout_keep_alive,
        # ws="none": the app serves no WebSocket route, and uvicorn's default
        # ("auto") would load its websockets implementation, which is built on
        # the deprecated websockets.legacy API. A WebSocket route would need a
        # deliberate choice here ("websockets-sansio").
        ws="none",
        # log_config=None so uvicorn installs no handlers of its own.
        # Its default config gives the `uvicorn` logger a handler and
        # propagate=False, so `uvicorn.error` records -- including the
        # "Exception in ASGI application" traceback, which embeds an HTTP
        # client's exception message and therefore its request URL -- stop
        # at uvicorn's handler and never reach the root logger where the
        # redacting renderer lives. With no config of its own, uvicorn's
        # loggers propagate to root and are rendered like everything else.
        log_config=None,
        **options,
    )


if __name__ == "__main__":
    run_server()
