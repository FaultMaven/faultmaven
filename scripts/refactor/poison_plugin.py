"""Fail a test if it ever reaches a target that must stay unreached.

PROVES/DOES: a dynamic adjudication of a single audit_patches.py finding — is
a reader really unreachable through a given patch target, in an actual test
run, not just in the static reader graph. Replaces the named target with a
call-recording double for the duration of the test and asserts, at fixture
teardown, that the double was never called (including through a `.inc()` /
`.labels()` chain, the common shape for a Prometheus counter/histogram).

WHY: audit_patches.py's FLAG/INERT verdicts are a hypothesis about the static
reader graph — which modules import ``name`` into their own globals — not a
proof about what a particular test actually executes. A poison double turns
"this reader should never be reached by this patch" into something a real
test run either violates or doesn't. Pair it ALWAYS with a positive control:
one run where the poisoned target IS reached, asserting that run fails; one
run where the code under test does not reach it, asserting that run passes.
An assertion with no observed failure mode is unfalsifiable and proves
nothing — a poison check that never once caught a planted reach is exactly
that.

USAGE (loaded as a pytest plugin, by module name, with its directory on
PYTHONPATH so `-p` can import it):
  PYTHONPATH=scripts/refactor POISON_TARGET="pkg.mod.name" POISON_KIND=async \\
      pytest -p poison_plugin path/to/test_file.py

  POISON_TARGET (required): dotted patch target, exactly as `unittest.mock.
  patch(...)` would take it — the attribute this run must never reach.
  POISON_KIND (optional, default "async"): "async" builds an AsyncMock double;
  anything else builds a plain MagicMock.

EXIT CODES: pytest's own. 0 means every test passed AND no poisoned target
was reached; 1 means some test failed, which includes the assertion this
plugin adds at teardown ("POISON reached: ...").
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _poison_recording():
    target = os.environ["POISON_TARGET"]
    kind = os.environ.get("POISON_KIND", "async")
    bomb = AsyncMock() if kind == "async" else MagicMock()
    with patch(target, bomb):
        yield
    reached = bomb.called or bomb.inc.called or bomb.labels.called
    assert not reached, f"POISON reached: {target}"
