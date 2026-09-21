"""Drive `check_all_components`' sweep-budget arm at the ordering that ships.

The shipped constants are `_PROBE_TIMEOUT_SECONDS = 3.0` **below**
`_ALL_COMPONENTS_TIMEOUT_SECONDS = 4.0`: every probe carries its own deadline
under the sweep budget, so the sweep budget is a BACKSTOP that no shipped
probe reaches. A test that wants the backstop has exactly one honest way in —
a probe that does not observe cancellation, which is the case
`check_all_components`' own docstring names it a backstop for.

The dishonest way in, which these helpers exist to replace, is to set the
sweep budget *below* the per-probe deadline (0.05 against 3.0). That reaches
the arm, and it reaches it in a configuration the product cannot be in, so the
test says nothing about whether the path it proves is reachable — and a reader
takes it as evidence that it is (#1565, and #1564's own first revision).

Shared by `test_component_health_gauge.py` (which introduced the pattern in
#1564) and `test_component_monitor_probes.py` (converted by #1565), because
two copies of a timing grammar is how the two files drift into proving
different things about the same arm.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

from faultmaven.infrastructure.health import (
    component_monitor as component_monitor_module,
)
from faultmaven.infrastructure.health.component_monitor import HealthStatus

#: What `_perform_health_check` is replaced with — one component name in, one
#: probe result out.
ProbeDouble = Callable[[str], Awaitable[Dict[str, Any]]]


def shipped_ratio(monkeypatch, *, probe: float = 0.15, sweep: float = 0.30) -> None:
    """Scale both deadlines while KEEPING the shipped ordering, probe < sweep.

    Magnitude is scaled so the test is fast; the ordering is the thing under
    test and is preserved. The assertion is the whole point of the helper —
    it is what makes "inverted the constants" a test failure rather than a
    reviewer's job.
    """
    assert probe < sweep, "the shipped ordering is per-probe deadline < sweep budget"
    monkeypatch.setattr(component_monitor_module, "_PROBE_TIMEOUT_SECONDS", probe)
    monkeypatch.setattr(
        component_monitor_module, "_ALL_COMPONENTS_TIMEOUT_SECONDS", sweep
    )


def probe_that_ignores_cancellation(
    monitor,
    monkeypatch,
    name: str,
    *,
    others: Optional[ProbeDouble] = None,
) -> None:
    """`name`'s probe swallows its first cancellation; the rest answer normally.

    The per-probe deadline fires first and is ignored, so the task is still
    pending when the sweep budget expires — the one route to the sweep-budget
    arm at the shipped ratio.

    `others` replaces what every OTHER component's probe does; the default is
    a healthy result. A caller passes it to drive a second write-back arm in
    the same sweep (`test_a_probe_sweep_cannot_disarm_the_gate` raises from
    it, so both the abandoned and the errored write-back run before the
    derived set is read).
    """

    async def _probe(component_name: str) -> Dict[str, Any]:
        if component_name == name:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)  # the sweep's cancel ends this one
        if others is not None:
            return await others(component_name)
        return {"status": HealthStatus.HEALTHY, "metadata": {}}

    monkeypatch.setattr(monitor, "_perform_health_check", _probe)
