"""The engine's and evidence's log lines name an exception by its class and
code, never its text, and neither reader of a worker's ``failure`` frame logs
it (#2889 S3b).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message or the
worker's ``failure`` frame, captures every record at DEBUG, and renders each
through the real chain's formatter (``build_log_formatter``). The marker is on
no rendered line, and the class name (with its code, where first-party) is on
the site's line.
"""

import asyncio
import io
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import case_telemetry
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.processing.log_analyzer import EnhancedLogProcessor
from faultmaven.exceptions import LLMException
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.logging.config import build_log_formatter
from faultmaven.modules.agent.domain.services.investigation_service import (
    turn_settlement,
)
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.knowledge.domain.services import runbook_gate
from faultmaven.modules.knowledge.exceptions import GateFailed
from faultmaven.modules.preprocessing import preprocessing_service as ps
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.test_refusal_reduced_retry_1770 import (
    REFUSED,
    _engine,
    _raising_queue,
    _turn,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
)
from tests.unit.modules.agent.tools.test_search_file_tool import (
    _make_evidence,
    _wire_case_lookup,
)
from tests.unit.modules.preprocessing import extraction_stubs as stubs

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture
def lines(caplog):
    """Every record the test emits, at DEBUG, as the chain's JSON lines."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="faultmaven")
    formatter = build_log_formatter("json", False, io.StringIO())

    def render() -> list[str]:
        return [formatter.format(record) for record in caplog.records]

    return render


# ── core/investigation/llm_error_handler.py ───────────────────────────────


async def test_the_handlers_auth_line_names_class_and_code(lines):
    error = LLMException(f"401 invalid key {MARKER}", status_code=401)
    result = await LLMErrorHandler().handle_error(error)
    assert result.error_code == "AUTH_FAILED"
    _named(lines(), "Authentication error", "LLMException[PROVIDER_AUTH_FAILED]")


# ── core/investigation/milestone_engine/generation.py ─────────────────────


async def test_a_failed_tool_loop_reissue_is_named_by_class(lines):
    engine = _engine()
    engine.deps.da_provider._generate, _ = _raising_queue(
        [REFUSED, LLMException(f"upstream {MARKER} rejected the call")]
    )
    with pytest.raises(MilestoneEngineError):
        await _turn(engine, tools=True)
    _named(lines(), "Tool loop: the re-issue failed", "LLMException")


# ── core/investigation/case_telemetry.py ──────────────────────────────────


def test_a_dropped_telemetry_field_is_named_by_class(lines):
    with patch.object(
        case_telemetry, "_scalar", side_effect=ValueError(f"bad {MARKER}")
    ):
        assert case_telemetry._sanitize({"case_id": 1}) == {}
    _named(lines(), "case telemetry dropped field", "ValueError")


# ── modules/agent/tools/search_file_tool.py ───────────────────────────────


async def test_a_search_failure_is_named_by_class_with_its_frames(lines):
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(return_value=b"line 1 ERROR timeout\n")
    tool = SearchFileTool(storage_service=storage)
    tool._keyword_search = MagicMock(side_effect=RuntimeError(f"scan {MARKER}"))
    case = MagicMock()
    case.case_id = "case_123"
    case.evidence = [_make_evidence()]
    _wire_case_lookup(case)
    context = ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id="user_1",
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )
    result = await tool.execute_with_context(
        params={"evidence_id": "ev_abc", "query": "ERROR"}, context=context
    )
    assert result.success is False
    failed = _named(lines(), "search_file failed", "RuntimeError")
    assert failed["frames"].startswith("RuntimeError:")


# ── modules/agent/domain/services/investigation_service/turn_settlement.py ─


async def test_a_failed_settlement_is_named_by_class(lines):
    settlement = asyncio.get_running_loop().create_future()
    settlement.set_exception(RuntimeError(f"commit {MARKER}"))
    turn_settlement._settlement_finished(settlement)
    _named(lines(), "Turn settlement ended in error", "RuntimeError")


# ── core/processing/log_analyzer.py ───────────────────────────────────────


async def test_a_failed_log_processing_is_named_by_class(lines):
    processor = EnhancedLogProcessor()
    processor._parse_logs_with_context = MagicMock(
        side_effect=RuntimeError(f"parse {MARKER}")
    )
    await processor._process_with_memory_context("x", {}, time.time())
    _named(lines(), "Enhanced log processing failed", "RuntimeError")


# ── the worker's failure frame: its two readers ───────────────────────────


def _failed_worker(monkeypatch, failure: str) -> None:
    end = worker_process.WorkerEnd(phase="work", failure=failure)
    monkeypatch.setattr(worker_process, "run", AsyncMock(return_value=end))


async def test_a_failed_runbook_check_logs_no_failure_text(lines, monkeypatch):
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    _failed_worker(monkeypatch, f"ValueError: {MARKER}")
    with pytest.raises(GateFailed):
        await runbook_gate.avalidate_and_score("body", principal="user_1")
    _named(lines(), "runbook checks", "failed")


async def test_a_failed_tier1_extraction_logs_no_failure_text(lines, monkeypatch):
    """The frame's text stays Tier 1's ``metadata.error``, a stored value, not
    a log line."""
    monkeypatch.setattr(ps, "_tier1_slots_state", None)
    _failed_worker(monkeypatch, f"bad extract {MARKER}")
    service = ps.PreprocessingService(
        classifier=MagicMock(), logs_extractor=stubs.CrimeScene()
    )
    result = await service._extract_with_timeout(
        stubs.CrimeScene(), "content", "x.log", principal="u1"
    )
    assert result.metadata == {
        "error_fallback": True,
        "error": f"bad extract {MARKER}",
    }
    _named(lines(), "Tier 1 extraction failed", "RuntimeError")
