"""
Report Recommendation Service - Intelligent Report Generation Recommendations

Determines which reports to offer for generation with intelligent runbook
similarity checking to prevent duplicate runbook generation.

Architecture Reference: docs/architecture/document-generation-and-closure-design.md
Section 5.4: Intelligent Report Recommendation
"""

from typing import List, Dict, Any, Optional
import logging

from faultmaven.services.base import BaseService
from faultmaven.models.report import (
    ReportType,
    ReportRecommendation,
    RunbookRecommendation,
    SimilarRunbook,
    CaseReport
)
from faultmaven.models.case import Case
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.observability.tracing import trace


logger = logging.getLogger(__name__)


class ReportRecommendationService(BaseService):
    """
    Determines which reports to offer for generation.

    Key Features:
    - Always offers: Incident Report, Post-Mortem (unique per incident)
    - Conditionally offers: Runbook (based on similarity search)
    - Prevents duplicate runbook generation through intelligent recommendations
    """

    # Similarity thresholds for recommendation logic
    HIGH_SIMILARITY_THRESHOLD = 0.85  # ≥85%: Recommend reuse
    MODERATE_SIMILARITY_THRESHOLD = 0.70  # 70-84%: Offer both options

    def __init__(
        self,
        runbook_kb: RunbookKnowledgeBase,
        embedding_service: Optional[Any] = None  # For future explicit embedding generation
    ):
        """
        Initialize report recommendation service.

        Args:
            runbook_kb: RunbookKnowledgeBase for similarity search
            embedding_service: Optional service for generating embeddings
        """
        super().__init__("report_recommendation_service")
        self.runbook_kb = runbook_kb
        self.embedding_service = embedding_service

    @trace("get_available_report_types")
    async def get_available_report_types(
        self,
        case: Case,
    ) -> ReportRecommendation:
        """
        Determine which report types to offer for case.

        Logic:
        - Incident Report: ALWAYS available (unique to this incident)
        - Post-Mortem: ALWAYS available (unique to this incident)
        - Runbook: CONDITIONAL (check for existing similar runbooks)

        Args:
            case: Case object with investigation context

        Returns:
            ReportRecommendation with available types and runbook suggestion
        """
        logger.info(
            f"Getting report recommendations for case",
            extra={"case_id": case.case_id}
        )

        # Always available: incident-specific reports
        available_types = [
            ReportType.INCIDENT_REPORT,
            ReportType.POST_MORTEM,
        ]

        # Check for existing similar runbooks
        existing_runbooks = await self._find_similar_runbooks(case)

        # Generate runbook recommendation based on similarity
        runbook_rec = self._generate_runbook_recommendation(existing_runbooks)

        # If recommendation is to generate (low/no similarity), add runbook to available types
        if runbook_rec.action in ["generate", "review_or_generate"]:
            available_types.append(ReportType.RUNBOOK)

        recommendation = ReportRecommendation(
            case_id=case.case_id,
            available_for_generation=available_types,
            runbook_recommendation=runbook_rec
        )

        logger.info(
            f"Report recommendation generated",
            extra={
                "case_id": case.case_id,
                "runbook_action": runbook_rec.action,
                "available_types": [t.value for t in available_types]
            }
        )

        return recommendation

    async def _find_similar_runbooks(
        self,
        case: Case,
    ) -> List[SimilarRunbook]:
        """
        Find existing runbooks similar to current case.

        Uses semantic similarity search on:
        - Problem description
        - Root cause (if available)
        - Resolution steps (if available)
        - Domain/technology tags

        Args:
            case: Case object

        Returns:
            List of similar runbooks sorted by similarity score (descending)
        """
        try:
            # Create embedding for case features
            query_embedding = await self._create_case_embedding(case)

            # Build filters for similarity search
            filters = {}
            if hasattr(case, 'domain') and case.domain:
                filters["domain"] = case.domain

            # Search knowledge base for similar runbooks
            similar_runbooks = await self.runbook_kb.search_runbooks(
                query_embedding=query_embedding,
                filters=filters,
                top_k=5,  # Get top 5 matches
                min_similarity=0.65,  # Minimum 65% similarity threshold
            )

            if similar_runbooks:
                logger.info(
                    f"Found {len(similar_runbooks)} similar runbooks",
                    extra={
                        "case_id": case.case_id,
                        "top_similarity": similar_runbooks[0].similarity_score
                    }
                )
            else:
                logger.debug(
                    f"No similar runbooks found",
                    extra={"case_id": case.case_id}
                )

            return similar_runbooks

        except Exception as e:
            logger.error(
                f"Error finding similar runbooks: {e}",
                extra={"case_id": case.case_id}
            )
            # Return empty list on error - fail gracefully
            return []

    async def _create_case_embedding(self, case: Case) -> List[float]:
        """
        Create semantic embedding for case.

        Combines:
        - Problem description
        - Root cause (if identified)
        - Resolution actions (if available)
        - Technology/domain keywords

        Args:
            case: Case object

        Returns:
            Embedding vector (list of floats)

        Note: Currently uses ChromaDB's default embedding.
        In production, should use explicit BGE-M3 model for consistency.
        """
        # Build searchable text from case attributes
        searchable_parts = []

        # Add title and description
        if case.title:
            searchable_parts.append(f"Problem: {case.title}")
        if case.description:
            searchable_parts.append(case.description)

        # Add domain if available
        if hasattr(case, 'domain') and case.domain:
            searchable_parts.append(f"Domain: {case.domain}")

        # Add tags if available
        if hasattr(case, 'tags') and case.tags:
            searchable_parts.append(f"Tags: {', '.join(case.tags)}")

        # For now, return dummy embedding
        # TODO: Integrate with actual embedding model (BGE-M3)
        # This will be handled by ChromaDB's built-in embedding for now
        searchable_text = " ".join(searchable_parts)

        # Placeholder: In production, generate actual embedding here
        # For now, we'll rely on ChromaDB to handle embedding generation
        # when we call search_runbooks with the searchable text

        logger.debug(
            f"Created case embedding",
            extra={"case_id": case.case_id, "text_length": len(searchable_text)}
        )

        # Return empty list for now - will be handled by ChromaDB
        # In production implementation, call embedding_service.encode(searchable_text)
        return []

    def _generate_runbook_recommendation(
        self,
        similar_runbooks: List[SimilarRunbook]
    ) -> RunbookRecommendation:
        """
        Generate runbook recommendation based on similarity analysis.

        Thresholds:
        - ≥85% similarity: Recommend reuse existing
        - 70-84% similarity: Offer both review OR generate
        - <70% similarity: Recommend generation

        Args:
            similar_runbooks: List of similar runbooks from search

        Returns:
            RunbookRecommendation with action and reasoning
        """
        if not similar_runbooks:
            # No existing runbooks found
            return RunbookRecommendation(
                action="generate",
                existing_runbook=None,
                similarity_score=None,
                reason="No similar runbooks found. Generate new runbook."
            )

        # Get best match (highest similarity)
        best_match = similar_runbooks[0]
        similarity = best_match.similarity_score

        if similarity >= self.HIGH_SIMILARITY_THRESHOLD:
            # Very similar runbook exists (85%+ match)
            return RunbookRecommendation(
                action="reuse",
                existing_runbook=best_match.runbook,
                similarity_score=similarity,
                reason=(
                    f"Found existing runbook with {similarity:.0%} similarity. "
                    "Recommend using existing runbook instead of generating new one."
                )
            )

        elif similarity >= self.MODERATE_SIMILARITY_THRESHOLD:
            # Moderately similar runbook exists (70-84% match)
            return RunbookRecommendation(
                action="review_or_generate",
                existing_runbook=best_match.runbook,
                similarity_score=similarity,
                reason=(
                    f"Found similar runbook ({similarity:.0%} match). "
                    "Review existing runbook or generate new one if significantly different."
                )
            )

        else:
            # Low similarity (<70%), offer generation
            return RunbookRecommendation(
                action="generate",
                existing_runbook=None,
                similarity_score=similarity,
                reason=(
                    f"Existing runbooks have low similarity ({similarity:.0%}). "
                    "Generate new runbook for this specific scenario."
                )
            )
