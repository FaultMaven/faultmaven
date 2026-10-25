"""Every uvicorn option ``run_server`` owns is pinned, for every app it launches.

``run_server(app)`` is the one place the API's uvicorn options live, so an
image that launches through it inherits all of them. The Cloud image is to
launch ``run_server("faultmaven_cloud.main:app")`` once faultmaven-cloud#25
bumps its core pin (faultmaven-cloud#42); it does not yet. The uvicorn CLI
cannot stand in: ``--log-config`` must name a file, so it cannot express
``log_config=None``. (``--no-access-log`` exists, so ``access_log=False`` alone
is expressible there; ``log_config=None`` is the option that is not.)

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
from faultmaven.server import run_server

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
        # The stores refusal reads more than ``server``; it has tests of its own.
        patch("faultmaven.config.worker_stores.refuse_configured_per_process_stores"),
    ):
        # No launcher migration here; that has tests of its own.
        get.return_value = SimpleNamespace(server=server, run_startup_migrations=False)
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
        "ws": "none",
        "log_config": None,
        "workers": workers,
    }
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


def test_web_concurrency_cannot_change_the_worker_count(monkeypatch):
    """With ``workers`` unset, uvicorn reads WEB_CONCURRENCY: WORKERS=1 plus
    WEB_CONCURRENCY=4 served four processes, and every check that reads the
    settings believed one (#2281). ``workers`` is always passed now."""
    import uvicorn

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    kwargs = _launch(workers=1).call_args.kwargs

    assert kwargs["workers"] == 1
    assert uvicorn.Config(DEFAULT_APP, workers=kwargs["workers"]).workers == 1


def test_a_refusal_the_configuration_decides_exits_before_uvicorn_starts(
    monkeypatch,
):
    """WORKERS>1 over FakeRedis (SKIP_SERVICE_CHECKS) is refused in the
    launcher, so no worker is started for uvicorn to restart forever (#2281)."""
    from faultmaven.config.settings import get_settings, reset_settings
    from faultmaven.config.worker_stores import PerProcessStoresError

    for name, value in {
        "CONFIG_PRESET": "none",
        "DEPLOYMENT_MODE": "standalone",
        "SKIP_SERVICE_CHECKS": "true",
        "RELOAD": "false",
        "WORKERS": "2",
    }.items():
        monkeypatch.setenv(name, value)
    reset_settings()
    try:
        with patch("uvicorn.run", autospec=True) as run:
            with pytest.raises(PerProcessStoresError, match="WORKERS=2"):
                run_server()
        run.assert_not_called()
        assert get_settings().server.workers == 2
    finally:
        reset_settings()


def test_default_app_is_the_faultmaven_app():
    """The default import string resolves to the real app object."""
    assert _launch().call_args.args == (DEFAULT_APP,)
    assert import_from_string(DEFAULT_APP) is faultmaven.main.app


@pytest.mark.parametrize(
    "app",
    [
        "pkg.mod:attr.sub",  # a dotted attribute, which uvicorn resolves
        "main:app",  # a top-level module, no dot
        "_pkg.mod_2:_app",
    ],
)
def test_well_formed_import_strings_are_accepted(app):
    assert _launch(app).call_args.args == (app,)


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


@pytest.mark.parametrize(
    ("workers", "reload", "run_startup_migrations", "migrates"),
    [
        (2, False, True, True),
        (1, False, True, False),
        (2, True, True, False),  # RELOAD serves one process, which migrates itself
        (2, False, False, False),  # a migration Job owns the schema
    ],
)
def test_several_workers_are_migrated_once_before_uvicorn_forks(
    workers, reload, run_startup_migrations, migrates
):
    """Each worker's lifespan runs the startup migrations; launched together on
    a first boot they raced (#2281). The launcher migrates first, once."""
    from unittest.mock import MagicMock

    order = MagicMock()
    server = ServerSettings(workers=workers, reload=reload)
    with (
        patch("faultmaven.config.settings.get_settings") as get,
        patch("uvicorn.run", autospec=True) as run,
        patch("faultmaven.config.worker_stores.refuse_configured_per_process_stores"),
        patch("faultmaven.bootstrap.data_init.migrate_before_workers") as migrate,
    ):
        get.return_value = SimpleNamespace(
            server=server, run_startup_migrations=run_startup_migrations
        )
        order.attach_mock(migrate, "migrate")
        order.attach_mock(run, "run")
        run_server()

    names = [name for name, _args, _kwargs in order.mock_calls]
    assert names == (["migrate", "run"] if migrates else ["run"])


@pytest.mark.parametrize("at_head", [True, False])
def test_the_launcher_migration_runs_alembic_only_off_head(at_head):
    from faultmaven.bootstrap import data_init

    async def _at_head():
        return at_head

    with (
        patch.object(data_init, "_schema_already_at_head", _at_head),
        patch.object(data_init, "run_alembic_migrations") as alembic,
    ):
        data_init.migrate_before_workers()

    assert alembic.called is (not at_head)
