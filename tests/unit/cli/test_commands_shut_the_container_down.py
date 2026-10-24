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
each call ran on, and then returns, raises, or has ``initialize()`` raise after
it would have built the client. ``fm-personal-tenant`` is listed once per
operation, because each has its own call site; only ``retire`` composes, and the
shutdown of a container that was never initialized does nothing.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ORGANIZATION_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

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
    """The container every command resolves, recording the loop of each call."""
    loops = {}

    def _on(name, raises=None):
        def _record(*_args, **_kwargs):
            loops[name] = asyncio.get_running_loop()
            if raises is not None:
                raise raises

        return _record

    stub = SimpleNamespace(
        loops=loops,
        initialize=AsyncMock(side_effect=_on("initialize")),
        shutdown=AsyncMock(side_effect=_on("shutdown")),
    )
    stub.fail_initialize = lambda: setattr(
        stub,
        "initialize",
        AsyncMock(side_effect=_on("initialize", raises=_InitializeFailed())),
    )
    monkeypatch.setattr("faultmaven.container.container", stub)
    return stub


@pytest.mark.parametrize("outcome", ["returns", "raises", "initialize raises"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_command_shuts_the_container_down_on_the_loop_it_initialized_on(
    command, outcome, container, monkeypatch
):
    module = importlib.import_module(f"faultmaven.cli.{command.partition(':')[0]}")
    work_name, argv = COMMANDS[command]

    async def _work(*_args, **_kwargs):
        from faultmaven.container import container as resolved

        await resolved.initialize()
        if outcome == "raises":
            raise _CommandFailed()
        return 0

    monkeypatch.setattr(module, work_name, _work)
    # The database gate reads settings, which this test does not set up.
    monkeypatch.setattr(module, "require_persistent_database_or_exit", lambda: None)
    monkeypatch.setattr(sys, "argv", argv.split())
    if outcome == "initialize raises":
        container.fail_initialize()

    expected = {
        "returns": SystemExit,
        "raises": _CommandFailed,
        "initialize raises": _InitializeFailed,
    }[outcome]
    with pytest.raises(expected):
        module.main()

    container.initialize.assert_awaited_once()
    container.shutdown.assert_awaited_once()
    assert container.loops["shutdown"] is container.loops["initialize"]
