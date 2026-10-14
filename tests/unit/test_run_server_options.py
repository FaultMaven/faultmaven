"""Every uvicorn option ``run_server`` owns is pinned, for every app it launches.

``run_server(app)`` is the one place the API's uvicorn options live, so an
image that launches through it (the Cloud image launches
``run_server("faultmaven_cloud.main:app")``, faultmaven-cloud#42) inherits all
of them. The uvicorn CLI cannot stand in: ``--log-config`` must name a file,
so it cannot express ``log_config=None``.

Pinned here, each with the consequence of losing it:

* ``access_log=False`` -- uvicorn's plaintext access lines would duplicate the
  structured request logs.
* ``log_config=None`` -- ``uvicorn.error`` records (tracebacks embed client
  exception messages and request URLs) would stop at uvicorn's own handler and
  never reach the root logger where the redacting renderer lives.
* ``timeout_keep_alive`` -- see ``test_run_server_keepalive.py`` (infra#377).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from uvicorn.importer import import_from_string

import faultmaven.main
from faultmaven.config.settings import ServerSettings
from faultmaven.main import run_server

pytestmark = pytest.mark.unit

DEFAULT_APP = "faultmaven.main:app"
CLOUD_APP = "faultmaven_cloud.main:app"


def _launch(app=None, **server_fields):
    """Call ``run_server`` with ``uvicorn.run`` patched; return the mock.

    ``autospec`` makes the mock carry ``uvicorn.run``'s real signature, so an
    option uvicorn does not have (a misspelt or removed kwarg) fails here
    instead of at the first production start.
    """
    server = ServerSettings(**server_fields)
    with (
        patch("faultmaven.config.settings.get_settings") as get,
        patch("uvicorn.run", autospec=True) as run,
    ):
        get.return_value = SimpleNamespace(server=server)
        if app is None:
            run_server()
        else:
            run_server(app)
    run.assert_called_once()
    return run


@pytest.mark.parametrize("app", [None, DEFAULT_APP, CLOUD_APP])
@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("reload", [False, True])
def test_every_uvicorn_option_is_passed(app, workers, reload):
    run = _launch(
        app,
        host="127.0.0.1",
        port=9123,
        reload=reload,
        workers=workers,
        timeout_keep_alive=90,
    )

    expected = {
        "host": "127.0.0.1",
        "port": 9123,
        "reload": reload,
        "log_level": "info",
        "access_log": False,
        "timeout_keep_alive": 90,
        "log_config": None,
    }
    if workers > 1:
        expected["workers"] = workers
    assert run.call_args.args == (app or DEFAULT_APP,)
    assert run.call_args.kwargs == expected


@pytest.mark.parametrize("app", [None, CLOUD_APP])
def test_access_log_is_off_and_uvicorn_installs_no_log_config(app):
    """The two options a CLI launch cannot carry, asserted on their own."""
    kwargs = _launch(app).call_args.kwargs

    assert kwargs["access_log"] is False
    # ``None`` is the value, not "absent": a missing key would leave uvicorn
    # on its default LOGGING_CONFIG (own handler, propagate=False).
    assert "log_config" in kwargs and kwargs["log_config"] is None


def test_no_workers_kwarg_for_a_single_worker():
    assert "workers" not in _launch(workers=1).call_args.kwargs


def test_default_app_is_the_faultmaven_app():
    """The default import string resolves to the real app object."""
    assert _launch().call_args.args == (DEFAULT_APP,)
    assert import_from_string(DEFAULT_APP) is faultmaven.main.app


@pytest.mark.parametrize(
    "app",
    [
        "",
        "faultmaven.main",  # no attribute
        "faultmaven.main:",  # empty attribute
        ":app",  # empty module
        "faultmaven.main:app:extra",
        "faultmaven..main:app",
        ".faultmaven.main:app",
        "faultmaven main:app",
        "faultmaven/main.py:app",
        "1faultmaven:app",
        "faultmaven.main:app\n",
        None,
        FastAPI(),  # an app object: uvicorn refuses it with workers/reload
    ],
    ids=lambda a: repr(a) if not isinstance(a, FastAPI) else "app-object",
)
def test_malformed_app_is_rejected_before_uvicorn_starts(app):
    with (
        patch("faultmaven.config.settings.get_settings") as get,
        patch("uvicorn.run", autospec=True) as run,
    ):
        with pytest.raises(ValueError, match="module:attr"):
            run_server(app)

    run.assert_not_called()
    get.assert_not_called()
