"""The runbook gate's load-shaped limits, derived at a two-core share (#2661 3b2).

Measured at full load on two cores, a check that ran to its 3 s CPU deadline
took up to 13.8 s of wall time, so the wall backstop is the deadline plus 2 s at
the worst share: 20 s there, and the conversion's slot wait 46 s. The limits are
derived when ``runbook_gate`` is imported, from ``index_cores()``; they are read
here in a fresh interpreter whose CPU quota reads two cores, so neither this
host's core count nor the module other tests imported decides them.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_PROBE = """
import json
from faultmaven.infrastructure import model_cache

model_cache._cgroup_cpu_quota = lambda: 2.0
from faultmaven.infrastructure.index_slot import index_cores
from faultmaven.modules.knowledge.domain.services import runbook_gate as g

print(json.dumps({
    "cores": index_cores(),
    "share": g._CPU_SHARE,
    "cpu": g.GATE_WORK_CPU_SECONDS,
    "start": g.GATE_START_SECONDS,
    "wall": g.GATE_WORK_WALL_SECONDS,
    "transfer": g.GATE_TRANSFER_SECONDS,
    "wait": g.GATE_SLOT_WAIT_SECONDS,
    "background": g.GATE_BACKGROUND_SLOT_WAIT_SECONDS,
}))
"""

#: The ingress read timeout a request's worst gate call must fit inside.
_INGRESS_READ_TIMEOUT_SECONDS = 60


@pytest.fixture(scope="module")
def two_cores() -> dict:
    root = Path(faultmaven.__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    limits = json.loads(done.stdout.strip().splitlines()[-1])
    assert limits["cores"] == 2.0
    assert limits["share"] == 0.25
    return limits


def test_the_wall_backstop_is_the_cpu_deadline_plus_two_seconds_at_the_share(
    two_cores,
):
    assert two_cores["wall"] * two_cores["share"] == pytest.approx(two_cores["cpu"] + 2)
    assert two_cores["wall"] == pytest.approx(20.0)


def test_the_conversion_waits_as_long_as_the_longest_call_takes(two_cores):
    lim = two_cores
    assert lim["background"] == pytest.approx(
        lim["start"] + 2 * lim["transfer"] + 2 * lim["wall"]
    )
    assert lim["background"] == pytest.approx(46.0)


def test_a_requests_worst_gate_call_fits_the_ingress_read_timeout(two_cores):
    lim = two_cores
    worst = lim["wait"] + lim["start"] + 2 * lim["transfer"] + 2 * lim["wall"]
    assert worst == pytest.approx(51.0)
    assert worst <= _INGRESS_READ_TIMEOUT_SECONDS
