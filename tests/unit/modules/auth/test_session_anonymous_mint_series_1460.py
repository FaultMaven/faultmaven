"""#1460: every anonymous-mint series exports 0 from import, before any mint.

A labelled series created by its first increment appears already at 1, and
``increase()`` cannot see that first event (#1787). Step 3 of #1460 reads the
``bearer="absent"`` series over a deploy cycle, so a lost first event is a
false zero. Runs in a subprocess because the shim picks a real or a no-op
counter at import, from ``ENABLE_METRICS``.

**The child's environment is explicit**, as in
``test_revocation_watermark_gauge_is_scraped.py``: nothing is inherited from
``os.environ``, so a ``PROMETHEUS_MULTIPROC_DIR`` in the parent cannot switch
the child into multiprocess mode. Its cwd is a temp directory, which holds no
``.env`` to discover.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_MARKER = "@@RESULT@@"

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


def test_every_series_exists_at_zero_from_import(tmp_path):
    pytest.importorskip("prometheus_client")
    repo_root = Path(faultmaven.__file__).resolve().parent.parent
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(repo_root),
        "ENABLE_METRICS": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(_MARKER))
    result = json.loads(line[len(_MARKER) :])
    assert result["file"].startswith(str(repo_root))
    assert result["series"] == {
        f"{bearer}|{origin}": 0.0
        for bearer in ("absent", "unresolved")
        for origin in (
            "chrome-extension",
            "moz-extension",
            "safari-web-extension",
            "web",
            "none",
        )
    }
