"""Solution Repository - Database and In-Memory Implementations (TASK-026).

This module provides solution repository implementations for managing
solution tracking during investigations.

Features:
- Full CRUD operations for solutions
- Query by case with optional implementation status filtering
- Multi-tenant isolation via organization_id
- Support for both in-memory and database backends
- Implementation tracking
- Verification status management

Usage:
    from faultmaven.infrastructure.persistence.solution_repository import (
        DatabaseSolutionRepository,
        InMemorySolutionRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseSolutionRepository(session)
        solution = await repo.create_solution(case_id, org_id, data)

    # In-memory usage (for testing)
    repo = InMemorySolutionRepository()
    solution = await repo.create_solution(case_id, org_id, data)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import SolutionModel
from faultmaven.modules.case.domain.models import Solution

logger = logging.getLogger(__name__)


class SolutionRepositoryException(Exception):
    """Exception raised when solution repository operations fail."""

    pass


class SolutionRepository(ABC):
    """Abstract base class for solution repositories."""

    @abstractmethod
    async def create_solution(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Solution:
        """Create new solution record.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)
            description: Solution description
            created_by: User who created the solution
            status: Solution status (proposed, in_progress, implemented, verified, rejected)
            implementation_steps: List of implementation steps
            risk_level: Risk level (low, medium, high, critical)
            estimated_effort: Estimated effort description
            metadata: Additional metadata

        Returns:
            Created solution with timestamps

        Raises:
            ValueError: If validation fails
            SolutionRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> Optional[Solution]:
        """Get solution by ID with multi-tenant isolation.

        Args:
            solution_id: Solution identifier
            organization_id: Organization identifier (for multi-tenant isolation)

        Returns:
            Solution if found and belongs to organization, None otherwise
        """
        pass

    @abstractmethod
    async def list_solutions_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Solution], int]:
        """List solutions for a case with optional status filter.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (for multi-tenant isolation)
            status: Optional status filter
            limit: Maximum results to return
            offset: Results offset for pagination

        Returns:
            Tuple of (solutions list, total count)
        """
        pass

    @abstractmethod
    async def update_solution(
        self,
        solution_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        verification_result: Optional[str] = None,
        implemented: Optional[bool] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Solution]:
        """Update solution fields.

        Args:
            solution_id: Solution identifier
            organization_id: Organization identifier (for multi-tenant isolation)
            updated_by: User performing the update
            description: New description (if provided)
            status: New status (if provided)
            implementation_steps: New implementation steps (if provided)
            risk_level: New risk level (if provided)
            estimated_effort: New estimated effort (if provided)
            verification_result: Verification result text (if provided)
            implemented: Whether solution has been implemented (if provided)
            metadata: New metadata (if provided)

        Returns:
            Updated solution if found and belongs to organization, None otherwise
        """
        pass

    @abstractmethod
    async def delete_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> bool:
        """Delete solution by ID.

        Args:
            solution_id: Solution identifier
            organization_id: Organization identifier (for multi-tenant isolation)

        Returns:
            True if solution was deleted, False if not found or not authorized
        """
        pass


# =============================================================================
# Database Implementation
# =============================================================================


class DatabaseSolutionRepository(SolutionRepository):
    """Database-backed solution repository using SQLAlchemy."""

    def __init__(self, session: AsyncSession):
        """Initialize repository with database session.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def create_solution(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Solution:
        """Create new solution in database."""
        try:
            # Generate solution ID
            solution_id = f"sol_{uuid4().hex[:12]}"

            # Create ORM model
            solution_model = SolutionModel(
                solution_id=solution_id,
                case_id=case_id,
                organization_id=organization_id,
                description=description,
                status=status or "proposed",
                implementation_steps=json.dumps(implementation_steps or []),
                risk_level=risk_level,
                estimated_effort=estimated_effort,
                solution_metadata=json.dumps(metadata or {}),
                created_by=created_by,
                proposed_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            # Add to session and commit
            self.session.add(solution_model)
            await self.session.commit()
            await self.session.refresh(solution_model)

            # Convert to domain model
            return self._to_domain_model(solution_model)

        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create solution: {e}")
            raise ValueError(
                f"Case {case_id} not found or integrity constraint violated"
            )
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Unexpected error creating solution: {e}")
            raise SolutionRepositoryException(f"Failed to create solution: {e}")

    async def get_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> Optional[Solution]:
        """Get solution by ID with multi-tenant isolation."""
        try:
            stmt = select(SolutionModel).where(
                and_(
                    SolutionModel.solution_id == solution_id,
                    SolutionModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            solution_model = result.scalar_one_or_none()

            if solution_model:
                return self._to_domain_model(solution_model)
            return None

        except Exception as e:
            logger.error(f"Error retrieving solution {solution_id}: {e}")
            raise SolutionRepositoryException(f"Failed to get solution: {e}")

    async def list_solutions_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Solution], int]:
        """List solutions for a case with optional status filter."""
        try:
            # Build query conditions
            conditions = [
                SolutionModel.case_id == case_id,
                SolutionModel.organization_id == organization_id,
            ]

            if status:
                conditions.append(SolutionModel.status == status)

            # Count total
            count_stmt = (
                select(func.count()).select_from(SolutionModel).where(and_(*conditions))
            )
            count_result = await self.session.execute(count_stmt)
            total_count = count_result.scalar()

            # Get paginated results (ordered by proposed_at desc)
            stmt = (
                select(SolutionModel)
                .where(and_(*conditions))
                .order_by(SolutionModel.proposed_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.session.execute(stmt)
            solution_models = result.scalars().all()

            solutions = [self._to_domain_model(s) for s in solution_models]

            return (solutions, total_count)

        except Exception as e:
            logger.error(f"Error listing solutions for case {case_id}: {e}")
            raise SolutionRepositoryException(f"Failed to list solutions: {e}")

    async def update_solution(
        self,
        solution_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        verification_result: Optional[str] = None,
        implemented: Optional[bool] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Solution]:
        """Update solution fields."""
        try:
            # Get existing solution
            stmt = select(SolutionModel).where(
                and_(
                    SolutionModel.solution_id == solution_id,
                    SolutionModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            solution_model = result.scalar_one_or_none()

            if not solution_model:
                return None

            # Update fields
            if description is not None:
                solution_model.description = description

            if status is not None:
                solution_model.status = status

            if implementation_steps is not None:
                solution_model.implementation_steps = json.dumps(implementation_steps)

            if risk_level is not None:
                solution_model.risk_level = risk_level

            if estimated_effort is not None:
                solution_model.estimated_effort = estimated_effort

            if verification_result is not None:
                solution_model.verification_result = verification_result
                solution_model.verification_timestamp = datetime.now(timezone.utc)

            if implemented is not None and implemented:
                solution_model.implemented_at = datetime.now(timezone.utc)

            if metadata is not None:
                solution_model.solution_metadata = json.dumps(metadata)

            solution_model.updated_by = updated_by
            solution_model.updated_at = datetime.now(timezone.utc)

            await self.session.commit()
            await self.session.refresh(solution_model)

            return self._to_domain_model(solution_model)

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error updating solution {solution_id}: {e}")
            raise SolutionRepositoryException(f"Failed to update solution: {e}")

    async def delete_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> bool:
        """Delete solution by ID."""
        try:
            stmt = delete(SolutionModel).where(
                and_(
                    SolutionModel.solution_id == solution_id,
                    SolutionModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            await self.session.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error deleting solution {solution_id}: {e}")
            raise SolutionRepositoryException(f"Failed to delete solution: {e}")

    def _to_domain_model(self, solution_model: SolutionModel) -> Solution:
        """Convert ORM model to domain model."""
        # Parse JSON fields
        implementation_steps = (
            json.loads(solution_model.implementation_steps)
            if solution_model.implementation_steps
            else []
        )

        metadata_dict = (
            json.loads(solution_model.solution_metadata)
            if solution_model.solution_metadata
            else {}
        )

        # Return simplified Solution (dict structure for API compatibility)
        return {
            "solution_id": solution_model.solution_id,
            "case_id": solution_model.case_id,
            "description": solution_model.description,
            "status": solution_model.status,
            "implementation_steps": implementation_steps,
            "risk_level": solution_model.risk_level,
            "estimated_effort": solution_model.estimated_effort,
            "verification_result": solution_model.verification_result,
            "verification_timestamp": solution_model.verification_timestamp,
            "proposed_at": solution_model.proposed_at,
            "implemented_at": solution_model.implemented_at,
            "updated_at": solution_model.updated_at,
            "organization_id": solution_model.organization_id,
            "created_by": solution_model.created_by,
            "updated_by": solution_model.updated_by,
            "metadata": metadata_dict,
        }


# =============================================================================
# In-Memory Implementation (for testing)
# =============================================================================


class InMemorySolutionRepository(SolutionRepository):
    """In-memory solution repository for testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._solutions: Dict[str, Dict] = {}

    async def create_solution(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Solution:
        """Create new solution in memory."""
        solution_id = f"sol_{uuid4().hex[:12]}"

        solution = {
            "solution_id": solution_id,
            "case_id": case_id,
            "organization_id": organization_id,
            "description": description,
            "status": status or "proposed",
            "implementation_steps": implementation_steps or [],
            "risk_level": risk_level,
            "estimated_effort": estimated_effort,
            "verification_result": None,
            "verification_timestamp": None,
            "proposed_at": datetime.now(timezone.utc),
            "implemented_at": None,
            "updated_at": datetime.now(timezone.utc),
            "created_by": created_by,
            "updated_by": None,
            "metadata": metadata or {},
        }

        self._solutions[solution_id] = deepcopy(solution)
        return solution

    async def get_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> Optional[Solution]:
        """Get solution by ID with multi-tenant isolation."""
        solution = self._solutions.get(solution_id)

        if solution and solution["organization_id"] == organization_id:
            return deepcopy(solution)

        return None

    async def list_solutions_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Solution], int]:
        """List solutions for a case."""
        # Filter by case and organization
        filtered = [
            s
            for s in self._solutions.values()
            if s["case_id"] == case_id and s["organization_id"] == organization_id
        ]

        # Filter by status if provided
        if status:
            filtered = [s for s in filtered if s["status"] == status]

        # Sort by proposed_at (desc)
        filtered.sort(key=lambda s: -s["proposed_at"].timestamp())

        # Paginate
        total_count = len(filtered)
        paginated = filtered[offset : offset + limit]

        return ([deepcopy(s) for s in paginated], total_count)

    async def update_solution(
        self,
        solution_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        verification_result: Optional[str] = None,
        implemented: Optional[bool] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Solution]:
        """Update solution in memory."""
        solution = self._solutions.get(solution_id)

        if not solution or solution["organization_id"] != organization_id:
            return None

        # Update fields
        if description is not None:
            solution["description"] = description

        if status is not None:
            solution["status"] = status

        if implementation_steps is not None:
            solution["implementation_steps"] = implementation_steps

        if risk_level is not None:
            solution["risk_level"] = risk_level

        if estimated_effort is not None:
            solution["estimated_effort"] = estimated_effort

        if verification_result is not None:
            solution["verification_result"] = verification_result
            solution["verification_timestamp"] = datetime.now(timezone.utc)

        if implemented is not None and implemented:
            solution["implemented_at"] = datetime.now(timezone.utc)

        if metadata is not None:
            solution["metadata"] = metadata

        solution["updated_by"] = updated_by
        solution["updated_at"] = datetime.now(timezone.utc)

        return deepcopy(solution)

    async def delete_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> bool:
        """Delete solution from memory."""
        solution = self._solutions.get(solution_id)

        if solution and solution["organization_id"] == organization_id:
            del self._solutions[solution_id]
            return True

        return False
