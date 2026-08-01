"""
Report Recommendation Service - Intelligent Report Generation Recommendations

Determines which reports to offer for generation with intelligent runbook
similarity checking to prevent duplicate runbook generation.

Architecture Reference: docs/architecture/document-generation-and-closure-design.md
Section 5.4: Intelligent Report Recommendation
"""

import logging
from typing import List

from faultmaven.config.tenant_context import usable_tenant_id
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.observability.tracing import trace

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    ReportRecommendation,
    ReportType,
    RunbookRecommendation,
    SimilarRunbook,
)

logger = logging.getLogger(__name__)


class ReportRecommendationService:
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
    ):
        """
        Initialize report recommendation service.

        Args:
            runbook_kb: RunbookKnowledgeBase for similarity search
        """
        self.runbook_kb = runbook_kb

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
            f"Getting report recommendations for case", extra={"case_id": case.case_id}
        )

        # Terminal summaries are auto-generated
        available_types = [
            (
                ReportType.RESOLUTION_SUMMARY
                if case.state == CaseState.RESOLVED
                else ReportType.CLOSURE_SUMMARY
            ),
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
            runbook_recommendation=runbook_rec,
        )

        logger.info(
            f"Report recommendation generated",
            extra={
                "case_id": case.case_id,
                "runbook_action": runbook_rec.action,
                "available_types": [t.value for t in available_types],
            },
        )

        return recommendation

    async def _find_similar_runbooks(
        self,
        case: Case,
    ) -> List[SimilarRunbook]:
        """
        Find existing runbooks similar to current case, within the case's tenant.

        Uses semantic similarity search on:
        - Problem description
        - Root cause (if available)
        - Resolution steps (if available)
        - Domain/technology tags

        The tenant key comes from ``case.organization_id`` — a similarity search
        is an id-free resolution path, so the org predicate is the only thing
        keeping another tenant's runbooks out of the result set. It is resolved
        through ``usable_tenant_id`` for the same reason
        ``terminal_transitions._find_similar_runbooks_for_case`` does: the case
        stamp comes from the *total* ``get_current_org_id``, so under
        ``TENANT_PROVIDER=multi`` it can be the Standalone sentinel, which is not
        a tenant there. ``search_runbooks`` then fails closed on the ``None``.

        Args:
            case: Case object

        Returns:
            List of similar runbooks sorted by similarity score (descending)
        """
        # Build filters for similarity search
        filters = {}
        if hasattr(case, "domain") and case.domain:
            filters["domain"] = case.domain

        # No try/except around this. Every failure mode here — an unavailable
        # embedder, an unreachable ChromaDB — used to collapse into [], which
        # `_generate_runbook_recommendation` reads as "no similar runbooks
        # found" and turns into action="generate". That made the endpoint
        # assert the KB holds nothing similar whenever it was simply unable to
        # look, so the answer was always "generate" and duplicates accumulated
        # (#944). The caller turns the typed error into a refusal.
        similar_runbooks = await self.runbook_kb.search_by_text(
            query_text=self._build_case_query_text(case),
            organization_id=usable_tenant_id(case.organization_id),
            filters=filters,
            top_k=5,  # Get top 5 matches
            min_similarity=0.65,  # Minimum 65% similarity threshold
        )

        if similar_runbooks:
            logger.info(
                f"Found {len(similar_runbooks)} similar runbooks",
                extra={
                    "case_id": case.case_id,
                    "top_similarity": similar_runbooks[0].similarity_score,
                },
            )
        else:
            logger.debug("No similar runbooks found", extra={"case_id": case.case_id})

        return similar_runbooks

    def _build_case_query_text(self, case: Case) -> str:
        """Build the text used to find runbooks similar to this case.

        Replaces ``_create_case_embedding``, which built exactly this text,
        discarded it, and returned ``[]`` on the belief that "ChromaDB will
        handle embedding generation". ChromaDB does not: it rejects a 0-dim
        vector outright, and the resulting error was swallowed into "no
        similar runbooks found" (#944). Embedding now happens once, in
        ``RunbookKnowledgeBase.search_by_text``, so both dedup callers share
        one implementation.
        """
        searchable_parts = []

        # Add title and description
        if case.title:
            searchable_parts.append(f"Problem: {case.title}")
        if case.description:
            searchable_parts.append(case.description)

        # Add domain if available
        if hasattr(case, "domain") and case.domain:
            searchable_parts.append(f"Domain: {case.domain}")

        # Add tags if available
        if hasattr(case, "tags") and case.tags:
            searchable_parts.append(f"Tags: {', '.join(case.tags)}")

        searchable_text = " ".join(searchable_parts)

        logger.debug(
            "Built case query text for runbook similarity search",
            extra={"case_id": case.case_id, "text_length": len(searchable_text)},
        )

        return searchable_text

    def _generate_runbook_recommendation(
        self, similar_runbooks: List[SimilarRunbook]
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
                reason="No similar runbooks found. Generate new runbook.",
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
                ),
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
                ),
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
                ),
            )
