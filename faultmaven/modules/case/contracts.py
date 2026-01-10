"""Case Module Contracts

This module defines the public interfaces (contracts) for the Case vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import Protocol, Optional, List, Dict, Any, TYPE_CHECKING
from abc import ABC

if TYPE_CHECKING:
    from faultmaven.modules.case.domain.models import Case, CaseStatus
    from faultmaven.modules.report.domain.models import CaseReport, ReportType
    from faultmaven.modules.evidence.domain.models import EvidenceArtifact, EvidenceListFilter
    from faultmaven.modules.agent.domain.models.agent_execution import (
        AgentExecution,
        AgentToolCall,
        ExecutionStatus,
        AgentType,
    )
    from uuid import UUID


# ============================================================
# Repository Contract
# ============================================================

class ICaseRepository(Protocol):
    """
    Repository interface for Case persistence operations.
    
    This is a Protocol (structural typing) that allows any implementation
    that matches this interface to be used. Concrete implementations are:
    - CaseRepository (abstract base class in infrastructure/case_repository.py)
    - InMemoryCaseRepository
    - PostgreSQLHybridCaseRepository
    """
    
    async def save(self, case: 'Case') -> 'Case':
        """Save case to persistence layer."""
        ...
    
    async def get(self, case_id: str) -> Optional['Case']:
        """Retrieve case by ID."""
        ...
    
    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional['CaseStatus'] = None,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List['Case'], int]:
        """List cases with optional filters."""
        ...
    
    async def delete(self, case_id: str) -> bool:
        """Delete case by ID."""
        ...
    
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20
    ) -> tuple[List['Case'], int]:
        """Search cases by text query."""
        ...
    
    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add a message to a case."""
        ...
    
    async def get_messages(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[dict]:
        """Get messages for a case with pagination."""
        ...
    
    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update case last_activity_at timestamp."""
        ...
    
    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for a case."""
        ...
    
    async def cleanup_expired(self, max_age_days: int = 90, batch_size: int = 100) -> int:
        """Clean up expired/old cases."""
        ...
    
    # Report operations (TD-001: reports stored via Case repository)
    async def add_report(self, report: 'CaseReport') -> 'CaseReport':
        """Save report to persistence layer."""
        ...
    
    async def get_report(self, report_id: str) -> Optional['CaseReport']:
        """Retrieve a report by ID."""
        ...
    
    async def get_reports(
        self,
        case_id: str,
        report_type: Optional['ReportType'] = None,
        include_history: bool = False,
        only_current: bool = False
    ) -> List['CaseReport']:
        """Get reports for a case with optional filtering."""
        ...
    
    async def update_report(self, report: 'CaseReport') -> 'CaseReport':
        """Update an existing report."""
        ...
    
    async def delete_report(self, report_id: str) -> bool:
        """Delete a report by ID."""
        ...
    
    # Standalone Evidence Operations (migrated from Evidence module)
    async def create_standalone_evidence(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: 'UUID',
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> 'EvidenceArtifact':
        """Create standalone evidence record (can link to multiple cases)."""
        ...
    
    async def get_standalone_evidence(self, evidence_id: 'UUID') -> Optional['EvidenceArtifact']:
        """Get standalone evidence by ID."""
        ...
    
    async def list_standalone_evidence(
        self,
        filters: 'EvidenceListFilter'
    ) -> tuple[List['EvidenceArtifact'], int]:
        """List standalone evidence with filters."""
        ...
    
    async def delete_standalone_evidence(self, evidence_id: 'UUID') -> bool:
        """Delete standalone evidence record."""
        ...
    
    async def link_standalone_evidence_to_case(
        self,
        evidence_id: 'UUID',
        case_id: 'UUID'
    ) -> Optional['EvidenceArtifact']:
        """Link standalone evidence to a case."""
        ...
    
    # Agent Execution Operations (migrated from Agent module)
    async def create_agent_execution(self, execution: 'AgentExecution') -> 'AgentExecution':
        """Create new agent execution record."""
        ...
    
    async def get_agent_execution(self, execution_id: str) -> Optional['AgentExecution']:
        """Get agent execution by ID with tool calls loaded."""
        ...
    
    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional['ExecutionStatus'] = None,
        agent_type: Optional['AgentType'] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List['AgentExecution'], int]:
        """List agent executions for a case with optional filters."""
        ...
    
    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional['ExecutionStatus'] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List['AgentExecution'], int]:
        """List agent executions for a session with optional filters."""
        ...
    
    async def update_agent_execution(self, execution: 'AgentExecution') -> 'AgentExecution':
        """Update agent execution status and results."""
        ...
    
    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution by ID (cascades to tool calls)."""
        ...
    
    async def create_agent_tool_call(self, tool_call: 'AgentToolCall') -> 'AgentToolCall':
        """Create new agent tool call record."""
        ...
    
    async def update_agent_tool_call(self, tool_call: 'AgentToolCall') -> 'AgentToolCall':
        """Update agent tool call status and results."""
        ...
    
    async def get_agent_tool_calls_for_execution(
        self,
        execution_id: str,
    ) -> List['AgentToolCall']:
        """Get all tool calls for an execution."""
        ...
    
    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case."""
        ...
    
    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional['AgentType'] = None,
    ) -> Optional['AgentExecution']:
        """Get the most recent agent execution for a case."""
        ...


# ============================================================
# Service Contract
# ============================================================

class ICaseService(ABC):
    """
    Service interface for Case business logic and orchestration.
    
    This interface defines the contract for case management business operations,
    coordinating between case storage, session management, and other services.
    """
    
    # Note: Using ABC here because ICaseService is already defined in models/interfaces_case.py
    # We'll import and re-export it, or define a simplified version here.
    # For now, we'll reference the existing interface.
    pass


# ============================================================
# Import and Re-export existing interfaces from models
# ============================================================

# Re-export ICaseService from models/interfaces_case for backward compatibility
# Eventually, this should be migrated fully to contracts.py
from faultmaven.models.interfaces_case import ICaseService as _ICaseService
ICaseService = _ICaseService  # Re-export with same name


# ============================================================
# DTOs (Data Transfer Objects)
# ============================================================

# Case domain model can be used directly as DTO
# If specific DTOs are needed, they can be added here
