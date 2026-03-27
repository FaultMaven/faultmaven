"""Domain model for Solution entity (TASK-026).

This module defines the domain model for solutions in the investigation workflow.
The Solution represents a proposed or implemented fix for a validated hypothesis.

Design: Framework-agnostic domain model for business logic and testing.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class SolutionStatus(str, Enum):
    """Solution lifecycle status.

    Status flow:
    - PROPOSED: Initial state when solution is created
    - IN_PROGRESS: Solution implementation has started
    - IMPLEMENTED: Solution has been applied
    - VERIFIED: Solution confirmed effective
    - REJECTED: Solution rejected or rolled back
    """

    PROPOSED = "proposed"
    IN_PROGRESS = "in_progress"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    REJECTED = "rejected"


class RiskLevel(str, Enum):
    """Solution risk level classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Solution:
    """Domain model for investigation solution.

    Represents a proposed or implemented fix for a validated hypothesis,
    with implementation steps and risk assessment.

    Attributes:
        solution_id: Unique identifier
        case_id: Associated case identifier
        organization_id: Organization identifier (multi-tenancy)
        hypothesis_id: Linked hypothesis identifier (optional)
        title: Short solution title
        description: Detailed solution description
        implementation_steps: Ordered list of implementation steps
        status: Current solution status
        risk_level: Risk assessment (low, medium, high, critical)
        estimated_effort: Estimated implementation effort
        created_by: User who created the solution
        created_at: Creation timestamp
        updated_at: Last update timestamp
        updated_by: User who last updated (optional)
        implemented_at: Implementation timestamp (optional)
        verification_result: Verification findings (optional)
        verification_timestamp: When verified (optional)
        metadata: Additional metadata (rollback plan, dependencies, etc.)
    """

    solution_id: str
    case_id: str
    organization_id: str
    title: str
    description: str
    implementation_steps: list[str]
    status: SolutionStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    hypothesis_id: str | None = None
    risk_level: RiskLevel | None = None
    estimated_effort: str | None = None
    updated_by: str | None = None
    implemented_at: datetime | None = None
    verification_result: str | None = None
    verification_timestamp: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate solution after initialization."""
        # Convert string status to enum if needed
        if isinstance(self.status, str):
            self.status = SolutionStatus(self.status)

        # Convert string risk_level to enum if needed
        if self.risk_level is not None and isinstance(self.risk_level, str):
            self.risk_level = RiskLevel(self.risk_level)

        # Validate description length
        if not self.description or len(self.description.strip()) < 20:
            raise ValueError("Description must be at least 20 characters")
        if len(self.description) > 5000:
            raise ValueError("Description must not exceed 5000 characters")

        # Validate implementation steps
        if not self.implementation_steps or len(self.implementation_steps) < 1:
            raise ValueError("At least one implementation step is required")

    @classmethod
    def from_dict(cls, data: dict) -> "Solution":
        """Create Solution from dictionary.

        Args:
            data: Dictionary containing solution data

        Returns:
            Solution instance
        """
        # Handle metadata extraction for implementation_steps
        metadata = data.get("metadata", {})
        implementation_steps = data.get("implementation_steps")
        if implementation_steps is None and isinstance(metadata, dict):
            implementation_steps = metadata.get("implementation_steps", [])

        return cls(
            solution_id=data["solution_id"],
            case_id=data["case_id"],
            organization_id=data["organization_id"],
            hypothesis_id=data.get("hypothesis_id"),
            title=data.get("title", ""),
            description=data["description"],
            implementation_steps=implementation_steps,
            status=SolutionStatus(data["status"]),
            risk_level=(
                RiskLevel(data["risk_level"]) if data.get("risk_level") else None
            ),
            estimated_effort=data.get("estimated_effort"),
            created_by=data["created_by"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            updated_by=data.get("updated_by"),
            implemented_at=data.get("implemented_at"),
            verification_result=data.get("verification_result"),
            verification_timestamp=data.get("verification_timestamp"),
            metadata=metadata,
        )

    def to_dict(self) -> dict:
        """Convert Solution to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "solution_id": self.solution_id,
            "case_id": self.case_id,
            "organization_id": self.organization_id,
            "hypothesis_id": self.hypothesis_id,
            "title": self.title,
            "description": self.description,
            "implementation_steps": self.implementation_steps,
            "status": (
                self.status.value
                if isinstance(self.status, SolutionStatus)
                else self.status
            ),
            "risk_level": (
                self.risk_level.value
                if isinstance(self.risk_level, RiskLevel)
                else self.risk_level
            ),
            "estimated_effort": self.estimated_effort,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "implemented_at": self.implemented_at,
            "verification_result": self.verification_result,
            "verification_timestamp": self.verification_timestamp,
            "metadata": self.metadata,
        }

    def can_transition_to_implemented(self) -> bool:
        """Check if solution can transition to implemented status.

        Business rule: Must be in proposed or in_progress status

        Returns:
            True if transition is allowed
        """
        return self.status in [SolutionStatus.PROPOSED, SolutionStatus.IN_PROGRESS]

    def can_transition_to_verified(self) -> bool:
        """Check if solution can transition to verified status.

        Business rule: Must be implemented first

        Returns:
            True if transition is allowed
        """
        return self.status == SolutionStatus.IMPLEMENTED

    def mark_implemented(self) -> None:
        """Mark solution as implemented with timestamp."""
        self.status = SolutionStatus.IMPLEMENTED
        self.implemented_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

    def mark_verified(self, verification_result: str) -> None:
        """Mark solution as verified with result.

        Args:
            verification_result: Verification findings
        """
        if not self.can_transition_to_verified():
            raise ValueError("Solution must be implemented before verification")

        self.status = SolutionStatus.VERIFIED
        self.verification_result = verification_result
        self.verification_timestamp = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
