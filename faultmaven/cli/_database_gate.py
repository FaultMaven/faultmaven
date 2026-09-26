"""The persistent-database gate every operator command shares (#1659).

Every ``fm-*`` command acts on a deployment's database. Seven reach it through
the DI container and three through the engine directly, and none of them
checked that a database was configured. On an empty or in-memory
``DATABASE_URL`` the container composes its in-memory stores (a test seam,
fm#1647), so ``fm-provision-service-account`` printed "✅ Created service
account" and a refresh token for an account that vanished with the process.
``fm-promote-platform-admin`` reported "User 'admin' not found", naming the
wrong cause. The rest died three layers down on a URL parse error, after they
had created ``data/`` and a Chroma file in the working directory.

The API lifespan and the jobs runner refuse such a URL at boot through
:func:`faultmaven.config.persistent_database.require_persistent_database`. This
module applies the same rule and message to the operator commands, with the
exit an operator command owes its caller: exit 1 and the message on stderr, not
a traceback. Stdout stays clean, so ``--token-only > token.txt`` captures
nothing.

Each command's ``main()`` calls it once, after its arguments are parsed and
validated and before its first ``asyncio.run``. Nothing that reads the database
or composes the container runs ahead of it. It is a call in ``main()``, not a
check inside ``container.initialize()``, for two reasons. Three commands never
compose the container: ``fm-set-turn-cap``, ``fm-provision-sso-org`` and
``fm-wipe-deployment`` open sessions or the engine directly, and
``fm-reset-kb`` deletes its rows before it composes anything. And the container
composes over an in-memory URL on purpose in the unit suite.
``tests/unit/cli/test_database_gate_census.py`` pins that every declared
command calls it.
"""

from __future__ import annotations

import sys


def require_persistent_database_or_exit() -> None:
    """Exit 1 with the boot gate's message unless a persistent database is configured.

    Reads ``DATABASE_URL`` through ``get_settings()``, the same settings every
    command reads afterwards, so the gate cannot judge a different URL from
    the one the command would have used.
    """
    from faultmaven.config.persistent_database import (
        NonPersistentDatabaseError,
        require_persistent_database,
    )
    from faultmaven.config.settings import get_settings

    try:
        require_persistent_database(get_settings())
    except NonPersistentDatabaseError as exc:
        print(f"❌ Refusing to run: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
