"""A child started with ``child_env`` runs offline and under the network guard (#2975).

The default-database boot probe's child had neither, and downloaded BGE-M3 from
huggingface.co on every run. The probe below looks the hub's name up only once
the guard is installed in it, so no run of this test leaves the machine, even
one where ``child_env`` regressed.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests import network_guard
from tests.child_process import child_env

pytestmark = pytest.mark.unit

_PROBE = """
import json, os, socket, sys

guard = sys.modules.get("tests.network_guard")
result = {
    "sitecustomize": getattr(sys.modules.get("sitecustomize"), "__file__", None),
    "guarded": guard is not None and guard.GUARD is not None,
    "env": {k: os.environ.get(k) for k in (
        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "FAULTMAVEN_SKIP_DOTENV")},
    "refusals": {},
}
if result["guarded"]:
    for via, call in (
        ("getaddrinfo", lambda: socket.getaddrinfo("huggingface.co", 443)),
        ("create_connection", lambda: socket.create_connection(("huggingface.co", 443))),
    ):
        try:
            call()
            result["refusals"][via] = None
        except Exception as exc:
            result["refusals"][via] = type(exc).__name__
print(json.dumps(result))
"""


def _probe(env: dict[str, str], tmp_path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.splitlines()[-1])


def test_the_guard_bites_in_the_child(tmp_path):
    assert network_guard.GUARD is not None, "this process must run guarded"
    result = _probe(child_env(tmp_path), tmp_path)

    assert result["sitecustomize"].endswith("child_site/sitecustomize.py"), result
    assert result["guarded"] is True, result
    assert result["refusals"] == {
        "getaddrinfo": "NameResolutionBlocked",
        "create_connection": "NetworkAccessBlocked",
    }


def test_the_offline_flags_reach_the_child(tmp_path):
    result = _probe(child_env(tmp_path), tmp_path)

    assert result["env"] == {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "FAULTMAVEN_SKIP_DOTENV": "1",
    }


def test_control_an_unguarded_parent_starts_an_unguarded_child(tmp_path, monkeypatch):
    """The ``-m real_llm`` lane's shape, and the control for the guard test:
    without the ``sitecustomize`` the child loads no guard. Asserted on what it
    imported, never by a lookup that would leave the machine."""
    monkeypatch.setattr(network_guard, "GUARD", None)
    result = _probe(child_env(tmp_path), tmp_path)

    assert result["sitecustomize"] is None, result
    assert result["guarded"] is False
    assert result["refusals"] == {}
