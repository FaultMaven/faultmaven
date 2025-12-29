"""Agent Execution Repository - Database and In-Memory Implementations.

This module provides agent execution repository implementations for tracking
AI agent executions and their tool calls.

Features:
- Full CRUD operations for agent executions
- Nested tool call management within executions
- Query by case with optional status/type filtering
- Pagination support
- Support for both in-memory and database backends

Usage:
    from faultmaven.infrastructure.persistence.agent_execution_repository import (
        DatabaseAgentExecutionRepository,
        InMemoryAgentExecutionRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseAgentExecutionRepository(session)
        execution = await repo.create_execution(execution)

    # In-memory usage (for testing)
    repo = InMemoryAgentExecutionRepository()
    execution = await repo.create_execution(execution)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from faultmaven.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.infrastructure.persistence.models import (
    AgentExecutionModel,
    AgentToolCallV2Model,
)

logger = logging.getLogger(__name__)


class AgentExecutionRepositoryException(Exception):
    """Exception raised when agent execution repository operations fail."""

    pass


class AgentExecutionRepository(ABC):
    """Abstract base class for agent execution repositories."""

    # =========================================================================
    # Execution Operations
    # =========================================================================

    @abstractmethod
    async def create_execution(self, execution: AgentExecution) -> AgentExecution:
        """Create new agent execution record.

        Args:
            execution: Agent execution to create

        Returns:
            Created agent execution with timestamps

        Raises:
            ValueError: If execution_id already exists or case_id not found
            AgentExecutionRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get execution by ID with tool calls loaded.

        Args:
            execution_id: Execution identifier

        Returns:
            Agent execution with tool calls if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_executions_by_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        agent_type: Optional[AgentType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        """List executions for a case with optional filters.

        Args:
            case_id: Case identifier
            status: Optional filter by execution status
            agent_type: Optional filter by agent type
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            Tuple of (execution list, total count)
        """
        pass

    @abstractmethod
    async def update_execution(self, execution: AgentExecution) -> AgentExecution:
        """Update execution status and results.

        Args:
            execution: Execution with updated fields

        Returns:
            Updated execution

        Raises:
            ValueError: If execution not found
            AgentExecutionRepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def delete_execution(self, execution_id: str) -> bool:
        """Delete execution by ID.

        This will also delete all associated tool calls due to CASCADE.

        Args:
            execution_id: Execution identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    # =========================================================================
    # Tool Call Operations
    # =========================================================================

    @abstractmethod
    async def create_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Create new tool call record.

        Args:
            tool_call: Tool call to create

        Returns:
            Created tool call with timestamps

        Raises:
            ValueError: If tool_call_id already exists or execution_id not found
            AgentExecutionRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def update_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Update tool call status and results.

        Args:
            tool_call: Tool call with updated fields

        Returns:
            Updated tool call

        Raises:
            ValueError: If tool call not found
            AgentExecutionRepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def get_tool_calls_for_execution(
        self,
        execution_id: str,
    ) -> List[AgentToolCall]:
        """Get all tool calls for an execution.

        Args:
            execution_id: Execution identifier

        Returns:
            List of tool calls ordered by created_at
        """
        pass

    # =========================================================================
    # Utility Operations
    # =========================================================================

    @abstractmethod
    async def count_executions_by_case(self, case_id: str) -> int:
        """Count executions for a case.

        Args:
            case_id: Case identifier

        Returns:
            Number of executions for the case
        """
        pass

    @abstractmethod
    async def get_latest_execution(
        self,
        case_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> Optional[AgentExecution]:
        """Get the most recent execution for a case.

        Args:
            case_id: Case identifier
            agent_type: Optional filter by agent type

        Returns:
            Most recent execution if any, None otherwise
        """
        pass


class DatabaseAgentExecutionRepository(AgentExecutionRepository):
    """Database-backed agent execution repository using SQLAlchemy ORM."""

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    # =========================================================================
    # Execution Operations
    # =========================================================================

    async def create_execution(self, execution: AgentExecution) -> AgentExecution:
        """Create new agent execution record in the database."""
        try:
            # Convert domain model to ORM model
            execution_model = AgentExecutionModel(
                execution_id=execution.execution_id,
                case_id=execution.case_id,
                agent_type=execution.agent_type.value,
                agent_model=execution.agent_model,
                status=execution.status.value,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                execution_duration_ms=execution.execution_duration_ms,
                prompt=execution.prompt,
                response=execution.response,
                error_message=execution.error_message,
                token_usage=json.dumps(execution.token_usage) if execution.token_usage else None,
                execution_metadata=json.dumps(execution.metadata) if execution.metadata else "{}",
                created_at=execution.created_at,
                updated_at=execution.updated_at,
            )

            self.db.add(execution_model)
            await self.db.commit()
            await self.db.refresh(execution_model)

            logger.debug(f"Created agent execution {execution.execution_id}")
            return self._execution_to_domain(execution_model)

        except IntegrityError as e:
            await self.db.rollback()
            error_str = str(e).lower()
            if "foreign key" in error_str or "fk_" in error_str:
                logger.error(f"Case {execution.case_id} not found")
                raise ValueError(f"Case {execution.case_id} not found") from e
            logger.error(f"Agent execution {execution.execution_id} already exists")
            raise ValueError(f"Agent execution {execution.execution_id} already exists") from e

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create agent execution: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to create agent execution: {e}"
            ) from e

    async def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get execution by ID with tool calls loaded."""
        try:
            stmt = (
                select(AgentExecutionModel)
                .options(selectinload(AgentExecutionModel.tool_calls_v2))
                .where(AgentExecutionModel.execution_id == execution_id)
            )
            result = await self.db.execute(stmt)
            execution_model = result.scalar_one_or_none()

            if execution_model is None:
                return None

            return self._execution_to_domain(execution_model)

        except Exception as e:
            logger.error(f"Failed to get agent execution {execution_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to get agent execution {execution_id}: {e}"
            ) from e

    async def list_executions_by_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        agent_type: Optional[AgentType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        """List executions for a case with optional filters."""
        try:
            # Build query conditions
            conditions = [AgentExecutionModel.case_id == case_id]
            if status:
                conditions.append(AgentExecutionModel.status == status.value)
            if agent_type:
                conditions.append(AgentExecutionModel.agent_type == agent_type.value)

            where_clause = and_(*conditions)

            # Get total count
            count_stmt = (
                select(func.count())
                .select_from(AgentExecutionModel)
                .where(where_clause)
            )
            count_result = await self.db.execute(count_stmt)
            total = count_result.scalar() or 0

            # Get paginated results with tool calls loaded
            stmt = (
                select(AgentExecutionModel)
                .options(selectinload(AgentExecutionModel.tool_calls_v2))
                .where(where_clause)
                .order_by(AgentExecutionModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            execution_models = result.scalars().all()

            executions = [self._execution_to_domain(model) for model in execution_models]

            return executions, total

        except Exception as e:
            logger.error(f"Failed to list executions for case {case_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to list executions for case {case_id}: {e}"
            ) from e

    async def update_execution(self, execution: AgentExecution) -> AgentExecution:
        """Update execution status and results."""
        try:
            execution.updated_at = datetime.now(timezone.utc)

            stmt = (
                update(AgentExecutionModel)
                .where(AgentExecutionModel.execution_id == execution.execution_id)
                .values(
                    status=execution.status.value,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                    execution_duration_ms=execution.execution_duration_ms,
                    prompt=execution.prompt,
                    response=execution.response,
                    error_message=execution.error_message,
                    token_usage=json.dumps(execution.token_usage) if execution.token_usage else None,
                    execution_metadata=json.dumps(execution.metadata) if execution.metadata else "{}",
                    updated_at=execution.updated_at,
                )
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(stmt)

            if result.rowcount == 0:
                raise ValueError(f"Agent execution {execution.execution_id} not found")

            await self.db.commit()
            logger.debug(f"Updated agent execution {execution.execution_id}")
            return execution

        except ValueError:
            raise

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update agent execution: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to update agent execution: {e}"
            ) from e

    async def delete_execution(self, execution_id: str) -> bool:
        """Delete execution by ID."""
        try:
            stmt = delete(AgentExecutionModel).where(
                AgentExecutionModel.execution_id == execution_id
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted agent execution {execution_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete agent execution {execution_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to delete agent execution {execution_id}: {e}"
            ) from e

    # =========================================================================
    # Tool Call Operations
    # =========================================================================

    async def create_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Create new tool call record in the database."""
        try:
            # Convert domain model to ORM model
            tool_call_model = AgentToolCallV2Model(
                tool_call_id=tool_call.tool_call_id,
                execution_id=tool_call.execution_id,
                tool_name=tool_call.tool_name,
                tool_input=json.dumps(tool_call.tool_input) if tool_call.tool_input else None,
                tool_output=json.dumps(tool_call.tool_output) if tool_call.tool_output else None,
                status=tool_call.status,
                error_message=tool_call.error_message,
                started_at=tool_call.started_at,
                completed_at=tool_call.completed_at,
                duration_ms=tool_call.duration_ms,
                created_at=tool_call.created_at,
                updated_at=tool_call.updated_at,
            )

            self.db.add(tool_call_model)
            await self.db.commit()
            await self.db.refresh(tool_call_model)

            logger.debug(f"Created tool call {tool_call.tool_call_id}")
            return self._tool_call_to_domain(tool_call_model)

        except IntegrityError as e:
            await self.db.rollback()
            error_str = str(e).lower()
            if "foreign key" in error_str or "fk_" in error_str:
                logger.error(f"Execution {tool_call.execution_id} not found")
                raise ValueError(f"Execution {tool_call.execution_id} not found") from e
            logger.error(f"Tool call {tool_call.tool_call_id} already exists")
            raise ValueError(f"Tool call {tool_call.tool_call_id} already exists") from e

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create tool call: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to create tool call: {e}"
            ) from e

    async def update_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Update tool call status and results."""
        try:
            tool_call.updated_at = datetime.now(timezone.utc)

            stmt = (
                update(AgentToolCallV2Model)
                .where(AgentToolCallV2Model.tool_call_id == tool_call.tool_call_id)
                .values(
                    tool_output=json.dumps(tool_call.tool_output) if tool_call.tool_output else None,
                    status=tool_call.status,
                    error_message=tool_call.error_message,
                    started_at=tool_call.started_at,
                    completed_at=tool_call.completed_at,
                    duration_ms=tool_call.duration_ms,
                    updated_at=tool_call.updated_at,
                )
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(stmt)

            if result.rowcount == 0:
                raise ValueError(f"Tool call {tool_call.tool_call_id} not found")

            await self.db.commit()
            logger.debug(f"Updated tool call {tool_call.tool_call_id}")
            return tool_call

        except ValueError:
            raise

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update tool call: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to update tool call: {e}"
            ) from e

    async def get_tool_calls_for_execution(
        self,
        execution_id: str,
    ) -> List[AgentToolCall]:
        """Get all tool calls for an execution."""
        try:
            stmt = (
                select(AgentToolCallV2Model)
                .where(AgentToolCallV2Model.execution_id == execution_id)
                .order_by(AgentToolCallV2Model.created_at.asc())
            )

            result = await self.db.execute(stmt)
            tool_call_models = result.scalars().all()

            return [self._tool_call_to_domain(model) for model in tool_call_models]

        except Exception as e:
            logger.error(f"Failed to get tool calls for execution {execution_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to get tool calls for execution {execution_id}: {e}"
            ) from e

    # =========================================================================
    # Utility Operations
    # =========================================================================

    async def count_executions_by_case(self, case_id: str) -> int:
        """Count executions for a case."""
        try:
            stmt = (
                select(func.count())
                .select_from(AgentExecutionModel)
                .where(AgentExecutionModel.case_id == case_id)
            )
            result = await self.db.execute(stmt)
            return result.scalar() or 0

        except Exception as e:
            logger.error(f"Failed to count executions for case {case_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to count executions for case {case_id}: {e}"
            ) from e

    async def get_latest_execution(
        self,
        case_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> Optional[AgentExecution]:
        """Get the most recent execution for a case."""
        try:
            conditions = [AgentExecutionModel.case_id == case_id]
            if agent_type:
                conditions.append(AgentExecutionModel.agent_type == agent_type.value)

            stmt = (
                select(AgentExecutionModel)
                .options(selectinload(AgentExecutionModel.tool_calls_v2))
                .where(and_(*conditions))
                .order_by(AgentExecutionModel.created_at.desc())
                .limit(1)
            )

            result = await self.db.execute(stmt)
            execution_model = result.scalar_one_or_none()

            if execution_model is None:
                return None

            return self._execution_to_domain(execution_model)

        except Exception as e:
            logger.error(f"Failed to get latest execution for case {case_id}: {e}")
            raise AgentExecutionRepositoryException(
                f"Failed to get latest execution for case {case_id}: {e}"
            ) from e

    # =========================================================================
    # Model Conversion Helpers
    # =========================================================================

    def _execution_to_domain(self, model: AgentExecutionModel) -> AgentExecution:
        """Convert SQLAlchemy model to domain model."""
        token_usage = None
        if model.token_usage:
            try:
                token_usage = json.loads(model.token_usage)
            except (json.JSONDecodeError, TypeError):
                token_usage = None

        metadata = None
        if model.execution_metadata:
            try:
                metadata = json.loads(model.execution_metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        # Convert tool calls
        tool_calls = []
        if hasattr(model, 'tool_calls_v2') and model.tool_calls_v2:
            tool_calls = [
                self._tool_call_to_domain(tc)
                for tc in sorted(model.tool_calls_v2, key=lambda x: x.created_at)
            ]

        return AgentExecution(
            execution_id=model.execution_id,
            case_id=model.case_id,
            agent_type=AgentType(model.agent_type),
            agent_model=model.agent_model,
            status=ExecutionStatus(model.status),
            started_at=self._ensure_tz_aware(model.started_at),
            completed_at=self._ensure_tz_aware(model.completed_at),
            execution_duration_ms=model.execution_duration_ms,
            prompt=model.prompt,
            response=model.response,
            error_message=model.error_message,
            token_usage=token_usage,
            tool_calls=tool_calls,
            metadata=metadata,
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
        )

    def _tool_call_to_domain(self, model: AgentToolCallV2Model) -> AgentToolCall:
        """Convert SQLAlchemy model to domain model."""
        tool_input = None
        if model.tool_input:
            try:
                tool_input = json.loads(model.tool_input)
            except (json.JSONDecodeError, TypeError):
                tool_input = None

        tool_output = None
        if model.tool_output:
            try:
                tool_output = json.loads(model.tool_output)
            except (json.JSONDecodeError, TypeError):
                tool_output = None

        return AgentToolCall(
            tool_call_id=model.tool_call_id,
            execution_id=model.execution_id,
            tool_name=model.tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            status=model.status,
            error_message=model.error_message,
            started_at=self._ensure_tz_aware(model.started_at),
            completed_at=self._ensure_tz_aware(model.completed_at),
            duration_ms=model.duration_ms,
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
        )

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class InMemoryAgentExecutionRepository(AgentExecutionRepository):
    """In-memory agent execution repository for testing."""

    def __init__(self):
        """Initialize empty storage."""
        self._executions: Dict[str, AgentExecution] = {}
        self._tool_calls: Dict[str, AgentToolCall] = {}

    # =========================================================================
    # Execution Operations
    # =========================================================================

    async def create_execution(self, execution: AgentExecution) -> AgentExecution:
        """Create new agent execution record in memory."""
        if execution.execution_id in self._executions:
            raise ValueError(f"Agent execution {execution.execution_id} already exists")

        # Store a deep copy to prevent mutation
        stored = deepcopy(execution)
        stored.tool_calls = []  # Tool calls stored separately
        self._executions[execution.execution_id] = stored

        return deepcopy(stored)

    async def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get execution by ID with tool calls loaded."""
        execution = self._executions.get(execution_id)
        if execution is None:
            return None

        # Deep copy and attach tool calls
        result = deepcopy(execution)
        result.tool_calls = [
            deepcopy(tc) for tc in self._tool_calls.values()
            if tc.execution_id == execution_id
        ]
        result.tool_calls.sort(key=lambda x: x.created_at)

        return result

    async def list_executions_by_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        agent_type: Optional[AgentType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        """List executions for a case with optional filters."""
        # Filter by case
        executions = [
            e for e in self._executions.values() if e.case_id == case_id
        ]

        # Filter by status
        if status:
            executions = [e for e in executions if e.status == status]

        # Filter by agent type
        if agent_type:
            executions = [e for e in executions if e.agent_type == agent_type]

        # Sort by created_at descending
        executions.sort(key=lambda e: e.created_at, reverse=True)

        total = len(executions)

        # Apply pagination
        paginated = executions[offset : offset + limit]

        # Deep copy and attach tool calls
        result = []
        for e in paginated:
            exec_copy = deepcopy(e)
            exec_copy.tool_calls = [
                deepcopy(tc) for tc in self._tool_calls.values()
                if tc.execution_id == e.execution_id
            ]
            exec_copy.tool_calls.sort(key=lambda x: x.created_at)
            result.append(exec_copy)

        return result, total

    async def update_execution(self, execution: AgentExecution) -> AgentExecution:
        """Update execution status and results."""
        if execution.execution_id not in self._executions:
            raise ValueError(f"Agent execution {execution.execution_id} not found")

        execution.updated_at = datetime.now(timezone.utc)

        # Update stored execution (preserve tool calls)
        stored = deepcopy(execution)
        stored.tool_calls = []
        self._executions[execution.execution_id] = stored

        # Return with tool calls attached
        result = deepcopy(stored)
        result.tool_calls = [
            deepcopy(tc) for tc in self._tool_calls.values()
            if tc.execution_id == execution.execution_id
        ]
        result.tool_calls.sort(key=lambda x: x.created_at)

        return result

    async def delete_execution(self, execution_id: str) -> bool:
        """Delete execution by ID."""
        if execution_id not in self._executions:
            return False

        # Delete execution
        del self._executions[execution_id]

        # Delete associated tool calls (CASCADE)
        to_delete = [
            tc_id for tc_id, tc in self._tool_calls.items()
            if tc.execution_id == execution_id
        ]
        for tc_id in to_delete:
            del self._tool_calls[tc_id]

        return True

    # =========================================================================
    # Tool Call Operations
    # =========================================================================

    async def create_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Create new tool call record in memory."""
        if tool_call.tool_call_id in self._tool_calls:
            raise ValueError(f"Tool call {tool_call.tool_call_id} already exists")

        if tool_call.execution_id not in self._executions:
            raise ValueError(f"Execution {tool_call.execution_id} not found")

        self._tool_calls[tool_call.tool_call_id] = deepcopy(tool_call)
        return deepcopy(tool_call)

    async def update_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Update tool call status and results."""
        if tool_call.tool_call_id not in self._tool_calls:
            raise ValueError(f"Tool call {tool_call.tool_call_id} not found")

        tool_call.updated_at = datetime.now(timezone.utc)
        self._tool_calls[tool_call.tool_call_id] = deepcopy(tool_call)
        return deepcopy(tool_call)

    async def get_tool_calls_for_execution(
        self,
        execution_id: str,
    ) -> List[AgentToolCall]:
        """Get all tool calls for an execution."""
        tool_calls = [
            deepcopy(tc) for tc in self._tool_calls.values()
            if tc.execution_id == execution_id
        ]
        tool_calls.sort(key=lambda x: x.created_at)
        return tool_calls

    # =========================================================================
    # Utility Operations
    # =========================================================================

    async def count_executions_by_case(self, case_id: str) -> int:
        """Count executions for a case."""
        return sum(1 for e in self._executions.values() if e.case_id == case_id)

    async def get_latest_execution(
        self,
        case_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> Optional[AgentExecution]:
        """Get the most recent execution for a case."""
        executions = [
            e for e in self._executions.values() if e.case_id == case_id
        ]

        if agent_type:
            executions = [e for e in executions if e.agent_type == agent_type]

        if not executions:
            return None

        latest = max(executions, key=lambda e: e.created_at)

        # Return with tool calls attached
        result = deepcopy(latest)
        result.tool_calls = [
            deepcopy(tc) for tc in self._tool_calls.values()
            if tc.execution_id == latest.execution_id
        ]
        result.tool_calls.sort(key=lambda x: x.created_at)

        return result

    # =========================================================================
    # Testing Helpers
    # =========================================================================

    def clear(self) -> None:
        """Clear all data (for testing)."""
        self._executions.clear()
        self._tool_calls.clear()

    def delete_executions_for_case(self, case_id: str) -> int:
        """Delete all executions for a case (simulates CASCADE from case delete).

        Returns:
            Number of executions deleted
        """
        to_delete = [
            e_id for e_id, e in self._executions.items() if e.case_id == case_id
        ]

        for e_id in to_delete:
            # Delete associated tool calls
            tc_to_delete = [
                tc_id for tc_id, tc in self._tool_calls.items()
                if tc.execution_id == e_id
            ]
            for tc_id in tc_to_delete:
                del self._tool_calls[tc_id]

            # Delete execution
            del self._executions[e_id]

        return len(to_delete)
