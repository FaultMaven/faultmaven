"""``ListEvidenceTool``: a failed case read names its class, not its text (#2332)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.list_evidence_tool import ListEvidenceTool
from tests.model_content import disabled_redaction
from tests.unit.modules.agent.tools.leaky_failure import (
    assert_reported_by_class,
    leaky_error,
)


@pytest.mark.asyncio
async def test_a_failed_case_read_names_its_class_not_its_text(caplog):
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=leaky_error())
    context = ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id="user_1",
        case_repository=repo,
        redaction_ctx=disabled_redaction(),
    )

    result = await ListEvidenceTool().execute_with_context({}, context)

    assert result.success is False
    assert_reported_by_class(result.error, caplog.text)
