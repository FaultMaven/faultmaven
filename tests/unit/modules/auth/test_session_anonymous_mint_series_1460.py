"""#1460: every anonymous-mint series exports 0 from import, before any mint.

A labelled series created by its first increment appears already at 1, and
``increase()`` cannot see that first event (#1787). Step 3 of #1460 reads the
``bearer="absent"`` series over a deploy cycle, so a lost first event is a
false zero. Runs in a subprocess because the shim picks a real or a no-op
counter at import, from ``ENABLE_METRICS``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CHILD = r"""
import json
from prometheus_client import REGISTRY

from faultmaven.modules.auth.infrastructure.metrics import session_metrics

series = {}
for family in REGISTRY.collect():
    if family.name != "faultmaven_auth_session_anonymous_mints":
        continue
    for s in family.samples:
        if s.name.endswith("_total"):
            series[s.labels["bearer"] + "|" + s.labels["origin"]] = s.value
print("@@RESULT@@" + json.dumps({"file": session_metrics.__file__, "series": series}))
"""


def test_every_series_exists_at_zero_from_import():
    pytest.importorskip("prometheus_client")
    repo_root = Path(__file__).resolve().parents[4]
    env = dict(os.environ)
    env["ENABLE_METRICS"] = "true"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH")) if p
    )
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("@@RESULT@@"))
    result = json.loads(line[len("@@RESULT@@") :])
    assert result["file"].startswith(str(repo_root))
    assert result["series"] == {
        f"{bearer}|{origin}": 0.0
        for bearer in ("absent", "unresolved")
        for origin in ("extension", "web", "none")
    }
