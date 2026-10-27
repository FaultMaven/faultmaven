"""A timed-out ``deep_analysis`` or ``search_file`` still starts reactive vectorization.

The trigger read ``"timed out"`` in the tool's error text, which no tool result
carries since #2332. It reads ``ToolResult.timed_out`` now, which
``tool_failure`` decides by class along the failure's cause chain. Driven
through the real tools over the real ``FileStorageService``, whose backend
raises: the service wraps a backend timeout in ``ServiceError``, so the class
is found on the chain or nowhere.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from tests.model_content import disabled_redaction

#: The S3 backend's read timeout, as botocore names it (not installed here).
_BotocoreReadTimeout = type(
    "ReadTimeoutError", (Exception,), {"__module__": "botocore.exceptions"}
)


def _context() -> ToolContext:
    ev = MagicMock(evidence_id="ev_1", source_file_id="file_1", category=None)
    ev.vectorized = False
    case = MagicMock(evidence=[ev], uploaded_files=[])
    case.find_uploaded_file.return_value = MagicMock(
        storage_ref="org_1/case_1/app.log", filename="app.log", display_name="app.log"
    )
    return ToolContext(
        session_id="sess_1",
        case_id="case_1",
        enterprise_id="org_1",
        user_id="user_1",
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )


def _tool(name: str, error: BaseException):
    backend = MagicMock()
    backend.get_storage_type.return_value.value = "local"
    backend.retrieve_file = AsyncMock(side_effect=error)
    storage = FileStorageService(backend=backend)
    if name == "deep_analysis":
        # The model is never reached: the storage read fails first.
        tier2 = LocalTier2Service(llm_client=AsyncMock(), storage_service=storage)
        return DeepAnalysisTool(tier2_service=tier2)
    return SearchFileTool(storage_service=storage)


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", ["deep_analysis", "search_file"])
@pytest.mark.parametrize(
    "error, timed_out",
    [
        (TimeoutError("timed out"), True),
        (asyncio.TimeoutError(), True),
        (_BotocoreReadTimeout("Read timeout on endpoint URL"), True),
        (OSError("timed out reading the disk"), False),
    ],
    ids=["TimeoutError", "asyncio.TimeoutError", "botocore", "OSError"],
)
async def test_a_tool_timeout_starts_reactive_vectorization(
    tool_name, error, timed_out
):
    context = _context()
    result = await _tool(tool_name, error).execute_with_context(
        {"evidence_id": "ev_1", "query": "ERROR"}, context
    )
    assert result.success is False
    assert "ServiceError" in result.error and "timed out" not in result.error

    vectorizer = EvidenceVectorizer(deps=MagicMock())
    vectorizer._reactive_vectorize = AsyncMock(return_value="vectorized")
    await vectorizer.track_da_result(
        func_name=tool_name,
        evidence_id="ev_1",
        tool_result=result,
        result_text="before",
        case=context.in_memory_case,
        tool_context=context,
        da_empty_search_counts={},
        proactive_tasks={},
        given_tools=frozenset(),
    )

    triggers = [call.args[3] for call in vectorizer._reactive_vectorize.await_args_list]
    assert triggers == (["tool_timeout"] if timed_out else [])
