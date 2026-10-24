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
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.container import DIContainer

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ORGANIZATION_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

#: The commands whose async function reports success as a bool, which ``main()``
#: turns into the exit status. The others return the status itself.
_REPORTS_A_BOOL = {
    "demote_platform_admin",
    "promote_platform_admin",
    "provision_service_account",
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
        with caplog.at_level(logging.WARNING, logger="faultmaven.cli._container"):
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
