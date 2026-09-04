"""A test fixture must not repoint the whole process at its own database.

Three engine fixtures here assigned ``DATABASE_URL`` into ``os.environ``
outright, from *function*-scoped fixtures that never put it back. The blast
radius was the rest of the pytest process, and ``tests/integration`` runs
before ``tests/unit`` in a whole-suite run.

It stayed invisible because ``get_settings()`` caches a process-wide singleton:
the one built before these tests ran kept the real URL, so nothing downstream
read the leaked value. The moment anything reset that singleton the rebuild
picked it up, and the application booted against an empty in-memory database —
``RuntimeError: Critical bootstrap failure: no such table: enterprises`` out of
``bootstrap_application``, in ``tests/unit/api/test_composition_root.py``, which
has nothing to do with any of this (fm#1325).

**Why this is measured rather than grepped.** The first version of this guard
scanned ``tests/`` for the assignment, which is cheap and total and would cover
modules nobody has written yet. It was wrong: three sibling modules
(``security/test_personal_tenant_provisioning.py``,
``security/test_tenant_turn_cap.py``, ``security/test_two_tenant_surface_probe.py``)
assign the same variable *legitimately* and restore it wholesale in teardown,
with a documented reason — the ``-m postgres`` lane's sibling modules need the
superuser URL back. A syntactic guard flags all three, and the only way to
silence it is an allowlist naming them, which fails open on the next file that
gets added to it. So the guard asks the question that actually matters — what is
the process holding when the module is done — and asks it of a real session.

The cost is one subprocess per subject module. That is deliberate: the state
under measurement is process-wide, and this test's own process has already been
shaped by everything that ran before it, so asking here would answer about the
wrong process.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.integration]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: A URL nothing else in the suite uses, so "the process still holds this"
#: cannot be true by coincidence, and no default satisfies it. Never connected
#: to — only set, and read back.
SENTINEL_URL = "sqlite+aiosqlite:///./data/sentinel-must-survive.db"

#: One real test out of each module whose engine fixture sets ``DATABASE_URL``.
#: A live test rather than a reconstruction of what the fixture does: the
#: subject is the fixture as the suite actually runs it, including every other
#: fixture layered on top of it.
SUBJECTS = {
    "case_repository": (
        "tests/integration/test_case_repository_integration.py"
        "::test_full_case_lifecycle"
    ),
    "investigation_session": (
        "tests/integration/test_investigation_session_integration.py"
        "::test_full_session_lifecycle"
    ),
    "knowledge_item": (
        "tests/integration/test_knowledge_item_integration.py::test_full_item_lifecycle"
    ),
}

#: Runs INSIDE the subprocess. ``pytest_sessionfinish`` fires after every
#: teardown has run, which is the far edge of the window the leak used to escape
#: from. Both facts are reported: the variable itself, and what a settings
#: object resolves to now — the second is the one that reaches the application,
#: and a fixture that restored the variable but left a stale singleton cached
#: would satisfy the first and fail the second.
PROBE = """
import os
import sys

import pytest


class Probe:
    def pytest_sessionfinish(self, session, exitstatus):
        import faultmaven.config.settings as settings_module

        sys.stderr.write("PROBE_ENV=%r\\n" % os.environ.get("DATABASE_URL"))
        sys.stderr.write(
            "PROBE_RESOLVED=%r\\n" % settings_module.get_settings().database.database_url
        )
        sys.stderr.flush()


sys.exit(
    pytest.main([{subject!r}, "-q", "-p", "no:cacheprovider"], plugins=[Probe()])
)
"""


def _reported(stderr: str, key: str) -> str:
    match = re.search(rf"^{key}=(.*)$", stderr, re.MULTILINE)
    assert match, f"the probe did not report {key}. stderr:\n{stderr[-4000:]}"
    return match.group(1)


@pytest.mark.slow
@pytest.mark.parametrize("subject", sorted(SUBJECTS), ids=sorted(SUBJECTS))
def test_the_engine_fixtures_leave_the_process_as_they_found_it(tmp_path, subject):
    """Run one real test from the module, then read the process it left behind.

    ``DATABASE_URL`` goes in as a sentinel so the assertion is "still exactly
    what we set", which nothing satisfies by accident. Parametrised over all
    three modules rather than probing one and assuming the others: they were
    three copies of one fixture, which is exactly the shape in which a fix lands
    in two of them.
    """
    probe = tmp_path / "probe_run.py"
    probe.write_text(PROBE.format(subject=SUBJECTS[subject]), encoding="utf-8")

    env = dict(os.environ)
    env["DATABASE_URL"] = SENTINEL_URL
    env["SKIP_SERVICE_CHECKS"] = "true"

    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert result.returncode == 0, (
        "the subject test itself failed, so this measures nothing:\n"
        f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    )

    assert _reported(result.stderr, "PROBE_ENV") == repr(SENTINEL_URL), (
        f"{SUBJECTS[subject]} left DATABASE_URL pointing somewhere else, so every "
        "later test in that process inherits it"
    )
    assert _reported(result.stderr, "PROBE_RESOLVED") == repr(SENTINEL_URL), (
        f"after {SUBJECTS[subject]} the settings resolve to a different database "
        "than the process was started with"
    )
