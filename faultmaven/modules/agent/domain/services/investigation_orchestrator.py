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

    async def get_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """
        Get hypothesis by ID with organization isolation.

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (multi-tenant isolation)

        Returns:
            Hypothesis object

        Raises:
            NotFoundError: If hypothesis doesn't exist or organization mismatch
        """
        hypothesis = await self.hypothesis_repo.get_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        if not hypothesis:
            raise NotFoundError(
                resource_type="Hypothesis",
                resource_id=hypothesis_id
            )

        return hypothesis

    async def list_hypotheses_for_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        List hypotheses for a case with optional filtering.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)
            status: Optional status filter
            limit: Maximum number of results (default 100)
            offset: Result offset for pagination (default 0)

        Returns:
            List of hypothesis objects
        """
        logger.info(
            f"Listing hypotheses for case {case_id}",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            }
        )

        hypotheses = await self.hypothesis_repo.list_hypotheses_by_case(
            case_id=case_id,
            organization_id=organization_id,
            status=status,
            limit=limit,
            offset=offset,
        )

        logger.info(
            f"Found {len(hypotheses)} hypotheses for case {case_id}",
            extra={"case_id": case_id, "count": len(hypotheses)}
        )

        return hypotheses

    async def update_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
        updated_by: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        confidence: Optional[float] = None,
        status: Optional[str] = None,
        evidence_supporting: Optional[List[str]] = None,
        evidence_against: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Update hypothesis fields with validation.

        Enforces business rules for status transitions:
        - Can only transition to "validated" if confidence ≥ 0.7
        - Can only transition to "refuted" if confidence ≤ 0.3

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (multi-tenant isolation)
            updated_by: User ID performing the update
            title: Optional title (stored in metadata)
            description: Optional updated description
            confidence: Optional new confidence score (0.0-1.0)
            status: Optional new status
            evidence_supporting: Optional list of supporting evidence IDs
            evidence_against: Optional list of refuting evidence IDs

        Returns:
            Updated hypothesis object

        Raises:
            NotFoundError: If hypothesis doesn't exist
            ValidationException: If validation fails
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

        # Validate description length if provided
        if description is not None and len(description) < 10:
            raise ValidationException(
                "Hypothesis description must be at least 10 characters",
                details={"description_length": len(description), "min_length": 10}
            )

        if description is not None and len(description) > 5000:
            raise ValidationException(
                "Hypothesis description cannot exceed 5000 characters",
                details={"description_length": len(description), "max_length": 5000}
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

        # Enforce business rules for status transitions if status is being changed
        if status is not None:
            if status == "validated" and target_confidence < 0.7:
                raise ConflictError(
                    f"Cannot validate hypothesis with confidence {target_confidence}. "
                    "Confidence must be ≥ 0.7 to validate.",
                    resource_type="Hypothesis",
                    resource_id=hypothesis_id,
                    conflict_reason=f"Low confidence ({target_confidence} < 0.7)"
                )

            if status == "refuted" and target_confidence > 0.3:
                raise ConflictError(
                    f"Cannot refute hypothesis with confidence {target_confidence}. "
                    "Confidence must be ≤ 0.3 to refute.",
                    resource_type="Hypothesis",
                    resource_id=hypothesis_id,
                    conflict_reason=f"High confidence ({target_confidence} > 0.3)"
                )

        logger.info(
            f"Updating hypothesis {hypothesis_id}",
            extra={
                "hypothesis_id": hypothesis_id,
                "organization_id": organization_id,
                "updated_by": updated_by,
                "has_status_change": status is not None,
                "has_confidence_change": confidence is not None,
            }
        )

        # Prepare metadata update
        metadata = hypothesis.get("metadata", {})
        if title is not None:
            metadata["title"] = title

        # Build update dict with only provided fields
        update_fields = {}
        if description is not None:
            update_fields["description"] = description
        if confidence is not None:
            update_fields["confidence_score"] = Decimal(str(confidence))
        if status is not None:
            update_fields["status"] = status
        if evidence_supporting is not None:
            update_fields["supporting_evidence_ids"] = evidence_supporting
        if evidence_against is not None:
            # Store in metadata for now (schema doesn't have evidence_against field)
            metadata["evidence_against"] = evidence_against
        if metadata != hypothesis.get("metadata", {}):
            update_fields["metadata"] = metadata

        # Update hypothesis
        updated_hypothesis = await self.hypothesis_repo.update_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
            **update_fields
        )

        logger.info(
            f"Hypothesis {hypothesis_id} updated successfully",
            extra={"hypothesis_id": hypothesis_id}
        )

        return updated_hypothesis

    async def delete_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> None:
        """
        Delete a hypothesis with organization isolation.

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (multi-tenant isolation)

        Raises:
            NotFoundError: If hypothesis doesn't exist
        """
        # Verify hypothesis exists
        hypothesis = await self.hypothesis_repo.get_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        if not hypothesis:
            raise NotFoundError(
                resource_type="Hypothesis",
                resource_id=hypothesis_id
            )

        logger.info(
            f"Deleting hypothesis {hypothesis_id}",
            extra={"hypothesis_id": hypothesis_id, "organization_id": organization_id}
        )

        await self.hypothesis_repo.delete_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        logger.info(
            f"Hypothesis {hypothesis_id} deleted successfully",
            extra={"hypothesis_id": hypothesis_id}
        )

    async def create_solution(
        self,
        case_id: str,
        organization_id: str,
        title: str,
        description: str,
        created_by: str,
        hypothesis_id: Optional[str] = None,
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Create new solution with optional hypothesis linking.

        If hypothesis_id provided, validates that hypothesis is in "validated" status
        (business rule: solutions can only link to validated hypotheses).

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)
            title: Solution title
            description: Solution description (20-5000 characters)
            created_by: User ID who created the solution
            hypothesis_id: Optional hypothesis to link to (must be validated)
            implementation_steps: Optional list of implementation steps
            risk_level: Optional risk level (low, medium, high, critical)
            estimated_effort: Optional effort estimate
            metadata: Additional metadata

        Returns:
            Created solution object

        Raises:
            ValidationException: If validation fails
            NotFoundError: If hypothesis doesn't exist
            ConflictError: If hypothesis is not validated
        """
        # Validate description length
        if len(description) < 20:
            raise ValidationException(
                "Solution description must be at least 20 characters",
                details={"description_length": len(description), "min_length": 20}
            )

        if len(description) > 5000:
            raise ValidationException(
                "Solution description cannot exceed 5000 characters",
                details={"description_length": len(description), "max_length": 5000}
            )

        # Validate risk level if provided
        if risk_level and risk_level not in ["low", "medium", "high", "critical"]:
            raise ValidationException(
                "Risk level must be: low, medium, high, or critical",
                details={"risk_level": risk_level, "valid_values": ["low", "medium", "high", "critical"]}
            )

        # If hypothesis_id provided, verify it exists and is validated
        if hypothesis_id:
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

        logger.info(
            f"Creating solution for case {case_id}",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "created_by": created_by,
                "hypothesis_id": hypothesis_id,
            }
        )

        # Prepare metadata
        solution_metadata = metadata or {}
        solution_metadata["title"] = title
        if implementation_steps:
            solution_metadata["implementation_steps"] = implementation_steps
        if risk_level:
            solution_metadata["risk_level"] = risk_level
        if estimated_effort:
            solution_metadata["estimated_effort"] = estimated_effort

        # Create solution via repository
        solution = await self.solution_repo.create_solution(
            case_id=case_id,
            organization_id=organization_id,
            description=description,
            created_by=created_by,
            hypothesis_id=hypothesis_id,
            metadata=solution_metadata,
        )

        logger.info(
            f"Solution created successfully: {solution['solution_id']}",
            extra={"solution_id": solution["solution_id"]}
        )

        return solution

    async def get_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """
        Get solution by ID with organization isolation.

        Args:
            solution_id: Solution identifier
            organization_id: Organization identifier (multi-tenant isolation)

        Returns:
            Solution object

        Raises:
            NotFoundError: If solution doesn't exist or organization mismatch
        """
        solution = await self.solution_repo.get_solution(
            solution_id=solution_id,
            organization_id=organization_id,
        )

        if not solution:
            raise NotFoundError(
                resource_type="Solution",
                resource_id=solution_id
            )

        return solution

    async def list_solutions_for_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        List solutions for a case with optional filtering.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)
            status: Optional status filter
            limit: Maximum number of results (default 100)
            offset: Result offset for pagination (default 0)

        Returns:
            List of solution objects
        """
        logger.info(
            f"Listing solutions for case {case_id}",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            }
        )

        solutions = await self.solution_repo.list_solutions_by_case(
            case_id=case_id,
            organization_id=organization_id,
            status=status,
            limit=limit,
            offset=offset,
        )

        logger.info(
            f"Found {len(solutions)} solutions for case {case_id}",
            extra={"case_id": case_id, "count": len(solutions)}
        )

        return solutions

    async def delete_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> None:
        """
        Delete a solution with organization isolation.

        Args:
            solution_id: Solution identifier
            organization_id: Organization identifier (multi-tenant isolation)

        Raises:
            NotFoundError: If solution doesn't exist
        """
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
            f"Deleting solution {solution_id}",
            extra={"solution_id": solution_id, "organization_id": organization_id}
        )

        await self.solution_repo.delete_solution(
            solution_id=solution_id,
            organization_id=organization_id,
        )

        logger.info(
            f"Solution {solution_id} deleted successfully",
            extra={"solution_id": solution_id}
        )
