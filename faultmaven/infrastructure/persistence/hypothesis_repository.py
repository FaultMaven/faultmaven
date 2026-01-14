"""Hypothesis Repository - Database and In-Memory Implementations (TASK-026).

This module provides hypothesis repository implementations for managing
hypothesis tracking during investigations.

Features:
- Full CRUD operations for hypotheses
- Query by case with optional status filtering
- Multi-tenant isolation via organization_id
- Support for both in-memory and database backends
- Confidence score tracking
- Evidence linking support

Usage:
    from faultmaven.infrastructure.persistence.hypothesis_repository import (
        DatabaseHypothesisRepository,
        InMemoryHypothesisRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseHypothesisRepository(session)
        hypothesis = await repo.create_hypothesis(case_id, org_id, data)

    # In-memory usage (for testing)
    repo = InMemoryHypothesisRepository()
    hypothesis = await repo.create_hypothesis(case_id, org_id, data)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import HypothesisModel
from faultmaven.modules.case.domain.models import Hypothesis, HypothesisStatus

logger = logging.getLogger(__name__)


class HypothesisRepositoryException(Exception):
    """Exception raised when hypothesis repository operations fail."""

    pass


class HypothesisRepository(ABC):
    """Abstract base class for hypothesis repositories."""

    @abstractmethod
    async def create_hypothesis(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Hypothesis:
        """Create new hypothesis record.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (multi-tenant isolation)
            description: Hypothesis description
            created_by: User who created the hypothesis
            status: Hypothesis status (proposed, testing, validated, invalidated, deferred)
            confidence_score: Confidence score (0.00-1.00)
            supporting_evidence_ids: List of evidence IDs supporting this hypothesis
            metadata: Additional metadata

        Returns:
            Created hypothesis with timestamps

        Raises:
            ValueError: If validation fails
            HypothesisRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> Optional[Hypothesis]:
        """Get hypothesis by ID with multi-tenant isolation.

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (for multi-tenant isolation)

        Returns:
            Hypothesis if found and belongs to organization, None otherwise
        """
        pass

    @abstractmethod
    async def list_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Hypothesis], int]:
        """List hypotheses for a case with optional status filter.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (for multi-tenant isolation)
            status: Optional status filter (proposed, testing, validated, invalidated, deferred)
            limit: Maximum results to return
            offset: Results offset for pagination

        Returns:
            Tuple of (hypotheses list, total count)
        """
        pass

    @abstractmethod
    async def update_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        validation_result: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Hypothesis]:
        """Update hypothesis fields.

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (for multi-tenant isolation)
            updated_by: User performing the update
            description: New description (if provided)
            status: New status (if provided)
            confidence_score: New confidence score (if provided)
            supporting_evidence_ids: New supporting evidence IDs (if provided)
            validation_result: Validation result text (if provided)
            metadata: New metadata (if provided)

        Returns:
            Updated hypothesis if found and belongs to organization, None otherwise
        """
        pass

    @abstractmethod
    async def delete_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> bool:
        """Delete hypothesis by ID.

        Args:
            hypothesis_id: Hypothesis identifier
            organization_id: Organization identifier (for multi-tenant isolation)

        Returns:
            True if hypothesis was deleted, False if not found or not authorized
        """
        pass

    @abstractmethod
    async def count_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
    ) -> int:
        """Count hypotheses for a case.

        Args:
            case_id: Case identifier
            organization_id: Organization identifier (for multi-tenant isolation)
            status: Optional status filter

        Returns:
            Count of hypotheses matching criteria
        """
        pass


# =============================================================================
# Database Implementation
# =============================================================================


class DatabaseHypothesisRepository(HypothesisRepository):
    """Database-backed hypothesis repository using SQLAlchemy."""

    def __init__(self, session: AsyncSession):
        """Initialize repository with database session.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def create_hypothesis(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Hypothesis:
        """Create new hypothesis in database."""
        try:
            # Generate hypothesis ID
            hypothesis_id = f"hyp_{uuid4().hex[:12]}"

            # Create ORM model
            hypothesis_model = HypothesisModel(
                hypothesis_id=hypothesis_id,
                case_id=case_id,
                organization_id=organization_id,
                description=description,
                status=status or "proposed",
                confidence_score=confidence_score,
                supporting_evidence_ids=json.dumps(supporting_evidence_ids or []),
                hypothesis_metadata=json.dumps(metadata or {}),
                created_by=created_by,
                proposed_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

            # Add to session and commit
            self.session.add(hypothesis_model)
            await self.session.commit()
            await self.session.refresh(hypothesis_model)

            # Convert to domain model
            return self._to_domain_model(hypothesis_model)

        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create hypothesis: {e}")
            raise ValueError(
                f"Case {case_id} not found or integrity constraint violated"
            )
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Unexpected error creating hypothesis: {e}")
            raise HypothesisRepositoryException(f"Failed to create hypothesis: {e}")

    async def get_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> Optional[Hypothesis]:
        """Get hypothesis by ID with multi-tenant isolation."""
        try:
            stmt = select(HypothesisModel).where(
                and_(
                    HypothesisModel.hypothesis_id == hypothesis_id,
                    HypothesisModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            hypothesis_model = result.scalar_one_or_none()

            if hypothesis_model:
                return self._to_domain_model(hypothesis_model)
            return None

        except Exception as e:
            logger.error(f"Error retrieving hypothesis {hypothesis_id}: {e}")
            raise HypothesisRepositoryException(f"Failed to get hypothesis: {e}")

    async def list_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Hypothesis], int]:
        """List hypotheses for a case with optional status filter."""
        try:
            # Build query conditions
            conditions = [
                HypothesisModel.case_id == case_id,
                HypothesisModel.organization_id == organization_id,
            ]

            if status:
                conditions.append(HypothesisModel.status == status)

            # Count total
            count_stmt = (
                select(func.count())
                .select_from(HypothesisModel)
                .where(and_(*conditions))
            )
            count_result = await self.session.execute(count_stmt)
            total_count = count_result.scalar()

            # Get paginated results (ordered by confidence desc, then proposed_at desc)
            stmt = (
                select(HypothesisModel)
                .where(and_(*conditions))
                .order_by(
                    HypothesisModel.confidence_score.desc().nullslast(),
                    HypothesisModel.proposed_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )

            result = await self.session.execute(stmt)
            hypothesis_models = result.scalars().all()

            hypotheses = [self._to_domain_model(h) for h in hypothesis_models]

            return (hypotheses, total_count)

        except Exception as e:
            logger.error(f"Error listing hypotheses for case {case_id}: {e}")
            raise HypothesisRepositoryException(f"Failed to list hypotheses: {e}")

    async def update_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        validation_result: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Hypothesis]:
        """Update hypothesis fields."""
        try:
            # Get existing hypothesis
            stmt = select(HypothesisModel).where(
                and_(
                    HypothesisModel.hypothesis_id == hypothesis_id,
                    HypothesisModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            hypothesis_model = result.scalar_one_or_none()

            if not hypothesis_model:
                return None

            # Update fields
            if description is not None:
                hypothesis_model.description = description

            if status is not None:
                hypothesis_model.status = status

            if confidence_score is not None:
                hypothesis_model.confidence_score = confidence_score

            if supporting_evidence_ids is not None:
                hypothesis_model.supporting_evidence_ids = json.dumps(
                    supporting_evidence_ids
                )

            if validation_result is not None:
                hypothesis_model.validation_result = validation_result
                hypothesis_model.validation_timestamp = datetime.now(timezone.utc)

            if metadata is not None:
                hypothesis_model.hypothesis_metadata = json.dumps(metadata)

            hypothesis_model.updated_by = updated_by
            hypothesis_model.updated_at = datetime.now(timezone.utc)

            await self.session.commit()
            await self.session.refresh(hypothesis_model)

            return self._to_domain_model(hypothesis_model)

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error updating hypothesis {hypothesis_id}: {e}")
            raise HypothesisRepositoryException(f"Failed to update hypothesis: {e}")

    async def delete_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> bool:
        """Delete hypothesis by ID."""
        try:
            stmt = delete(HypothesisModel).where(
                and_(
                    HypothesisModel.hypothesis_id == hypothesis_id,
                    HypothesisModel.organization_id == organization_id,
                )
            )

            result = await self.session.execute(stmt)
            await self.session.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error deleting hypothesis {hypothesis_id}: {e}")
            raise HypothesisRepositoryException(f"Failed to delete hypothesis: {e}")

    async def count_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
    ) -> int:
        """Count hypotheses for a case."""
        try:
            conditions = [
                HypothesisModel.case_id == case_id,
                HypothesisModel.organization_id == organization_id,
            ]

            if status:
                conditions.append(HypothesisModel.status == status)

            stmt = (
                select(func.count())
                .select_from(HypothesisModel)
                .where(and_(*conditions))
            )

            result = await self.session.execute(stmt)
            return result.scalar()

        except Exception as e:
            logger.error(f"Error counting hypotheses for case {case_id}: {e}")
            raise HypothesisRepositoryException(f"Failed to count hypotheses: {e}")

    def _to_domain_model(self, hypothesis_model: HypothesisModel) -> Hypothesis:
        """Convert ORM model to domain model.

        Note: This is a simplified conversion. The full domain model in case.py
        has many more fields. For TASK-026, we focus on the core fields needed
        for hypothesis tracking via the API.
        """
        # Parse JSON fields
        supporting_evidence_ids = (
            json.loads(hypothesis_model.supporting_evidence_ids)
            if hypothesis_model.supporting_evidence_ids
            else []
        )

        metadata_dict = (
            json.loads(hypothesis_model.hypothesis_metadata)
            if hypothesis_model.hypothesis_metadata
            else {}
        )

        # Return simplified Hypothesis (Pydantic model compatible subset)
        # Note: The full Hypothesis model in case.py requires more fields
        # For API purposes, we'll create a compatible dict structure
        return {
            "hypothesis_id": hypothesis_model.hypothesis_id,
            "case_id": hypothesis_model.case_id,
            "description": hypothesis_model.description,
            "status": hypothesis_model.status,
            "confidence_score": (
                float(hypothesis_model.confidence_score)
                if hypothesis_model.confidence_score
                else None
            ),
            "supporting_evidence_ids": supporting_evidence_ids,
            "validation_result": hypothesis_model.validation_result,
            "validation_timestamp": hypothesis_model.validation_timestamp,
            "proposed_at": hypothesis_model.proposed_at,
            "updated_at": hypothesis_model.updated_at,
            "organization_id": hypothesis_model.organization_id,
            "created_by": hypothesis_model.created_by,
            "updated_by": hypothesis_model.updated_by,
            "metadata": metadata_dict,
        }


# =============================================================================
# In-Memory Implementation (for testing)
# =============================================================================


class InMemoryHypothesisRepository(HypothesisRepository):
    """In-memory hypothesis repository for testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._hypotheses: Dict[str, Dict] = {}

    async def create_hypothesis(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Hypothesis:
        """Create new hypothesis in memory."""
        hypothesis_id = f"hyp_{uuid4().hex[:12]}"

        hypothesis = {
            "hypothesis_id": hypothesis_id,
            "case_id": case_id,
            "organization_id": organization_id,
            "description": description,
            "status": status or "proposed",
            "confidence_score": float(confidence_score) if confidence_score else None,
            "supporting_evidence_ids": supporting_evidence_ids or [],
            "validation_result": None,
            "validation_timestamp": None,
            "proposed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "created_by": created_by,
            "updated_by": None,
            "metadata": metadata or {},
        }

        self._hypotheses[hypothesis_id] = deepcopy(hypothesis)
        return hypothesis

    async def get_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> Optional[Hypothesis]:
        """Get hypothesis by ID with multi-tenant isolation."""
        hypothesis = self._hypotheses.get(hypothesis_id)

        if hypothesis and hypothesis["organization_id"] == organization_id:
            return deepcopy(hypothesis)

        return None

    async def list_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Hypothesis], int]:
        """List hypotheses for a case."""
        # Filter by case and organization
        filtered = [
            h
            for h in self._hypotheses.values()
            if h["case_id"] == case_id and h["organization_id"] == organization_id
        ]

        # Filter by status if provided
        if status:
            filtered = [h for h in filtered if h["status"] == status]

        # Sort by confidence (desc), then proposed_at (desc)
        filtered.sort(
            key=lambda h: (
                -(h["confidence_score"] if h["confidence_score"] is not None else -1),
                -h["proposed_at"].timestamp(),
            )
        )

        # Paginate
        total_count = len(filtered)
        paginated = filtered[offset : offset + limit]

        return ([deepcopy(h) for h in paginated], total_count)

    async def update_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        confidence_score: Optional[Decimal] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        validation_result: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Hypothesis]:
        """Update hypothesis in memory."""
        hypothesis = self._hypotheses.get(hypothesis_id)

        if not hypothesis or hypothesis["organization_id"] != organization_id:
            return None

        # Update fields
        if description is not None:
            hypothesis["description"] = description

        if status is not None:
            hypothesis["status"] = status

        if confidence_score is not None:
            hypothesis["confidence_score"] = float(confidence_score)

        if supporting_evidence_ids is not None:
            hypothesis["supporting_evidence_ids"] = supporting_evidence_ids

        if validation_result is not None:
            hypothesis["validation_result"] = validation_result
            hypothesis["validation_timestamp"] = datetime.now(timezone.utc)

        if metadata is not None:
            hypothesis["metadata"] = metadata

        hypothesis["updated_by"] = updated_by
        hypothesis["updated_at"] = datetime.now(timezone.utc)

        return deepcopy(hypothesis)

    async def delete_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> bool:
        """Delete hypothesis from memory."""
        hypothesis = self._hypotheses.get(hypothesis_id)

        if hypothesis and hypothesis["organization_id"] == organization_id:
            del self._hypotheses[hypothesis_id]
            return True

        return False

    async def count_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
    ) -> int:
        """Count hypotheses for a case."""
        filtered = [
            h
            for h in self._hypotheses.values()
            if h["case_id"] == case_id and h["organization_id"] == organization_id
        ]

        if status:
            filtered = [h for h in filtered if h["status"] == status]

        return len(filtered)
