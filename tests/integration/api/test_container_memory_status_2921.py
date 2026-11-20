"""``GET /admin/config/status`` reports ``container_memory`` from a booted app (#2921).

Pin 3 of #2596 S2: the jobs' address-space caps over the container's limit are
reported on the route and warned once at startup, and the app starts and
serves under every cgroup state, a failed check included: ruling (c), never
refused. The parse matrix and the formula pins are the unit tests in
``tests/unit/api/test_container_memory_status_2921.py``.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from faultmaven.infrastructure import model_cache

pytestmark = pytest.mark.integration

GIB = 1024**3

_WARNING = "The jobs' address-space caps"

#: Every cgroup state the app must start and serve under: its files ({path:
#: text, or None for a directory there}) and the flag the route reports.
_STATES = {
    "over": ({"memory.max": f"{8 * GIB}\n", "memory.oom.group": "1\n"}, True),
    "absent": ({}, None),
    "invalid": ({"memory.max": "garbage\n", "memory.oom.group": "1\n"}, None),
    "unreadable": ({"memory.max": None}, None),
}


def _cgroup(root, files):
    for rel, text in files.items():
        if text is None:
            (root / rel).mkdir()
        else:
            (root / rel).write_text(text)
    return str(root)


def _status(client):
    login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
    assert login.status_code == 200, login.text
    return client.get(
        "/api/v1/admin/config/status",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )


def _warnings(caplog):
    return [r for r in caplog.records if r.getMessage().startswith(_WARNING)]


@pytest.mark.parametrize("state", list(_STATES))
def test_the_app_starts_serves_and_reports_under_every_cgroup_state(
    state, tmp_path, monkeypatch, caplog, unshared_app_boot
):
    from faultmaven.main import app

    files, exceeds = _STATES[state]
    monkeypatch.setattr(model_cache, "CGROUP_ROOT", _cgroup(tmp_path, files))
    caplog.set_level(logging.WARNING, logger="faultmaven.bootstrap.lifespan")

    with TestClient(app) as client:
        response = _status(client)

    assert response.status_code == 200, response.text
    memory = response.json()["container_memory"]
    assert memory["job_caps_exceed_limit"] is exceeds
    warnings = _warnings(caplog)
    if exceeds:
        assert memory["memory_max_bytes"] == 8 * GIB
        assert memory["oom_group"] is True
        assert memory["headroom_bytes"] == 8 * GIB - memory["job_caps_bytes"] < 0
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        # The caps, never "memory" or a predicted OOM.
        assert not {"memory", "oom"} & set(warnings[0].getMessage().lower().split())
    else:
        assert memory["cgroup_source"] == state
        assert (memory["memory_max_bytes"], memory["headroom_bytes"]) == (None, None)
        assert warnings == []


def test_the_app_starts_and_serves_when_the_check_raises(
    monkeypatch, caplog, unshared_app_boot
):
    """The startup check is guarded: its failure is logged by class alone."""
    from faultmaven.api import dependencies
    from faultmaven.main import app

    def _raises(settings):
        raise RuntimeError("a secret-bearing message")

    monkeypatch.setattr(dependencies, "container_memory_status", _raises)
    caplog.set_level(logging.WARNING, logger="faultmaven.bootstrap.lifespan")

    with TestClient(app) as client:
        login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})

    assert login.status_code == 200, login.text
    [logged] = _warnings(caplog)
    assert "RuntimeError" in logged.getMessage()
    assert "secret" not in logged.getMessage()
