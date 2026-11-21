"""A child started with ``child_env`` runs offline and under the network guard (#2975).

The default-database boot probe's child had neither, and downloaded BGE-M3 from
huggingface.co on every run. The probe asserts the guard's patching and asks the
guard's policy about the hub's name (``check_lookup``, which looks nothing up),
so no run of this test can leave the machine, even one where the guard
regressed.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest

from tests import network_guard
from tests.child_process import (
    GUARDED_SITE,
    assert_no_child_refusals,
    child_env,
    child_refusals,
)

pytestmark = pytest.mark.unit

guarded_parent = pytest.mark.skipif(
    network_guard.GUARD is None, reason="the suite runs unguarded"
)

_PROBE = """
import json, os, socket, sys

guard = getattr(sys.modules.get("tests.network_guard"), "GUARD", None)
result = {
    "sitecustomize": getattr(sys.modules.get("sitecustomize"), "__file__", None),
    "guarded": guard is not None,
    "env": {k: os.environ.get(k) for k in (
        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "FAULTMAVEN_SKIP_DOTENV")},
}
if guard is not None:
    result["getaddrinfo"] = socket.getaddrinfo.__qualname__
    result["bound_ports"] = sorted(guard.bound_ports)
    try:  # a refusal the child's code swallows
        guard.check_lookup("huggingface.co", "probe")
        result["refused"] = None
    except Exception as exc:
        result["refused"] = type(exc).__name__
print(json.dumps(result))
"""


def _probe(env: dict[str, str], tmp_path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _result(completed: subprocess.CompletedProcess) -> dict:
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.splitlines()[-1])


@guarded_parent
def test_the_guard_bites_in_the_child_and_its_refusal_is_reported(tmp_path):
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
        completed = _probe(child_env(tmp_path), tmp_path)
    result = _result(completed)

    assert result["sitecustomize"] == str(GUARDED_SITE / "sitecustomize.py")
    assert result["guarded"] is True
    assert result["getaddrinfo"] == "install.<locals>.getaddrinfo"
    assert result["refused"] == "NameResolutionBlocked"
    assert port in result["bound_ports"], "a port the parent bound is the child's"
    # Caught in the child, and still reported, so the test fails on it.
    [refusal] = child_refusals(completed.stderr)
    assert refusal.startswith("huggingface.co (name lookup) via probe")
    with pytest.raises(AssertionError, match="huggingface.co"):
        assert_no_child_refusals(completed.stderr)


def test_the_offline_flags_reach_the_child(tmp_path):
    result = _result(_probe(child_env(tmp_path), tmp_path))

    assert result["env"] == {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "FAULTMAVEN_SKIP_DOTENV": "1",
    }


@guarded_parent
def test_a_child_that_cannot_install_the_guard_does_not_run(tmp_path):
    """Without the repository on its path the hook's import fails: the child
    exits 97 before its own code, never running unguarded."""
    env = child_env(tmp_path, PYTHONPATH=str(GUARDED_SITE))
    completed = _probe(env, tmp_path)

    assert completed.returncode == 97, completed.stderr[-3000:]
    assert completed.stdout == ""
    assert "tests/child_site/sitecustomize.py" in completed.stderr


def test_control_an_unguarded_parent_starts_an_unguarded_child(tmp_path, monkeypatch):
    """The ``-m real_llm`` lane's shape, and the control for the guard test:
    without the ``sitecustomize`` the child loads no guard."""
    monkeypatch.setattr(network_guard, "GUARD", None)
    completed = _probe(child_env(tmp_path), tmp_path)
    result = _result(completed)

    assert result["sitecustomize"] is None, result
    assert result["guarded"] is False
    assert child_refusals(completed.stderr) == []
