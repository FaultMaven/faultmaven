"""Domain model for Hypothesis entity (TASK-026).

This module defines the domain model for hypotheses in the investigation workflow.
The Hypothesis represents a proposed explanation for a problem's root cause.

Design: Framework-agnostic domain model for business logic and testing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class HypothesisStatus(str, Enum):
    """Hypothesis lifecycle status.

    Status flow:
    - PROPOSED: Initial state when hypothesis is created
    - TESTING: Hypothesis is being actively tested/validated
    - VALIDATED: Hypothesis confirmed with confidence >= 0.7
    - INVALIDATED: Hypothesis refuted with confidence <= 0.3
    - DEFERRED: Hypothesis postponed for later investigation
    """

    PROPOSED = "proposed"
    TESTING = "testing"
    VALIDATED = "validated"
    INVALIDATED = "invalidated"
    DEFERRED = "deferred"


@dataclass
class Hypothesis:
    """Domain model for investigation hypothesis.

    Represents a proposed explanation for a problem's root cause with
    supporting evidence and confidence scoring.

    Attributes:
        hypothesis_id: Unique identifier
        case_id: Associated case identifier
        organization_id: Organization identifier (multi-tenancy)
        title: Short hypothesis title
        description: Detailed hypothesis description
        confidence: Confidence score (0.0-1.0)
        status: Current hypothesis status
        evidence_supporting: List of supporting evidence IDs
        evidence_against: List of contradicting evidence IDs
        created_by: User who created the hypothesis
        created_at: Creation timestamp
        updated_at: Last update timestamp
        updated_by: User who last updated (optional)
        validation_result: Validation findings (optional)
        validation_timestamp: When validated (optional)
        metadata: Additional metadata
    """

    hypothesis_id: str
    case_id: str
    organization_id: str
    title: str
    description: str
    confidence: float
    status: HypothesisStatus
    evidence_supporting: list[str]
    evidence_against: list[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None
    validation_result: str | None = None
    validation_timestamp: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate hypothesis after initialization."""
        # Convert string status to enum if needed
        if isinstance(self.status, str):
            self.status = HypothesisStatus(self.status)

        # Validate confidence range
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(
                f"Confidence must be between 0.0 and 1.0, got {self.confidence}"
            )

        # Validate description length
        if not self.description or len(self.description.strip()) < 10:
            raise ValueError("Description must be at least 10 characters")
        if len(self.description) > 5000:
            raise ValueError("Description must not exceed 5000 characters")

    @classmethod
    def from_dict(cls, data: dict) -> "Hypothesis":
        """Create Hypothesis from dictionary.

        Args:
            data: Dictionary containing hypothesis data

        Returns:
            Hypothesis instance
        """
        return cls(
            hypothesis_id=data["hypothesis_id"],
            case_id=data["case_id"],
            organization_id=data["organization_id"],
            title=data.get("title", ""),
            description=data["description"],
            confidence=(
                float(data["confidence"]) if data.get("confidence") is not None else 0.5
            ),
            status=HypothesisStatus(data["status"]),
            evidence_supporting=data.get("evidence_supporting", []),
            evidence_against=data.get("evidence_against", []),
            created_by=data["created_by"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            updated_by=data.get("updated_by"),
            validation_result=data.get("validation_result"),
            validation_timestamp=data.get("validation_timestamp"),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> dict:
        """Convert Hypothesis to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "hypothesis_id": self.hypothesis_id,
            "case_id": self.case_id,
            "organization_id": self.organization_id,
            "title": self.title,
            "description": self.description,
            "confidence": self.confidence,
            "status": (
                self.status.value
                if isinstance(self.status, HypothesisStatus)
                else self.status
            ),
            "evidence_supporting": self.evidence_supporting,
            "evidence_against": self.evidence_against,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "validation_result": self.validation_result,
            "validation_timestamp": self.validation_timestamp,
            "metadata": self.metadata,
        }

    def can_transition_to_validated(self) -> bool:
        """Check if hypothesis can transition to validated status.

        Business rule: Confidence must be >= 0.7

        Returns:
            True if transition is allowed
        """
        return self.confidence >= 0.7

    def can_transition_to_invalidated(self) -> bool:
        """Check if hypothesis can transition to invalidated status.

        Business rule: Confidence must be <= 0.3

        Returns:
            True if transition is allowed
        """
        return self.confidence <= 0.3

    def can_have_solutions(self) -> bool:
        """Check if hypothesis can have linked solutions.

        Business rule: Only validated hypotheses can have solutions

        Returns:
            True if solutions can be linked
        """
        return self.status == HypothesisStatus.VALIDATED
