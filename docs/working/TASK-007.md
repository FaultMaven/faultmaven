# TASK-007: Agent Execution Repository Pattern

## Task Metadata
- **Phase**: Week 2, Day 4-5 (Modular Foundation - Agent Execution Tracking)
- **Priority**: P1 (Critical for agent transparency and debugging)
- **Estimated Time**: 3-4 hours
- **Dependencies**:
  - TASK-001 (Alembic migration infrastructure) ✅ Complete
  - TASK-002 (Case Repository pattern) ✅ Complete
  - TASK-003 (Session Management integration) ✅ Complete
  - TASK-004 (Minimal Shim Pattern) ✅ Complete
  - TASK-005 (Performance Baseline Suite) ✅ Complete
  - TASK-006 (Evidence Artifact Repository) ✅ Complete
- **Assignee**: Developer
- **Reviewer**: Test-Engineer + Solutions Architect

## Objective

**Implement the Agent Execution Repository to track AI agent execution history, tool calls, and results** for debugging, transparency, and audit compliance.

### Success Criteria

1. ✅ AgentExecution domain model defined
2. ✅ AgentToolCall domain model defined (nested executions)
3. ✅ Alembic migration creates agent_executions and agent_tool_calls tables
4. ✅ Database repository implementation (async SQLAlchemy)
5. ✅ In-memory repository implementation (testing)
6. ✅ Foreign key relationships (executions → cases, tool_calls → executions)
7. ✅ Execution status tracking (queued, running, completed, failed)
8. ✅ Tool call result storage (success/failure, output, errors)
9. ✅ Comprehensive tests (unit + integration, 80%+ coverage)
10. ✅ Performance benchmarks added
11. ✅ Documentation updated

---

## Context

### Evolution Strategy Alignment

From the FaultMaven roadmap:

> **Week 2: Modular Foundation**
> - Agent Repository (execution tracking, result storage)
> - Enable agent behavior debugging and transparency
> - Track tool calls, errors, and execution time

This task establishes the **Agent Execution Repository** to provide:
- **Execution Transparency**: What did the agent do? Why?
- **Debugging**: Which tool calls failed? What were the errors?
- **Audit Trail**: Complete history of agent actions
- **Performance Monitoring**: Execution duration, token usage

### Why Agent Execution Tracking Matters

AI agents are powerful but opaque. Users need visibility into:
- **Tool Calls**: Which tools did the agent use? (web_search, code_exec, file_read)
- **Failures**: Why did the agent fail? What errors occurred?
- **Costs**: How many tokens were used? What was the execution time?
- **Audit**: Complete history for compliance and debugging

Agent execution tracking enables:
1. **Debugging**: Understand why an agent failed or produced unexpected results
2. **Transparency**: Show users exactly what the agent did
3. **Optimization**: Identify slow tool calls or high token usage
4. **Compliance**: Maintain audit trail for regulatory requirements

---

## Domain Models

### AgentExecution Entity

**File**: `faultmaven/models/agent_execution.py`

```python
"""Agent execution domain model.

Represents a single execution of an AI agent for a case.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from enum import Enum


class ExecutionStatus(str, Enum):
    """Agent execution status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class AgentType(str, Enum):
    """Types of AI agents."""

    INVESTIGATOR = "investigator"  # Root cause analysis
    DEBUGGER = "debugger"  # Code debugging
    RESEARCHER = "researcher"  # Information gathering
    VALIDATOR = "validator"  # Hypothesis validation
    REPORTER = "reporter"  # Report generation
    CUSTOM = "custom"  # User-defined agents


@dataclass
class AgentExecution:
    """Agent execution for a case.

    Represents a single execution of an AI agent, tracking the full lifecycle
    from queued to completion or failure.

    Attributes:
        execution_id: Unique identifier
        case_id: Case this execution belongs to
        agent_type: Type of agent (investigator, debugger, etc.)
        agent_model: LLM model used (e.g., "gpt-4", "claude-3-opus")
        status: Current execution status
        started_at: When execution started
        completed_at: When execution completed (success or failure)
        execution_duration_ms: Total execution time in milliseconds
        prompt: Prompt sent to the agent
        response: Agent's response
        error_message: Error message if execution failed
        token_usage: Token usage statistics (prompt_tokens, completion_tokens, total_tokens)
        tool_calls: List of tool calls made during execution
        metadata: Additional execution-specific metadata (JSON)
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    execution_id: str
    case_id: str
    agent_type: AgentType
    agent_model: str
    status: ExecutionStatus = ExecutionStatus.QUEUED
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_ms: Optional[int] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    error_message: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None  # {prompt_tokens, completion_tokens, total_tokens}
    tool_calls: List["AgentToolCall"] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate agent execution data."""
        if not self.execution_id:
            raise ValueError("execution_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not self.agent_model:
            raise ValueError("agent_model is required")

    def is_completed(self) -> bool:
        """Check if execution is in a terminal state."""
        return self.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        )

    def is_running(self) -> bool:
        """Check if execution is currently running."""
        return self.status == ExecutionStatus.RUNNING

    def mark_started(self) -> None:
        """Mark execution as started."""
        self.status = ExecutionStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def mark_completed(self, response: str) -> None:
        """Mark execution as successfully completed."""
        self.status = ExecutionStatus.COMPLETED
        self.completed_at = datetime.now(timezone.utc)
        self.response = response
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_failed(self, error_message: str) -> None:
        """Mark execution as failed."""
        self.status = ExecutionStatus.FAILED
        self.completed_at = datetime.now(timezone.utc)
        self.error_message = error_message
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_cancelled(self) -> None:
        """Mark execution as cancelled."""
        self.status = ExecutionStatus.CANCELLED
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def _calculate_duration(self) -> None:
        """Calculate execution duration if both timestamps are set."""
        if self.started_at and self.completed_at:
            duration = self.completed_at - self.started_at
            self.execution_duration_ms = int(duration.total_seconds() * 1000)

    def add_tool_call(self, tool_call: "AgentToolCall") -> None:
        """Add a tool call to this execution."""
        self.tool_calls.append(tool_call)

    def get_total_tokens(self) -> int:
        """Get total token usage."""
        if self.token_usage:
            return self.token_usage.get("total_tokens", 0)
        return 0

    def __repr__(self) -> str:
        return (
            f"AgentExecution(execution_id={self.execution_id!r}, "
            f"case_id={self.case_id!r}, "
            f"agent_type={self.agent_type.value!r}, "
            f"status={self.status.value!r})"
        )


@dataclass
class AgentToolCall:
    """Tool call made during agent execution.

    Represents a single tool invocation (e.g., web_search, code_exec, file_read).

    Attributes:
        tool_call_id: Unique identifier
        execution_id: Parent execution this tool call belongs to
        tool_name: Name of the tool (web_search, code_exec, etc.)
        tool_input: Input parameters passed to the tool (JSON)
        tool_output: Output returned by the tool (JSON)
        status: Tool call status (success, failed)
        error_message: Error message if tool call failed
        started_at: When tool call started
        completed_at: When tool call completed
        duration_ms: Tool call duration in milliseconds
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    tool_call_id: str
    execution_id: str
    tool_name: str
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[Dict[str, Any]] = None
    status: str = "pending"  # pending, success, failed
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate tool call data."""
        if not self.tool_call_id:
            raise ValueError("tool_call_id is required")
        if not self.execution_id:
            raise ValueError("execution_id is required")
        if not self.tool_name:
            raise ValueError("tool_name is required")

    def mark_started(self) -> None:
        """Mark tool call as started."""
        self.status = "running"
        self.started_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def mark_success(self, output: Dict[str, Any]) -> None:
        """Mark tool call as successful."""
        self.status = "success"
        self.tool_output = output
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_failed(self, error_message: str) -> None:
        """Mark tool call as failed."""
        self.status = "failed"
        self.error_message = error_message
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def _calculate_duration(self) -> None:
        """Calculate tool call duration."""
        if self.started_at and self.completed_at:
            duration = self.completed_at - self.started_at
            self.duration_ms = int(duration.total_seconds() * 1000)

    def __repr__(self) -> str:
        return (
            f"AgentToolCall(tool_call_id={self.tool_call_id!r}, "
            f"tool_name={self.tool_name!r}, "
            f"status={self.status!r})"
        )
```

---

## Database Schema

### Alembic Migration

**File**: `alembic/versions/20251229_1800_004_add_agent_executions.py`

```python
"""Add agent execution tracking.

Revision ID: 004
Revises: 003
Create Date: 2025-12-29 18:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# Revision identifiers
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Apply agent execution tracking migration."""
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """Remove agent execution tracking schema objects."""
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for agent executions."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: agent_executions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_executions (
            execution_id VARCHAR(64) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL,
            agent_type VARCHAR(64) NOT NULL,
            agent_model VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            execution_duration_ms INTEGER,
            prompt TEXT,
            response TEXT,
            error_message TEXT,
            token_usage JSONB,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_executions_status_valid CHECK (
                status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timeout')
            ),
            CONSTRAINT agent_executions_agent_type_valid CHECK (
                agent_type IN ('investigator', 'debugger', 'researcher', 'validator', 'reporter', 'custom')
            ),
            CONSTRAINT agent_executions_duration_non_negative CHECK (
                execution_duration_ms IS NULL OR execution_duration_ms >= 0
            )
        )
    """))

    # Indexes for agent_executions
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_case_id "
        "ON agent_executions(case_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_status "
        "ON agent_executions(status)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_created_at "
        "ON agent_executions(created_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_agent_type "
        "ON agent_executions(agent_type)"
    ))

    # Foreign key to cases
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_agent_executions_case_id'
            ) THEN
                ALTER TABLE agent_executions
                ADD CONSTRAINT fk_agent_executions_case_id
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Table: agent_tool_calls
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_tool_calls (
            tool_call_id VARCHAR(64) PRIMARY KEY,
            execution_id VARCHAR(64) NOT NULL,
            tool_name VARCHAR(128) NOT NULL,
            tool_input JSONB,
            tool_output JSONB,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            duration_ms INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_tool_calls_status_valid CHECK (
                status IN ('pending', 'running', 'success', 'failed')
            ),
            CONSTRAINT agent_tool_calls_duration_non_negative CHECK (
                duration_ms IS NULL OR duration_ms >= 0
            )
        )
    """))

    # Indexes for agent_tool_calls
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_execution_id "
        "ON agent_tool_calls(execution_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_tool_name "
        "ON agent_tool_calls(tool_name)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_status "
        "ON agent_tool_calls(status)"
    ))

    # Foreign key to agent_executions
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_agent_tool_calls_execution_id'
            ) THEN
                ALTER TABLE agent_tool_calls
                ADD CONSTRAINT fk_agent_tool_calls_execution_id
                FOREIGN KEY (execution_id) REFERENCES agent_executions(execution_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # Triggers for auto-update timestamps
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_agent_executions_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS agent_executions_update_timestamp ON agent_executions;
        CREATE TRIGGER agent_executions_update_timestamp
            BEFORE UPDATE ON agent_executions
            FOR EACH ROW
            EXECUTE FUNCTION update_agent_executions_updated_at()
    """))

    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_agent_tool_calls_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS agent_tool_calls_update_timestamp ON agent_tool_calls;
        CREATE TRIGGER agent_tool_calls_update_timestamp
            BEFORE UPDATE ON agent_tool_calls
            FOR EACH ROW
            EXECUTE FUNCTION update_agent_tool_calls_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL agent execution objects."""
    conn = op.get_bind()

    # Drop triggers
    conn.execute(text("DROP TRIGGER IF EXISTS agent_tool_calls_update_timestamp ON agent_tool_calls CASCADE"))
    conn.execute(text("DROP TRIGGER IF EXISTS agent_executions_update_timestamp ON agent_executions CASCADE"))

    # Drop functions
    conn.execute(text("DROP FUNCTION IF EXISTS update_agent_tool_calls_updated_at() CASCADE"))
    conn.execute(text("DROP FUNCTION IF EXISTS update_agent_executions_updated_at() CASCADE"))

    # Drop tables (CASCADE handles foreign keys)
    conn.execute(text("DROP TABLE IF EXISTS agent_tool_calls CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS agent_executions CASCADE"))


# =============================================================================
# SQLite Implementation
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for agent executions."""

    # Table: agent_executions
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(17), nullable=False),
        sa.Column("agent_type", sa.String(64), nullable=False),
        sa.Column("agent_model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("execution_duration_ms", sa.Integer, nullable=True),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("response", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("token_usage", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("metadata", sa.Text, server_default="{}"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.case_id"],
            name="fk_agent_executions_case_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for agent_executions
    op.create_index("idx_agent_executions_case_id", "agent_executions", ["case_id"])
    op.create_index("idx_agent_executions_status", "agent_executions", ["status"])
    op.create_index("idx_agent_executions_created_at", "agent_executions", ["created_at"])
    op.create_index("idx_agent_executions_agent_type", "agent_executions", ["agent_type"])

    # Table: agent_tool_calls
    op.create_table(
        "agent_tool_calls",
        sa.Column("tool_call_id", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_input", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("tool_output", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_executions.execution_id"],
            name="fk_agent_tool_calls_execution_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for agent_tool_calls
    op.create_index("idx_agent_tool_calls_execution_id", "agent_tool_calls", ["execution_id"])
    op.create_index("idx_agent_tool_calls_tool_name", "agent_tool_calls", ["tool_name"])
    op.create_index("idx_agent_tool_calls_status", "agent_tool_calls", ["status"])


def _downgrade_sqlite() -> None:
    """Remove SQLite agent execution objects."""
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_executions")
```

**Migration Design Notes:**

1. **Two Tables**: agent_executions (parent) + agent_tool_calls (child)
2. **CASCADE Delete**: Executions deleted when case deleted, tool calls deleted when execution deleted
3. **JSONB for PostgreSQL**: token_usage, tool_input, tool_output stored as JSONB
4. **Status Constraints**: Check constraints for valid enum values
5. **Indexes**: Optimized for queries by case, status, created_at, tool_name

---

## Repository Interface

(Due to length constraints, I'll provide the key signatures)

**File**: `faultmaven/infrastructure/persistence/agent_execution_repository.py`

```python
class AgentExecutionRepository(ABC):
    """Abstract repository for agent execution management."""

    @abstractmethod
    async def create_execution(self, execution: AgentExecution) -> AgentExecution:
        """Create new agent execution record."""
        pass

    @abstractmethod
    async def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get execution by ID with tool calls loaded."""
        pass

    @abstractmethod
    async def list_executions_by_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        """List executions for a case with optional status filter."""
        pass

    @abstractmethod
    async def update_execution(self, execution: AgentExecution) -> AgentExecution:
        """Update execution status and results."""
        pass

    @abstractmethod
    async def delete_execution(self, execution_id: str) -> bool:
        """Delete execution by ID."""
        pass

    @abstractmethod
    async def create_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Create new tool call record."""
        pass

    @abstractmethod
    async def update_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Update tool call status and results."""
        pass

    @abstractmethod
    async def get_tool_calls_for_execution(self, execution_id: str) -> List[AgentToolCall]:
        """Get all tool calls for an execution."""
        pass
```

---

## Performance Benchmarks

**Performance Targets:**
- Execution creation: < 150ms p95
- Execution retrieval (with tool calls): < 200ms p95
- List executions by case: < 150ms for 20 executions
- Tool call creation: < 100ms p95
- Update execution status: < 100ms p95

---

## Deliverables

### Code Files (10)

1. **Domain Models**
   - `faultmaven/models/agent_execution.py` - AgentExecution and AgentToolCall entities

2. **Database Migration**
   - `alembic/versions/20251229_1800_004_add_agent_executions.py` - Create agent execution tables

3. **Repository Interface**
   - `faultmaven/infrastructure/persistence/agent_execution_repository.py` - Abstract repository

4. **Repository Implementations**
   - Database and In-Memory implementations

5. **ORM Models**
   - Update `faultmaven/infrastructure/persistence/models.py` - Add AgentExecutionModel and AgentToolCallModel

6. **Repository Factory**
   - Update factory with agent execution repository creation

### Test Files (4)

7. **Unit Tests**
   - Domain model tests
   - Repository tests

8. **Integration Tests**
   - CASCADE delete tests (case → executions → tool calls)

9. **Performance Benchmarks**
   - Execution operation benchmarks

---

## Acceptance Criteria

### Functional Requirements

- [x] AgentExecution domain model with status lifecycle methods
- [x] AgentToolCall domain model for tool invocations
- [x] ExecutionStatus and AgentType enums
- [x] Alembic migration creates both tables with CASCADE delete
- [x] Database repository implements all operations
- [x] In-memory repository for testing
- [x] Tool call result storage (input/output/errors)
- [x] Token usage tracking

### Testing Requirements

- [x] Unit tests achieve 80%+ coverage
- [x] Integration tests verify CASCADE delete chain (case → executions → tool calls)
- [x] Performance benchmarks added
- [x] All tests pass locally and in CI

---

**Ready to implement?** Follow TASK-002 and TASK-006 patterns. This completes Week 2, Day 4-5 of the evolution strategy.
