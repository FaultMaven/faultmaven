"""Every ``fm-*`` command that composes the container shuts it down, on its own loop (#2237).

Seven commands awaited ``container.initialize()`` inside a bare ``asyncio.run``
and never ``container.shutdown()``. With service checks on, ``initialize()``
builds a real ``redis.asyncio`` client, so each invocation ended its loop over
that client's open pool. They run through ``faultmaven/cli/_container.py``'s
``run_with_container`` now, which awaits the shutdown in a ``finally`` on the
loop the command ran on.

Each case drives the command's real ``main()`` with a real argv, so the call
site under test is the command's own. Only the work it runs is replaced: a
stand-in that initializes the container, which is a stub recording the loop
each call ran on. The stub is installed where the command resolves its
container: the module's own ``container`` for a command that binds it at
import, the process singleton's name otherwise. The work then succeeds, fails,
raises, has ``initialize()`` raise after it would have built the client, or
succeeds with a ``shutdown()`` that raises. ``fm-personal-tenant`` is listed
once per operation, because each has its own call site; only ``retire``
composes, and the shutdown of a container that was never initialized does
nothing.

Every run also disposes the database engine before its loop ends (#2254),
the three commands that open only the engine included (``OPENS_ONLY_THE_ENGINE``).
They run through ``run_command``, which shuts no container down. A stand-in
opens a real engine, and its ``dispose()`` is recorded with the loop it ran on.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from faultmaven.cli._container import run_with_container
from faultmaven.container import DIContainer
from faultmaven.infrastructure.persistence import database

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ORGANIZATION_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

#: The commands whose async function reports success as a bool, which ``main()``
#: turns into the exit status. The others return the status itself.
_REPORTS_A_BOOL = {
    "demote_platform_admin",
    "promote_platform_admin",
    "provision_service_account",
    "provision_sso_org",
}

#: ``module[:operation]`` -> the async function its ``main()`` runs, and an argv.
COMMANDS = {
    "demote_platform_admin": (
        "demote_from_platform_admin",
        "fm-demote-platform-admin alice",
    ),
    "promote_platform_admin": (
        "promote_to_platform_admin",
        "fm-promote-platform-admin alice",
    ),
    "provision_service_account": (
        "provision",
        "fm-provision-service-account --username slack-agent",
    ),
    "remove_org_member": (
        "remove_org_member",
        f"fm-remove-org-member --enterprise-id {ENTERPRISE_ID} "
        f"--organization-id {ORGANIZATION_ID} --user alice --dry-run",
    ),
    "reset_kb": ("reset_kb", "fm-reset-kb --dry-run"),
    "reassign_cases": (
        "reassign_cases",
        f"fm-reassign-cases --enterprise-id {ENTERPRISE_ID} --from-user alice "
        "--to-user bob --case-ids-file case-ids.txt --dry-run",
    ),
    "personal_tenant:retire": (
        "retire",
        "fm-personal-tenant retire --subject user_01HPLANTED",
    ),
    "personal_tenant:purge-idp-org": (
        "purge_idp_org",
        "fm-personal-tenant purge-idp-org --provider-org-id org_01HPLANTED",
    ),
    "personal_tenant:re-anchor": (
        "reanchor",
        "fm-personal-tenant re-anchor --subject user_01HPLANTED "
        f"--enterprise-id {ENTERPRISE_ID}",
    ),
}


#: The commands that compose no container and reach the database engine through
#: the process-global session factory: ``module`` -> its async function, an argv.
OPENS_ONLY_THE_ENGINE = {
    "provision_sso_org": (
        "provision",
        "fm-provision-sso-org --name Acme --slug acme --workos-org-id org_01H "
        "--domain acme.example",
    ),
    "set_turn_cap": (
        "set_turn_cap",
        f"fm-set-turn-cap --enterprise-id {ENTERPRISE_ID} "
        f"--organization-id {ORGANIZATION_ID} --show",
    ),
    "wipe_deployment": ("wipe_deployment", "fm-wipe-deployment"),
}


class _CommandFailed(Exception):
    pass


class _InitializeFailed(Exception):
    pass


@pytest.fixture
def container(monkeypatch):
    """The container a command composes, recording the loop of each call.

    The real singleton's ``shutdown`` is a spy, so a case can tell that a
    command which composed its own container left the singleton alone."""
    loops = {}

    def _on(name, raises=None):
        def _record(*_args, **_kwargs):
            loops[name] = asyncio.get_running_loop()
            if raises is not None:
                raise raises

        return _record

    stub = SimpleNamespace(
        loops=loops,
        on=_on,
        initialize=AsyncMock(side_effect=_on("initialize")),
        shutdown=AsyncMock(side_effect=_on("shutdown")),
        singleton_shutdown=AsyncMock(),
    )
    monkeypatch.setattr(DIContainer, "shutdown", stub.singleton_shutdown)
    return stub


@pytest.mark.parametrize(
    "outcome", ["succeeds", "fails", "raises", "initialize raises", "shutdown raises"]
)
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_command_shuts_the_container_down_on_the_loop_it_initialized_on(
    command, outcome, container, monkeypatch, caplog
):
    module_name = command.partition(":")[0]
    module = importlib.import_module(f"faultmaven.cli.{module_name}")
    work_name, argv = COMMANDS[command]
    # Where the command resolves its container: its own module-level name when it
    # binds one at import, the process singleton's name when it imports at use.
    binds_at_import = hasattr(module, "container")
    if binds_at_import:
        monkeypatch.setattr(module, "container", container)
    else:
        monkeypatch.setattr("faultmaven.container.container", container)
    if outcome == "initialize raises":
        container.initialize.side_effect = container.on(
            "initialize", raises=_InitializeFailed()
        )
    if outcome == "shutdown raises":
        container.shutdown.side_effect = container.on(
            "shutdown", raises=RuntimeError("the pool would not close")
        )
    succeeded = outcome != "fails"

    async def _work(*_args, **_kwargs):
        await container.initialize()
        if outcome == "raises":
            raise _CommandFailed()
        if module_name in _REPORTS_A_BOOL:
            return succeeded
        return 0 if succeeded else 1

    monkeypatch.setattr(module, work_name, _work)
    # The database gate reads settings, which this test does not set up.
    monkeypatch.setattr(module, "require_persistent_database_or_exit", lambda: None)
    monkeypatch.setattr(sys, "argv", argv.split())

    if outcome in ("raises", "initialize raises"):
        # Propagated as itself, which a console script exits 1 on.
        expected = _CommandFailed if outcome == "raises" else _InitializeFailed
        with pytest.raises(expected):
            module.main()
    else:
        # A failed step is logged by the teardown's step runner.
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.infrastructure.concurrency.teardown"
        ):
            with pytest.raises(SystemExit) as exited:
                module.main()
        # The command's own status; a failed shutdown does not change it.
        assert exited.value.code == (0 if succeeded else 1)
        if outcome == "shutdown raises":
            assert "Container shutdown failed" in caplog.text
            assert "the pool would not close" in caplog.text

    container.initialize.assert_awaited_once()
    container.shutdown.assert_awaited_once()
    assert container.loops["shutdown"] is container.loops["initialize"]
    if binds_at_import:
        container.singleton_shutdown.assert_not_awaited()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A database of the test's own, and each ``(engine, loop)`` a ``dispose()``
    ran on. The process-global engine is put back after."""
    disposals = []
    real_dispose = AsyncEngine.dispose

    async def _dispose(self, *args, **kwargs):
        disposals.append((self, asyncio.get_running_loop()))
        await real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", _dispose)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    return SimpleNamespace(
        url=f"sqlite+aiosqlite:///{tmp_path / 'faultmaven.db'}", disposals=disposals
    )


async def _open_the_engine(url):
    """What a run's stores do: open the engine on the running loop.

    Returns ``(engine, loop)``, as its disposal on this loop is recorded."""
    opened = database.get_engine(url)
    async with opened.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return opened, asyncio.get_running_loop()


@pytest.mark.parametrize("outcome", ["returns", "raises", "shutdown raises"])
def test_the_run_disposes_the_database_engine_the_command_opened(
    outcome, container, engine
):
    """#2254: the run shut the container down and left the engine its stores
    opened, so ``asyncio.run`` closed the loop over the engine's connections.
    The engine is disposed after the shutdown, on the run's loop, whatever the
    command or the shutdown did."""
    opened = []

    async def _command():
        opened.append(await _open_the_engine(engine.url))
        if outcome == "raises":
            raise _CommandFailed()
        return 0

    if outcome == "shutdown raises":
        container.shutdown.side_effect = RuntimeError("the pool would not close")
    if outcome == "raises":
        with pytest.raises(_CommandFailed):
            run_with_container(_command(), container=container)
    else:
        assert run_with_container(_command(), container=container) == 0

    assert len(opened) == 1, "the command did not open the engine"
    assert engine.disposals == opened, "not disposed, or not on the run's loop"
    assert database._engine is None, "the run left the database engine in place"
    container.shutdown.assert_awaited_once()


@pytest.mark.parametrize("command", sorted({**COMMANDS, **OPENS_ONLY_THE_ENGINE}))
def test_every_command_disposes_the_engine_before_its_loop_ends(
    command, container, engine, monkeypatch
):
    """#2254, through each command's own call site: three commands ran their
    work in a bare ``asyncio.run``, and none of the others' runs disposed the
    engine either. A command that composes nothing shuts no container down."""
    module_name = command.partition(":")[0]
    module = importlib.import_module(f"faultmaven.cli.{module_name}")
    work_name, argv = {**COMMANDS, **OPENS_ONLY_THE_ENGINE}[command]
    if hasattr(module, "container"):
        monkeypatch.setattr(module, "container", container)
    else:
        monkeypatch.setattr("faultmaven.container.container", container)
    opened = []

    async def _work(*_args, **_kwargs):
        opened.append(await _open_the_engine(engine.url))
        return True if module_name in _REPORTS_A_BOOL else 0

    monkeypatch.setattr(module, work_name, _work)
    monkeypatch.setattr(module, "require_persistent_database_or_exit", lambda: None)
    monkeypatch.setattr(sys, "argv", argv.split())

    with pytest.raises(SystemExit) as exited:
        module.main()

    assert exited.value.code == 0
    assert len(opened) == 1, "the command did not open the engine"
    assert engine.disposals == opened, "not disposed, or not on the run's loop"
    assert database._engine is None, "the run left the database engine in place"
    if module_name in OPENS_ONLY_THE_ENGINE:
        container.shutdown.assert_not_awaited()
        container.singleton_shutdown.assert_not_awaited()
    else:
        container.shutdown.assert_awaited_once()
