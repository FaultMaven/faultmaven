"""Installs the suite's network guard in a child process a test starts (#2975).

``tests/child_process.child_env`` puts this directory on the child's
``PYTHONPATH`` while the test process runs guarded, and Python imports
``sitecustomize`` at start-up, before the child's own code. It also installs
the offline tiktoken loader, allows the ports the parent bound, and reports the
guard's refusals on stderr at exit, marked nested in a process the child started
with its environment (``tests/child_process.py``).

It fails closed: CPython reports a failed ``sitecustomize`` and runs the child
anyway, unguarded, so any failure here ends the child with status 97 instead.
"""

import os
import sys


def _install() -> None:
    import atexit
    import json

    from tests import network_guard, offline_tiktoken
    from tests.child_process import BOUND_PORTS_ENV, NESTED_ENV, REFUSALS_MARKER

    guard = network_guard.install()
    if guard is None:
        raise RuntimeError("the network guard did not install")
    for port in filter(None, os.environ.get(BOUND_PORTS_ENV, "").split(",")):
        guard.note_bound(int(port))
    offline_tiktoken.install()
    nested = bool(os.environ.get(NESTED_ENV))
    os.environ[NESTED_ENV] = "1"  # inherited: what this process starts is nested

    def report() -> None:
        body = {"nested": nested, "refusals": guard.drain_refusals()}
        os.write(2, (REFUSALS_MARKER + json.dumps(body) + "\n").encode())

    atexit.register(report)


try:
    _install()
except BaseException as exc:
    sys.stderr.write(f"tests/child_site/sitecustomize.py: {exc!r}\n")
    sys.stderr.flush()
    os._exit(97)
