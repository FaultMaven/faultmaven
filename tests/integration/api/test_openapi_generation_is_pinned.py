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


@pytest.mark.integration
@pytest.mark.slow
def test_generated_reference_ignores_the_ambient_environment():
    """`--check` passes even when every router-gating setting is hostile."""
    assert GENERATOR.exists(), f"generator not found at {GENERATOR}"

    environment = {**os.environ, **HOSTILE_ENVIRONMENT}

    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, (
        "The committed API reference changed under a hostile environment, so "
        "it is a function of configuration rather than of the code. Whatever "
        "setting leaked needs to be neutralised in the generator — pin it in "
        "PINNED_ENVIRONMENT, or (better) confirm the environment is being "
        "emptied rather than selectively overridden.\n\n"
        f"stdout:\n{result.stdout[-3000:]}\n\nstderr:\n{result.stderr[-2000:]}"
    )
