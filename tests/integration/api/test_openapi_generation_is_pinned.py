"""The generated API reference must not depend on the ambient environment.

Several routers are mounted conditionally — debug endpoints, the OAuth router,
the SSO router, ``/metrics``. If the generator inherits any of those settings
from whoever runs it, the same commit produces different documents on a laptop
and in CI, and the drift gate reports a configuration difference as a contract
change. A gate that fails for reasons unrelated to the code is a gate people
turn off.

``generate_api_docs.py`` handles this by emptying the environment down to the
keys the interpreter needs and then applying its own pinned set, rather than
overriding a list of known-relevant variables. That distinction is the point of
this test: an enumerated list is exactly what went stale the first time this
was written (``METRICS_EXPORTER`` was missed), so what is asserted here is the
outcome — hostile environment in, identical artifact out — and not the
mechanism.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = PROJECT_ROOT / "scripts" / "generate_api_docs.py"

# Every setting known to change which routers mount, each set to the opposite
# of what the generator pins, plus a `.env`-style value for good measure. If
# any of these reaches the app, the route table changes and `--check` fails.
HOSTILE_ENVIRONMENT = {
    "ENVIRONMENT": "development",
    "ENABLE_DEBUG_ENDPOINTS": "true",
    "OAUTH_ENABLED": "false",
    "AUTH_MODE": "local",
    "WORKOS_API_KEY": "",
    "WORKOS_CLIENT_ID": "",
    "WORKOS_REDIRECT_URI": "",
    "METRICS_EXPORTER": "none",
    "CORS_ALLOW_ORIGINS": '["http://localhost:3333"]',
}


# Keys the interpreter and its imports need, kept when the baseline run's
# environment is built. Everything else is DROPPED rather than enumerated —
# same discipline as the generator, and for the same reason: a denylist of
# "settings known to gate a router" is exactly what goes stale, and any such
# setting reaching the baseline run is what makes it lie (see below).
_SYSTEM_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PWD",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "COMSPEC",
    }
)


def _run_check(environment):
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.mark.integration
@pytest.mark.slow
def test_generated_reference_ignores_the_ambient_environment():
    """`--check` passes even when every router-gating setting is hostile."""
    assert GENERATOR.exists(), f"generator not found at {GENERATOR}"

    # Establish that this interpreter can reproduce the committed artifact at
    # all before asking whether a hostile environment changes it.
    #
    # The document is a function of the code AND of the installed FastAPI and
    # Pydantic, which decide how schemas are serialised (`format: binary` vs
    # `contentMediaType`, `ctx`/`input` on ValidationError). Run from a venv
    # that does not match requirements/*.txt, `--check` fails for that reason
    # alone — and this test used to report it as "a setting leaked", sending
    # the reader to hunt a configuration bug that does not exist. It cost a
    # whole PR (fm#1009), which regenerated the artifacts against a drifted
    # interpreter and was closed unmerged.
    #
    # Discriminating on the BASELINE rather than on marker strings in the diff
    # keeps this closed for the property under test: if the baseline passes,
    # any hostile-environment failure is a genuine leak and still fails. Only
    # the case where nothing could have passed is skipped.
    #
    # ⚠ The baseline runs under a NEUTRAL environment, not the ambient one.
    # Under ambient it inherits whatever the caller exported — and if the
    # generator ever stops emptying the environment, that leak reaches the
    # baseline too, the baseline fails, and this test SKIPS instead of failing:
    # fail-open on precisely the defect it exists to catch. That is not
    # hypothetical. Reproduced with `os.environ.clear()` removed from the
    # generator and `ENABLE_DEBUG_ENDPOINTS=true` exported — hostile-listed but
    # NOT in the generator's PINNED_ENVIRONMENT, so nothing overwrites it — and
    # the run skipped. CI's own jobs export settings of this shape, and `.env`
    # values reach os.environ, so the contaminated case is the normal one.
    baseline = _run_check(
        {k: v for k, v in os.environ.items() if k in _SYSTEM_ENVIRONMENT_KEYS}
    )
    if baseline.returncode != 0:
        pytest.skip(
            "this interpreter cannot reproduce the committed API reference, so "
            "the hostile-environment comparison would be meaningless. That is "
            "an environment problem, not a generator or contract one: the "
            "document depends on the installed FastAPI/Pydantic as well as on "
            "the code. Re-run from a venv synced to the lockfile CI installs:\n"
            "    ./scripts/sync-venv.sh dev\n"
            "    .venv-dev/bin/python -m pytest " + __file__ + "\n"
            "Do NOT regenerate the artifacts to make this pass — that commits "
            "a document matching your local libraries and breaks the drift "
            "gate, which installs requirements/dev.txt.\n\n"
            # stderr as well as stdout: a generator that dies on an ImportError
            # writes nothing to stdout, and a skip whose diagnosis is blank
            # while asserting "your venv drifted" is its own wrong answer.
            f"baseline stdout:\n{baseline.stdout[-2000:]}\n\n"
            f"baseline stderr:\n{baseline.stderr[-2000:]}"
        )

    environment = {**os.environ, **HOSTILE_ENVIRONMENT}
    result = _run_check(environment)

    assert result.returncode == 0, (
        "The committed API reference changed under a hostile environment, so "
        "it is a function of configuration rather than of the code. Whatever "
        "setting leaked needs to be neutralised in the generator — pin it in "
        "PINNED_ENVIRONMENT, or (better) confirm the environment is being "
        "emptied rather than selectively overridden.\n\n"
        f"stdout:\n{result.stdout[-3000:]}\n\nstderr:\n{result.stderr[-2000:]}"
    )
