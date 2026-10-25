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
from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.config.persistent_database import NonPersistentDatabaseError
from faultmaven.config.settings import ServerSettings
from faultmaven.config.worker_stores import PerProcessStoresError
from faultmaven.infrastructure.security.pseudonym_key import (
    PseudonymKeyUnavailableError,
)
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
        # The gates read more than ``server``; they have tests of their own.
        patch("faultmaven.bootstrap.configuration_gates.validate_configuration"),
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


@pytest.mark.parametrize(
    ("environ", "refusal", "names"),
    [
        # Several processes over the in-process FakeRedis.
        ({"SKIP_SERVICE_CHECKS": "true"}, PerProcessStoresError, "FakeRedis"),
        # No persistent database: refused as that, ahead of the stores
        # refusal it would also meet.
        (
            {
                "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                "SKIP_SERVICE_CHECKS": "true",
            },
            NonPersistentDatabaseError,
            "DATABASE_URL",
        ),
        # Incoherent with DEPLOYMENT_MODE: refused ahead of the database and
        # stores refusals it would also meet.
        (
            {
                "DEPLOYMENT_MODE": "cloud",
                "AUTH_MODE": "local",
                "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                "SKIP_SERVICE_CHECKS": "true",
            },
            DeploymentCoherenceError,
            "DEPLOYMENT_MODE",
        ),
        # A wildcard CORS origin on a deployed box: the shipped default holds
        # chrome-extension://*, so a production box that sets none refuses.
        # (The app also refuses it at import, in every worker.)
        (
            {"ENVIRONMENT": "production"},
            RuntimeError,
            "Wildcard CORS origins are not allowed",
        ),
        # A pseudonym key too short to use.
        (
            {"REDACTION_PSEUDONYM_KEY": "short"},
            PseudonymKeyUnavailableError,
            "REDACTION_PSEUDONYM_KEY is only 5 characters",
        ),
        # A configured provider with no credential, the gate the lifespan runs
        # outside test environments.
        (
            {
                "CHAT_PROVIDER": "openai",
                "OPENAI_API_KEY": "",
                "REDACTION_PSEUDONYM_KEY": "x" * 64,  # no key file is written
                "FM_TEST_OUTSIDE_TEST_ENVIRONMENT": "1",
            },
            ValueError,
            "No usable LLM credential",
        ),
    ],
)
def test_a_refusal_the_configuration_decides_exits_before_uvicorn_starts(
    monkeypatch, environ, refusal, names
):
    """Before several workers, the launcher runs every gate the configuration
    alone decides, in the lifespan's order, and exits as uvicorn does on a
    failed startup: refused only in the workers, uvicorn restarts them forever
    (#2281)."""
    from faultmaven.bootstrap import configuration_gates
    from faultmaven.config.settings import get_settings, reset_settings

    if environ.pop("FM_TEST_OUTSIDE_TEST_ENVIRONMENT", None):
        monkeypatch.setattr(
            "faultmaven.bootstrap.composition._is_test_environment",
            lambda settings=None: False,
        )
    for name, value in {
        "CONFIG_PRESET": "none",
        "DEPLOYMENT_MODE": "standalone",
        "SKIP_SERVICE_CHECKS": "false",
        "RELOAD": "false",
        "WORKERS": "2",
        "RUN_STARTUP_MIGRATIONS": "true",
        "REDIS_URL": "redis://127.0.0.1:6390/0",
        "CHROMADB_URL": "http://chroma.internal:8000",
        "VECTOR_STORAGE_TYPE": "chromadb",
        # Never connected to: the launcher refuses before it migrates.
        "DATABASE_URL": "postgresql+asyncpg://fm@127.0.0.1:1/fm",
        **environ,
    }.items():
        monkeypatch.setenv(name, value)
    validate = configuration_gates.validate_configuration
    refused = []

    def recording(settings, **kwargs):
        try:
            validate(settings, **kwargs)
        except Exception as error:
            refused.append(error)
            raise

    reset_settings()
    try:
        with (
            patch.object(configuration_gates, "validate_configuration", recording),
            patch("uvicorn.run", autospec=True) as run,
            patch("faultmaven.bootstrap.data_init.migrate_before_workers") as migrate,
        ):
            with pytest.raises(SystemExit) as exited:
                run_server()
        assert exited.value.code == 3  # uvicorn's STARTUP_FAILURE
        (error,) = refused
        assert isinstance(error, refusal)
        assert names in str(error)
        migrate.assert_not_called()
        run.assert_not_called()
        assert get_settings().server.workers == 2
    finally:
        reset_settings()


@pytest.mark.parametrize("workers", ["0", "-1"])
def test_fewer_than_one_worker_is_refused_by_the_settings(monkeypatch, workers):
    """``workers`` reaches uvicorn whatever its value now (#2281), and no count
    below one serves anything."""
    from pydantic import ValidationError

    monkeypatch.setenv("WORKERS", workers)
    with pytest.raises(ValidationError, match="workers"):
        ServerSettings()


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
    ("workers", "reload", "run_startup_migrations", "expected"),
    [
        (2, False, True, ["logging", "gates", "migrate", "run"]),
        # A migration Job owns the schema.
        (2, False, False, ["logging", "gates", "run"]),
        # One process: the lifespan refuses and migrates, and its refusal
        # stops uvicorn.
        (1, False, True, ["run"]),
        (2, True, True, ["run"]),  # RELOAD serves one process
    ],
)
def test_several_workers_are_validated_and_migrated_once_before_uvicorn_forks(
    workers, reload, run_startup_migrations, expected
):
    """Each worker's lifespan runs the gates and the startup migration. Refused
    there, uvicorn restarted the workers forever; migrating together on a first
    boot, they raced (#2281). Before several workers the launcher does both
    first, once, with the app's logging installed (until uvicorn imports the
    app, nothing else installs it in this process)."""
    from unittest.mock import MagicMock

    order = MagicMock()
    server = ServerSettings(workers=workers, reload=reload)
    with (
        patch("faultmaven.config.settings.get_settings") as get,
        patch("uvicorn.run", autospec=True) as run,
        patch(
            "faultmaven.bootstrap.configuration_gates.validate_configuration"
        ) as gates,
        patch("faultmaven.infrastructure.logging.config.get_logger") as logging,
        patch("faultmaven.bootstrap.data_init.migrate_before_workers") as migrate,
    ):
        get.return_value = SimpleNamespace(
            server=server, run_startup_migrations=run_startup_migrations
        )
        for name, mock in [
            ("logging", logging),
            ("gates", gates),
            ("migrate", migrate),
            ("run", run),
        ]:
            order.attach_mock(mock, name)
        run_server()

    names = [name for name, _args, _kwargs in order.mock_calls if "." not in name]
    assert names == expected
    if gates.called:
        # The advisory warnings are the workers' to print, once each.
        assert gates.call_args.kwargs == {"advisories": False}


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
