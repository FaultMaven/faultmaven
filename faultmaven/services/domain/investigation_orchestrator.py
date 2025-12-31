"""Investigation Orchestrator Service (TASK-026)

Purpose: Business logic layer for hypothesis-solution workflow orchestration.

This service coordinates the investigation lifecycle between hypotheses and solutions,
enforcing business rules and managing status transitions. It sits between the API layer
and repository layer, providing a clean separation of concerns.

Core Responsibilities:
- Create and manage hypotheses with confidence scoring
- Enforce confidence-based status transitions (testing → confirmed/rejected)
- Link solutions to validated hypotheses
- Track investigation progress across cases
- Validate business rules (confidence thresholds, linking constraints)

Business Rules:
- Hypotheses can transition to "validated" only with confidence ≥ 0.7
- Hypotheses can transition to "refuted" only with confidence ≤ 0.3
- Solutions can only link to "validated" hypotheses
- All operations enforce multi-tenant isolation via organization_id

Integration Points:
- HypothesisRepository: CRUD operations for hypotheses
- SolutionRepository: CRUD operations for solutions
- AgentService: (Future) AI-driven hypothesis generation

Design: Follows TASK-024 service layer patterns with repository abstraction.
"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import uuid4

from faultmaven.infrastructure.persistence.hypothesis_repository import HypothesisRepository
from faultmaven.infrastructure.persistence.solution_repository import SolutionRepository
from faultmaven.exceptions import (
    ValidationException,
    NotFoundError,
    AuthorizationError,
    ConflictError,
)

logger = logging.getLogger(__name__)


class InvestigationOrchestrator:
    """
    Orchestrates investigation workflow: hypotheses → solutions.

    This service manages the business logic for the investigation process,
    including hypothesis lifecycle management, confidence-based validation,
    and solution linking.

    Attributes:
        hypothesis_repo: Repository for hypothesis persistence
        solution_repo: Repository for solution persistence
    """

    def __init__(
        self,
        hypothesis_repo: HypothesisRepository,
        solution_repo: SolutionRepository,
    ):
        """
        Initialize Investigation Orchestrator.

        Args:
            hypothesis_repo: Hypothesis repository implementation
            solution_repo: Solution repository implementation
        """
        self.hypothesis_repo = hypothesis_repo
        self.solution_repo = solution_repo

        logger.info("InvestigationOrchestrator initialized")

    async def create_hypothesis(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        confidence: float = 0.5,
        supporting_evidence_ids: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Create new investigation hypothesis with validation.

        Validates input parameters and creates a hypothesis in "captured" status.
        The hypothesis will begin with default confidence (0.5) unless specified.

        Args:
            case_id: Case identifier this hypothesis belongs to
            organization_id: Organization identifier (multi-tenant isolation)
            description: Hypothesis description (10-5000 characters)
            created_by: User ID who created the hypothesis
            confidence: Initial confidence score (0.0-1.0, default 0.5)
            supporting_evidence_ids: List of evidence IDs supporting hypothesis
            metadata: Additional metadata (source, rationale, etc.)

        Returns:
            Created hypothesis object

        Raises:
            ValidationException: If validation fails (confidence out of range, description too short/long)
        """
        # Validate description length
        if len(description) < 10:
            raise ValidationException(
                "Hypothesis description must be at least 10 characters",
                details={"description_length": len(description), "min_length": 10}
            )

        if len(description) > 5000:
            raise ValidationException(
                "Hypothesis description cannot exceed 5000 characters",
                details={"description_length": len(description), "max_length": 5000}
            )

        # Validate confidence range
        if not (0.0 <= confidence <= 1.0):
            raise ValidationException(
                "Confidence score must be between 0.0 and 1.0",
                details={"confidence": confidence, "valid_range": "0.0-1.0"}
            )

        logger.info(
            f"Creating hypothesis for case {case_id}",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "created_by": created_by,
                "confidence": confidence,
            }
        )

        # Create hypothesis via repository
        hypothesis = await self.hypothesis_repo.create_hypothesis(
            case_id=case_id,
            organization_id=organization_id,
            description=description,
            created_by=created_by,
            status="captured",  # All new hypotheses start in "captured" status
            confidence_score=Decimal(str(confidence)),
            supporting_evidence_ids=supporting_evidence_ids or [],
            metadata=metadata or {},
        )

        logger.info(
            f"Hypothesis created successfully: {hypothesis['hypothesis_id']}",
            extra={"hypothesis_id": hypothesis["hypothesis_id"]}
        )

        return hypothesis

    async def update_hypothesis_status(
        self,
        hypothesis_id: str,
        organization_id: str,
        new_status: str,
        updated_by: str,
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Update hypothesis status with confidence-based validation.

        Enforces business rules for status transitions:
        - Can only transition to "validated" if confidence ≥ 0.7
        - Can only transition to "refuted" if confidence ≤ 0.3
        - Other transitions allowed without confidence constraints

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (multi-tenant isolation)
            new_status: New status (captured, active, validated, refuted, inconclusive, retired)
            updated_by: User ID performing the update
            confidence: Optional new confidence score (0.0-1.0)

        Returns:
            Updated hypothesis object

        Raises:
            NotFoundError: If hypothesis doesn't exist
            AuthorizationError: If organization_id doesn't match
            ValidationException: If confidence out of range
            ConflictError: If status transition violates business rules
        """
        # Get existing hypothesis
        hypothesis = await self.hypothesis_repo.get_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        if not hypothesis:
            raise NotFoundError(
                resource_type="Hypothesis",
                resource_id=hypothesis_id
            )

        # Validate confidence if provided
        if confidence is not None and not (0.0 <= confidence <= 1.0):
            raise ValidationException(
                "Confidence score must be between 0.0 and 1.0",
                details={"confidence": confidence, "valid_range": "0.0-1.0"}
            )

        # Get current confidence (use provided or existing)
        current_confidence = float(hypothesis.get("confidence_score", 0.5)) if hypothesis.get("confidence_score") else 0.5
        target_confidence = confidence if confidence is not None else current_confidence

        # Enforce business rules for status transitions
        if new_status == "validated" and target_confidence < 0.7:
            raise ConflictError(
                f"Cannot validate hypothesis with confidence {target_confidence}. "
                "Confidence must be ≥ 0.7 to validate.",
                resource_type="Hypothesis",
                resource_id=hypothesis_id,
                conflict_reason=f"Low confidence ({target_confidence} < 0.7)"
            )

        if new_status == "refuted" and target_confidence > 0.3:
            raise ConflictError(
                f"Cannot refute hypothesis with confidence {target_confidence}. "
                "Confidence must be ≤ 0.3 to refute.",
                resource_type="Hypothesis",
                resource_id=hypothesis_id,
                conflict_reason=f"High confidence ({target_confidence} > 0.3)"
            )

        logger.info(
            f"Updating hypothesis {hypothesis_id} status: {hypothesis.get('status')} → {new_status}",
            extra={
                "hypothesis_id": hypothesis_id,
                "old_status": hypothesis.get("status"),
                "new_status": new_status,
                "confidence": target_confidence,
            }
        )

        # Update hypothesis
        updated_hypothesis = await self.hypothesis_repo.update_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
            status=new_status,
            confidence_score=Decimal(str(target_confidence)) if confidence is not None else None,
        )

        logger.info(
            f"Hypothesis {hypothesis_id} updated successfully",
            extra={"hypothesis_id": hypothesis_id, "new_status": new_status}
        )

        return updated_hypothesis

    async def link_solution_to_hypothesis(
        self,
        solution_id: str,
        hypothesis_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """
        Link solution to validated hypothesis.

        Enforces business rule: Solutions can only link to "validated" hypotheses.
        This ensures solutions are based on confirmed root causes.

        Args:
            solution_id: Solution identifier
            hypothesis_id: Hypothesis identifier to link to
            organization_id: Organization identifier (multi-tenant isolation)

        Returns:
            Updated solution object with hypothesis link

        Raises:
            NotFoundError: If hypothesis or solution doesn't exist
            ConflictError: If hypothesis is not validated
        """
        # Verify hypothesis exists and is validated
        hypothesis = await self.hypothesis_repo.get_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        if not hypothesis:
            raise NotFoundError(
                resource_type="Hypothesis",
                resource_id=hypothesis_id
            )

        if hypothesis.get("status") != "validated":
            raise ConflictError(
                f"Cannot link solution to {hypothesis.get('status')} hypothesis. "
                "Only validated hypotheses can have solutions.",
                resource_type="Hypothesis",
                resource_id=hypothesis_id,
                conflict_reason=f"Hypothesis status is '{hypothesis.get('status')}', not 'validated'"
            )

        # Verify solution exists
        solution = await self.solution_repo.get_solution(
            solution_id=solution_id,
            organization_id=organization_id,
        )

        if not solution:
            raise NotFoundError(
                resource_type="Solution",
                resource_id=solution_id
            )

        logger.info(
            f"Linking solution {solution_id} to hypothesis {hypothesis_id}",
            extra={
                "solution_id": solution_id,
                "hypothesis_id": hypothesis_id,
                "organization_id": organization_id,
            }
        )

        # Link solution to hypothesis
        updated_solution = await self.solution_repo.link_to_hypothesis(
            solution_id=solution_id,
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        logger.info(
            f"Solution {solution_id} linked to hypothesis {hypothesis_id} successfully"
        )

        return updated_solution

    async def get_investigation_progress(
        self,
        case_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """
        Get investigation progress summary for a case.

        Returns counts and percentages for hypotheses and solutions,
        providing visibility into investigation status.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)

        Returns:
            Progress summary with:
            - hypotheses: total, validated, refuted, active, completion_rate
            - solutions: total, implemented, implementation_rate
        """
        logger.info(
            f"Getting investigation progress for case {case_id}",
            extra={"case_id": case_id, "organization_id": organization_id}
        )

        # Get all hypotheses for the case
        hypotheses = await self.hypothesis_repo.list_by_case(
            case_id=case_id,
            organization_id=organization_id,
        )

        # Count hypotheses by status
        total_hypotheses = len(hypotheses)
        validated = sum(1 for h in hypotheses if h.get("status") == "validated")
        refuted = sum(1 for h in hypotheses if h.get("status") == "refuted")
        active = sum(1 for h in hypotheses if h.get("status") == "active")

        # Calculate completion rate (validated + refuted / total)
        completion_rate = round(
            ((validated + refuted) / total_hypotheses * 100), 1
        ) if total_hypotheses > 0 else 0.0

        # Get all solutions for the case
        solutions = await self.solution_repo.list_by_case(
            case_id=case_id,
            organization_id=organization_id,
        )

        # Count implemented solutions
        total_solutions = len(solutions)
        implemented = sum(1 for s in solutions if s.get("applied_at") is not None)

        # Calculate implementation rate
        implementation_rate = round(
            (implemented / total_solutions * 100), 1
        ) if total_solutions > 0 else 0.0

        progress = {
            'hypotheses': {
                'total': total_hypotheses,
                'validated': validated,
                'refuted': refuted,
                'active': active,
                'completion_rate': completion_rate,
            },
            'solutions': {
                'total': total_solutions,
                'implemented': implemented,
                'implementation_rate': implementation_rate,
            }
        }

        logger.info(
            f"Investigation progress for case {case_id}: "
            f"{validated}/{total_hypotheses} validated, {implemented}/{total_solutions} implemented",
            extra={"case_id": case_id, "progress": progress}
        )

        return progress
