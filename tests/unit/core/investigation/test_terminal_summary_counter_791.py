"""#791: every automatic terminal-summary attempt increments one outcome series.

Drives ``TerminalTurnHandler.auto_generate_report`` with a stub report service
(no LLM call). The summary is rendered (``render_reports``) and its row carried
in the turn's plan (#1882); the counter counts the render ATTEMPT. The counter is a no-op shim unless ``ENABLE_METRICS`` is set, so
the labelled child is asserted through a patched counter object.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import terminal_turns
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.modules.case.contracts import CaseState

pytestmark = pytest.mark.unit


def _case(state, *, substance=True):
    return SimpleNamespace(
        case_id="case_x",
        state=state,
        evidence=[object()] if substance else [],
        hypotheses=[],
        progress=None,
    )


def _handler(report_service):
    return TerminalTurnHandler(
        deps=SimpleNamespace(report_service=report_service),
        generator=None,
        runbooks=None,
    )


def _service(content=None, *, reports=True, raises=False):
    svc = AsyncMock()
    if raises:
        svc.render_reports.side_effect = RuntimeError("render failed")
    else:
        svc.render_reports.return_value = (
            [SimpleNamespace(content=content, report_type="x")] if reports else []
        )
    return svc


@pytest.mark.unit
class TestTerminalSummaryCounter:
    async def _run(self, case, service):
        with patch.object(terminal_turns, "terminal_summary_total") as counter:
            result = await _handler(service).auto_generate_report(
                case, plan=TurnCommitPlan()
            )
        return result, counter

    async def test_resolved_generated(self):
        (payload, failed), counter = await self._run(
            _case(CaseState.RESOLVED), _service("# Summary")
        )
        assert (payload, failed) == ("# Summary", False)
        counter.labels.assert_called_once_with(
            summary_type="resolution_summary", outcome="generated"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_closed_generated(self):
        _, counter = await self._run(_case(CaseState.CLOSED), _service("body"))
        counter.labels.assert_called_once_with(
            summary_type="closure_summary", outcome="generated"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    @pytest.mark.parametrize(
        "service",
        [_service(""), _service(None), _service(reports=False)],
        ids=["empty-content", "none-content", "no-reports"],
    )
    async def test_empty(self, service):
        (payload, failed), counter = await self._run(_case(CaseState.RESOLVED), service)
        assert (payload, failed) == (None, False)
        counter.labels.assert_called_once_with(
            summary_type="resolution_summary", outcome="empty"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_failed(self):
        (_, failed), counter = await self._run(
            _case(CaseState.CLOSED), _service(raises=True)
        )
        assert failed is True
        counter.labels.assert_called_once_with(
            summary_type="closure_summary", outcome="failed"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_skipped_by_substance_gate(self):
        service = _service("never used")
        (_, failed), counter = await self._run(
            _case(CaseState.CLOSED, substance=False), service
        )
        assert failed is False
        service.render_reports.assert_not_awaited()
        counter.labels.assert_called_once_with(
            summary_type="closure_summary", outcome="skipped"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_skipped_counted_without_a_report_service(self):
        # The substance gate runs before the report-service check, so a skip
        # is counted whether or not a report service is configured.
        (_, failed), counter = await self._run(
            _case(CaseState.CLOSED, substance=False), None
        )
        assert failed is False
        counter.labels.assert_called_once_with(
            summary_type="closure_summary", outcome="skipped"
        )
        counter.labels.return_value.inc.assert_called_once_with()

    async def test_no_report_service_counts_nothing(self):
        result, counter = await self._run(_case(CaseState.RESOLVED), None)
        assert result == (None, False)
        counter.labels.assert_not_called()

    async def test_unexpected_state_counts_nothing(self):
        result, counter = await self._run(_case(CaseState.INVESTIGATING), _service("x"))
        assert result == (None, False)
        counter.labels.assert_not_called()


_CHILD = r"""
import json
from prometheus_client import REGISTRY

import faultmaven.core.investigation.lifecycle_metrics as lm

series = {}
for family in REGISTRY.collect():
    if family.name != "faultmaven_terminal_summary":
        continue
    for s in family.samples:
        if s.name.endswith("_total"):
            series[s.labels["summary_type"] + "|" + s.labels["outcome"]] = s.value
print("@@RESULT@@" + json.dumps({"file": lm.__file__, "series": series}))
"""


def test_every_series_exists_at_zero_from_import():
    """A series born by its first increment is born at 1 and ``increase()``
    cannot see it. Subprocess: the shim picks real-or-NoOp at import."""
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
        f"{t}|{o}": 0.0
        for t in ("resolution_summary", "closure_summary")
        for o in ("generated", "empty", "failed", "skipped")
    }
