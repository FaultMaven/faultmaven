"""Domain ↔ row mappers for agent_executions and agent_tool_calls.

Pure functions only — no DB access, no SQLAlchemy session. Both
SQLiteCaseRepository and PostgreSQLHybridCaseRepository import these to
avoid duplicating the JSON / enum / datetime translation logic.

Schema reference: docs/architecture/data-and-storage/schemas/case-schema.md §4.11
ORM models: faultmaven/infrastructure/persistence/models.py (AgentExecutionModel,
AgentToolCallModel).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)


def _ensure_utc(value: Any) -> datetime | None:
    """Normalize a datetime-like value to a tz-aware UTC datetime, or None.

    SQLite returns naive strings or datetimes; PostgreSQL returns tz-aware
    datetimes. Treat naive datetimes as UTC (matches the timestamptz column
    semantics on the write path).
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def _parse_json_dict(value: Any) -> dict | None:
    """Parse a JSON column that may arrive as str, dict, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ---------------------------------------------------------------------------
# AgentExecution
# ---------------------------------------------------------------------------


def execution_insert_params(
    execution: AgentExecution,
    organization_id: str,
) -> dict:
    """Build :param dict for the agent_executions INSERT statement.

    `organization_id` is taken as an explicit argument because the dataclass
    field is Optional — the repository resolves it (from the dataclass or
    the parent case row) before calling.
    """
    return {
        "execution_id": execution.execution_id,
        "case_id": execution.case_id,
        "organization_id": organization_id,
        "agent_type": execution.agent_type.value,
        "agent_model": execution.agent_model,
        "status": execution.status.value,
        "started_at": execution.started_at,
        "completed_at": execution.completed_at,
        "execution_duration_ms": execution.execution_duration_ms,
        "prompt": execution.prompt,
        "response": execution.response,
        "error_message": execution.error_message,
        "token_usage": (
            json.dumps(execution.token_usage) if execution.token_usage else None
        ),
        "metadata": json.dumps(execution.metadata) if execution.metadata else None,
        "session_id": (
            execution.metadata.get("session_id")
            if isinstance(execution.metadata, dict)
            else None
        ),
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
    }


def execution_update_params(execution: AgentExecution) -> dict:
    """Build :param dict for the agent_executions UPDATE statement.

    Excludes immutable fields (execution_id, case_id, organization_id,
    created_at) — those are used in the WHERE clause or never change.
    """
    return {
        "execution_id": execution.execution_id,
        "agent_type": execution.agent_type.value,
        "agent_model": execution.agent_model,
        "status": execution.status.value,
        "started_at": execution.started_at,
        "completed_at": execution.completed_at,
        "execution_duration_ms": execution.execution_duration_ms,
        "prompt": execution.prompt,
        "response": execution.response,
        "error_message": execution.error_message,
        "token_usage": (
            json.dumps(execution.token_usage) if execution.token_usage else None
        ),
        "metadata": json.dumps(execution.metadata) if execution.metadata else None,
        "updated_at": execution.updated_at,
    }


def row_to_execution(
    row: Any,
    tool_call_rows: Iterable[Any] = (),
) -> AgentExecution:
    """Convert an agent_executions row + its tool_call rows into a domain object.

    Expects row to expose attributes (or indexable access) for the columns:
    execution_id, case_id, organization_id, agent_type, agent_model, status,
    started_at, completed_at, execution_duration_ms, prompt, response,
    error_message, token_usage, metadata, created_at, updated_at.

    `metadata` column is stored under SQLAlchemy attribute `execution_metadata`
    (Python attribute, see models.py:1278). Callers pass the row from a SELECT
    that aliases the column to `metadata` to keep this mapper simple.
    """
    token_usage = _parse_json_dict(_get(row, "token_usage"))
    metadata = _parse_json_dict(_get(row, "metadata"))

    tool_calls = [row_to_tool_call(tc) for tc in tool_call_rows]
    tool_calls.sort(key=lambda tc: tc.created_at or datetime.min.replace(tzinfo=UTC))

    try:
        agent_type = AgentType(_get(row, "agent_type"))
    except ValueError:
        agent_type = AgentType.CUSTOM

    try:
        status = ExecutionStatus(_get(row, "status"))
    except ValueError:
        status = ExecutionStatus.FAILED

    return AgentExecution(
        execution_id=str(_get(row, "execution_id")),
        case_id=str(_get(row, "case_id")),
        organization_id=_optional_str(_get(row, "organization_id")),
        agent_type=agent_type,
        agent_model=str(_get(row, "agent_model")),
        status=status,
        started_at=_ensure_utc(_get(row, "started_at")),
        completed_at=_ensure_utc(_get(row, "completed_at")),
        execution_duration_ms=_get(row, "execution_duration_ms"),
        prompt=_get(row, "prompt"),
        response=_get(row, "response"),
        error_message=_get(row, "error_message"),
        token_usage=token_usage,
        tool_calls=tool_calls,
        metadata=metadata,
        created_at=_ensure_utc(_get(row, "created_at")) or datetime.now(UTC),
        updated_at=_ensure_utc(_get(row, "updated_at")) or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# AgentToolCall
# ---------------------------------------------------------------------------


def tool_call_insert_params(
    tool_call: AgentToolCall,
    organization_id: str,
) -> dict:
    """Build :param dict for the agent_tool_calls INSERT statement."""
    return {
        "tool_call_id": tool_call.tool_call_id,
        "execution_id": tool_call.execution_id,
        "organization_id": organization_id,
        "tool_name": tool_call.tool_name,
        "tool_input": (
            json.dumps(tool_call.tool_input)
            if tool_call.tool_input is not None
            else None
        ),
        "tool_output": (
            json.dumps(tool_call.tool_output)
            if tool_call.tool_output is not None
            else None
        ),
        "status": tool_call.status,
        "error_message": tool_call.error_message,
        "started_at": tool_call.started_at,
        "completed_at": tool_call.completed_at,
        "duration_ms": tool_call.duration_ms,
        "created_at": tool_call.created_at,
        "updated_at": tool_call.updated_at,
    }


def tool_call_update_params(tool_call: AgentToolCall) -> dict:
    """Build :param dict for the agent_tool_calls UPDATE statement."""
    return {
        "tool_call_id": tool_call.tool_call_id,
        "tool_name": tool_call.tool_name,
        "tool_input": (
            json.dumps(tool_call.tool_input)
            if tool_call.tool_input is not None
            else None
        ),
        "tool_output": (
            json.dumps(tool_call.tool_output)
            if tool_call.tool_output is not None
            else None
        ),
        "status": tool_call.status,
        "error_message": tool_call.error_message,
        "started_at": tool_call.started_at,
        "completed_at": tool_call.completed_at,
        "duration_ms": tool_call.duration_ms,
        "updated_at": tool_call.updated_at,
    }


def row_to_tool_call(row: Any) -> AgentToolCall:
    """Convert an agent_tool_calls row into a domain object."""
    return AgentToolCall(
        tool_call_id=str(_get(row, "tool_call_id")),
        execution_id=str(_get(row, "execution_id")),
        organization_id=_optional_str(_get(row, "organization_id")),
        tool_name=str(_get(row, "tool_name")),
        tool_input=_parse_json_dict(_get(row, "tool_input")),
        tool_output=_parse_json_dict(_get(row, "tool_output")),
        status=str(_get(row, "status")),
        error_message=_get(row, "error_message"),
        started_at=_ensure_utc(_get(row, "started_at")),
        completed_at=_ensure_utc(_get(row, "completed_at")),
        duration_ms=_get(row, "duration_ms"),
        created_at=_ensure_utc(_get(row, "created_at")) or datetime.now(UTC),
        updated_at=_ensure_utc(_get(row, "updated_at")) or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get(row: Any, name: str) -> Any:
    """Read a column off a SQLAlchemy Row that may support attribute or
    mapping-style access. Falls back to None on missing columns so callers
    that SELECT a subset still work."""
    if hasattr(row, name):
        return getattr(row, name)
    try:
        return row[name]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
