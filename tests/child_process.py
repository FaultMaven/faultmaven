"""The environment of a child process a test starts to import ``faultmaven`` (#2975).

"No test reaches a network or a model server" (CLAUDE.md), but its guard
(``tests/network_guard.py``) patches the pytest process only, and each
child-boot test built its child's environment by hand. One lacked
``HF_HUB_OFFLINE``, so its lifespan downloaded BGE-M3 from huggingface.co on
every run: ~256 s of a 316 s boot, under a 600 s timeout.

``child_env`` is the one base: offline, as the image runs, no ``.env``, this
tree's ``faultmaven``; and, while the test process runs guarded, the
``sitecustomize`` in ``tests/child_site/`` installs the same guard in the child
and the offline tiktoken loader, as ``tests/conftest.py`` does. The child's
guard allows what its environment names and the ports this process had bound
when ``child_env`` was called (``BOUND_PORTS_ENV``). It reports its refusals at
exit, behind ``REFUSALS_MARKER`` on stderr, and the test asserts there are none
(``assert_no_child_refusals``): a refusal the child's code swallowed fails the
test, as in this process. A process the child starts with its environment (the
boot's migration subprocess) loads the same hook and reports too, tagged as
nested (``NESTED_ENV``); every report counts, and the child's own must be there.
The ``-m real_llm`` lane runs both unguarded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests import network_guard

#: The tree the test process imports ``faultmaven`` (and ``tests``) from.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Holds the ``sitecustomize`` that installs the network guard in the child.
GUARDED_SITE = Path(__file__).resolve().parent / "child_site"

#: Ports this process bound, comma-separated: the child's guard allows them.
BOUND_PORTS_ENV = "FM_TEST_PARENT_BOUND_PORTS"

#: Starts the stderr line on which a guard lists its refusals at exit.
REFUSALS_MARKER = "FM-CHILD-NETWORK-REFUSALS "

#: Set by the hook for the processes the child starts: their reports are nested.
NESTED_ENV = "FM_TEST_CHILD_GUARD_NESTED"


def child_env(home: Path | str, **overrides: str) -> dict[str, str]:
    """A child's whole environment: the base, then the caller's ``overrides``.

    Nothing is inherited from ``os.environ``; a caller that wants the parent's
    variables passes them. Bind a port before calling this to allow it.
    """
    path = [str(REPO_ROOT)]
    guarded = {}
    if network_guard.GUARD is not None:
        path.append(str(GUARDED_SITE))
        ports = sorted(network_guard.GUARD.bound_ports)
        guarded[BOUND_PORTS_ENV] = ",".join(map(str, ports))
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(path),
        "FAULTMAVEN_SKIP_DOTENV": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        # The parent's Redis, which the suite's steps leave empty
        # (tests/conftest.py): unset, the client connects to the default host
        # (faultmaven-redis-master) first.
        "REDIS_HOST": os.environ.get("REDIS_HOST", ""),
        **guarded,
        **overrides,
    }


def child_refusals(stderr: str) -> list[str]:
    """What the child's guard, and those of the processes it started, refused.

    Fails unless the child itself reported exactly once when it ran guarded: a
    missing report means it died before exit or its guard was never installed.
    """
    reports = [
        json.loads(line[len(REFUSALS_MARKER) :])
        for line in stderr.splitlines()
        if line.startswith(REFUSALS_MARKER)
    ]
    own = sum(1 for report in reports if not report["nested"])
    assert own == (0 if network_guard.GUARD is None else 1), (
        f"the child's network guard reported {own} times at exit, not once:\n"
        + stderr[-3000:]
    )
    return [refusal for report in reports for refusal in report["refusals"]]


def assert_no_child_refusals(stderr: str) -> None:
    """A child's refused connect or lookup fails the test, even if it was caught."""
    refusals = child_refusals(stderr)
    assert (
        refusals == []
    ), "a test's child process attempted network connections:\n" + "\n".join(refusals)
