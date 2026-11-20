"""``container_memory`` on ``GET /admin/config/status`` (#2921, #2596 S2).

Principle 14, *Container memory*: the worst-overlap formula's bounded terms,
the jobs' address-space caps, beside the container's cgroup memory limit.
Pin 1: the cgroup parse matrix. Pin 2: each term moves the figure by exactly
its share, asserted as literals from the design text's worked figures. Pin 3:
caps over the limit are reported and warned once, and the app still starts
and serves under every cgroup state (ruling (c): never refused).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from faultmaven.api.dependencies import container_memory_status
from faultmaven.infrastructure import model_cache
from faultmaven.infrastructure.model_cache import CgroupMemory, cgroup_memory
from faultmaven.modules.preprocessing import preprocessing_service

pytestmark = pytest.mark.unit

MIB = 1024 * 1024
GIB = 1024 * MIB


def _tree(root, files):
    """Write ``files`` ({relative path: text, or None for a directory there})."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.mkdir()
        else:
            path.write_text(text)
    return root


# -- pin 1: the parse matrix -------------------------------------------------


@pytest.mark.parametrize(
    "files, expected",
    [
        (
            {"memory.max": "8589934592\n", "memory.oom.group": "0\n"},
            CgroupMemory(8589934592, False, "cgroup_v2"),
        ),
        (
            {"memory.max": "max\n", "memory.oom.group": "1\n"},
            CgroupMemory(None, True, "cgroup_v2"),
        ),
        (
            {"memory.max": "4294967296\n", "memory.oom.group": "1\n"},
            CgroupMemory(4294967296, True, "cgroup_v2"),
        ),
        (
            {"memory/memory.limit_in_bytes": "2147483648\n"},
            CgroupMemory(2147483648, None, "cgroup_v1"),
        ),
        (
            {"memory/memory.limit_in_bytes": "9223372036854771712\n"},
            CgroupMemory(None, None, "cgroup_v1"),
        ),
        ({}, CgroupMemory(None, None, "absent")),
        ({"memory.max": None}, CgroupMemory(None, None, "unreadable")),
        (
            {"memory.max": "1024\n", "memory.oom.group": None},
            CgroupMemory(None, None, "unreadable"),
        ),
        (
            {"memory.max": "lots\n", "memory.oom.group": "1\n"},
            CgroupMemory(None, None, "invalid"),
        ),
        (
            {"memory.max": "1024\n", "memory.oom.group": "2\n"},
            CgroupMemory(None, None, "invalid"),
        ),
        (
            {"memory/memory.limit_in_bytes": "-1\n"},
            CgroupMemory(None, None, "invalid"),
        ),
        (
            {
                "memory.max": "garbage\n",
                "memory.oom.group": "0\n",
                "memory/memory.limit_in_bytes": "2147483648\n",
            },
            CgroupMemory(None, None, "invalid"),
        ),
    ],
    ids=[
        "v2-number",
        "v2-max",
        "v2-oom-group-1",
        "v1-number",
        "v1-kernel-no-limit",
        "absent",
        "unreadable-memory-max",
        "unreadable-oom-group",
        "invalid-v2",
        "invalid-oom-group",
        "invalid-v1",
        "invalid-v2-does-not-fall-through-to-v1",
    ],
)
def test_the_cgroup_parse_matrix(tmp_path, monkeypatch, files, expected):
    monkeypatch.setattr(model_cache, "CGROUP_ROOT", str(_tree(tmp_path, files)))

    assert cgroup_memory() == expected


# -- pin 2: the terms move the figure ---------------------------------------


def _settings(upload_mb=10, workers=1, reload=False):
    return SimpleNamespace(
        server=SimpleNamespace(workers=workers, reload=reload),
        upload=SimpleNamespace(max_upload_size_mb=upload_mb),
    )


@pytest.fixture
def two_cores(tmp_path, monkeypatch):
    """Tier 1 at two slots, and a container with no cgroup files."""
    monkeypatch.setattr(preprocessing_service, "TIER1_SLOTS", 2)
    monkeypatch.setattr(model_cache, "CGROUP_ROOT", str(tmp_path))


def test_the_worked_figures_at_ten_mb_one_process_two_cores(two_cores):
    status = container_memory_status(_settings())

    # 1,536 + 4,992 + 1,024 + 2,048 + 12,288 MiB.
    assert status.pools_bytes + status.encode_bytes == 21_888 * MIB
    # 2 x 5 + 2 x (2 x 30 + 61) + 4 x (2 x 0.76 + 10) + 2 x (2 x 10 + 1) MiB:
    # 340.10 MiB, the gate's texts 2 x 2 x 400,000 bytes each.
    assert status.buffers_bytes == 356_624_384
    assert status.job_caps_bytes == 21_888 * MIB + 356_624_384
    assert (status.serving_processes, status.upload_limit_mb, status.tier1_slots) == (
        1,
        10,
        2,
    )
    assert status.unbounded_terms == ["api", "encode_buffers"]
    assert status.encode_without_host_lock_bytes == 12 * GIB


def test_each_further_upload_mb_adds_636_mib(two_cores):
    at_10 = container_memory_status(_settings(upload_mb=10))
    at_11 = container_memory_status(_settings(upload_mb=11))

    # Caps 2 x 64 + 2 x 240 = 608 MiB; buffers 2 x (6 + 6) + 2 x 2 = 28 MiB.
    assert at_11.job_caps_bytes - at_10.job_caps_bytes == 636 * MIB


def test_a_second_serving_process_adds_its_pools_buffers_and_the_supervisor(
    two_cores,
):
    one = container_memory_status(_settings(workers=1))
    two = container_memory_status(_settings(workers=2))

    # One process's pools, 9,600 MiB, and its buffers; the encode stays once.
    assert two.job_caps_bytes - one.job_caps_bytes == 9_600 * MIB + 356_624_384
    assert two.unbounded_terms == ["api", "supervisor", "encode_buffers"]
    assert two.encode_without_host_lock_bytes == 24 * GIB
    assert container_memory_status(_settings(reload=True)).unbounded_terms == [
        "api",
        "supervisor",
        "encode_buffers",
    ]


# -- pin 3: over the limit, reported and served -----------------------------

_WARNING = "The jobs' address-space caps"

#: Every cgroup state the app must start and serve under, and what it reports.
_STATES = {
    "over": (
        {"memory.max": f"{8 * GIB}\n", "memory.oom.group": "1\n"},
        True,
    ),
    "absent": ({}, None),
    "invalid": ({"memory.max": "garbage\n", "memory.oom.group": "1\n"}, None),
    "unreadable": ({"memory.max": None}, None),
}


@pytest.mark.parametrize("state", list(_STATES))
def test_the_app_starts_serves_and_reports_under_every_cgroup_state(
    state, tmp_path, monkeypatch, caplog, unshared_app_boot
):
    from fastapi.testclient import TestClient

    from faultmaven.main import app

    files, exceeds = _STATES[state]
    monkeypatch.setattr(model_cache, "CGROUP_ROOT", str(_tree(tmp_path, files)))
    caplog.set_level(logging.WARNING, logger="faultmaven.bootstrap.lifespan")

    with TestClient(app) as client:
        login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
        assert login.status_code == 200, login.text
        response = client.get(
            "/api/v1/admin/config/status",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

    assert response.status_code == 200, response.text
    memory = response.json()["container_memory"]
    assert memory["job_caps_exceed_limit"] is exceeds
    warnings = [r for r in caplog.records if r.getMessage().startswith(_WARNING)]
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
