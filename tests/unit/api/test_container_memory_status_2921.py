"""``container_memory`` on ``GET /admin/config/status`` (#2921, #2596 S2).

Principle 14, *Container memory*: the worst-overlap formula's bounded terms,
the jobs' address-space caps, beside the container's cgroup memory limit.
Pin 1: the cgroup parse matrix. Pin 2: each term moves the figure by exactly
its share, asserted as literals from the design text's worked figures. Pin 3,
the app booted under each cgroup state, is the route test
``tests/integration/api/test_container_memory_status_2921.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.api.dependencies import container_memory_status
from faultmaven.infrastructure import model_cache
from faultmaven.infrastructure.model_cache import CgroupMemory, cgroup_memory
from faultmaven.modules.preprocessing import preprocessing_service

pytestmark = pytest.mark.unit

MIB = 1024 * 1024
GIB = 1024 * MIB


#: A ``files`` value: a symlink at the path to a file that does not exist.
DANGLING = object()


def _tree(root, files):
    """Write ``files`` ({relative path: text, bytes, None for a directory there,
    or ``DANGLING``})."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.mkdir()
        elif content is DANGLING:
            path.symlink_to(root / "nowhere")
        elif isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
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
            CgroupMemory(1024, None, "cgroup_v2"),
        ),
        ({"memory.max": "1024\n"}, CgroupMemory(1024, None, "cgroup_v2")),
        (
            {"memory.max": "1024\n", "memory.oom.group": "max\n"},
            CgroupMemory(1024, None, "cgroup_v2"),
        ),
        ({"memory.max": DANGLING}, CgroupMemory(None, None, "unreadable")),
        (
            {"memory.max": "lots\n", "memory.oom.group": "1\n"},
            CgroupMemory(None, None, "invalid"),
        ),
        (
            {"memory.max": "1024\n", "memory.oom.group": "2\n"},
            CgroupMemory(1024, None, "cgroup_v2"),
        ),
        ({"memory.max": b"\xff\xfe1024\n"}, CgroupMemory(None, None, "invalid")),
        ({"memory.max": ""}, CgroupMemory(None, None, "invalid")),
        ({"memory.max": "1_073_741_824\n"}, CgroupMemory(None, None, "invalid")),
        ({"memory.max": "+1073741824\n"}, CgroupMemory(None, None, "invalid")),
        (
            {"memory.max": "\u0661\u0660\u0662\u0664\n"},
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
        "unreadable-oom-group-keeps-the-limit",
        "absent-oom-group-keeps-the-limit",
        "max-in-oom-group-keeps-the-limit",
        "dangling-symlink-memory-max",
        "invalid-v2",
        "invalid-oom-group-keeps-the-limit",
        "non-utf8",
        "empty",
        "underscore-digits",
        "plus-sign",
        "non-ascii-digits",
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

    # 1,536 + 4,992 + 1,024 + 2,048 + 2 x 618.00004 + 12,288 MiB; a search's
    # limit is 64 MiB, its text (30 MiB) and 4 x its output limit (30 MiB of
    # text, 10 Mi + 1 lines at 10 bytes of prefix each, and 1 MiB).
    assert status.pools_bytes + status.encode_bytes == 24_247_271_504
    # 2 x 5 + 2 x (2 x 30 + 61) + 4 x (2 x 0.76 + 10) + 2 x (2 x 10 + 1)
    # + 2 x (2 x 30 + 131) MiB: 722.10 MiB, the gate's texts 2 x 2 x 400,000
    # bytes each.
    assert status.buffers_bytes == 757_180_436
    assert status.job_caps_bytes == 24_247_271_504 + 757_180_436
    assert (status.serving_processes, status.upload_limit_mb, status.tier1_slots) == (
        1,
        10,
        2,
    )
    assert status.unbounded_terms == ["api", "encode_buffers"]
    assert status.encode_without_host_lock_bytes == 12 * GIB


def test_each_further_upload_mb_adds_784_mib(two_cores):
    at_10 = container_memory_status(_settings(upload_mb=10))
    at_11 = container_memory_status(_settings(upload_mb=11))

    # Caps 2 x 64 + 2 x 240 + 2 x (3 + 4 x (3 + 10)) = 718 MiB; buffers
    # 2 x (6 + 6) + 2 x 2 + 2 x (6 + 3 + 10) = 66 MiB.
    assert at_11.job_caps_bytes - at_10.job_caps_bytes == 784 * MIB


def test_a_second_serving_process_adds_its_pools_buffers_and_the_supervisor(
    two_cores,
):
    one = container_memory_status(_settings(workers=1))
    two = container_memory_status(_settings(workers=2))

    # One process's pools, 9,600 MiB and 2 x 618.00004 MiB, and its buffers;
    # the encode stays once.
    assert (
        two.job_caps_bytes - one.job_caps_bytes
        == 24_247_271_504 - 12_288 * MIB + 757_180_436
    )
    assert two.unbounded_terms == ["api", "supervisor", "encode_buffers"]
    assert two.encode_without_host_lock_bytes == 24 * GIB
    assert container_memory_status(_settings(reload=True)).unbounded_terms == [
        "api",
        "supervisor",
        "encode_buffers",
    ]
