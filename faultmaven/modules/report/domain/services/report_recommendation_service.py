"""
Report Recommendation Service - Intelligent Report Generation Recommendations

Determines which reports to offer for generation, with runbook similarity
checking against the live knowledge base to warn about likely duplicates.

Scoped to the REQUESTER: the recommendation answers "should *you* generate a
runbook?", and the requester is the principal who will act on the answer, so
the search covers what the requester can read (global ∪ their own items ∪
items shared to their teams). The engine's terminal-turn dedup scopes on the
case owner for the same reason — the two call sites may legitimately disagree
on the same case (owner decision, fm#1030).

Architecture Reference: docs/architecture/knowledge-and-ai/runbook-dedup.md
"""

import logging
from typing import Any, List, Optional

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.report import RunbookMatch

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    ReportRecommendation,
    ReportType,
    RunbookRecommendation,
    RunbookRef,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    build_kb_scope_filter,
    resolve_shared_kb_ids,
)

logger = logging.getLogger(__name__)


class ReportRecommendationService:
    """
    Determines which reports to offer for generation.

    Key Features:
    - Always offers the terminal summary for the case's state
    - Conditionally offers: Runbook (based on similarity search)
    - Surfaces likely duplicates by title and score for the user to judge

    There is deliberately no "reuse" verdict. Best-chunk-max similarity
    detects OVERLAP, not whole-runbook equivalence, and the old ≥0.85
    auto-reuse threshold never fired against any real distribution — so the
    service ships only the ≥0.70 review band and emits the top score as a
    metric for later calibration (owner decision, fm#1030).
    """

    # A match at or above this best-chunk similarity is surfaced for review.
    REVIEW_SIMILARITY_THRESHOLD = 0.70

    def __init__(
        self,
        runbook_kb: RunbookKnowledgeBase,
        *,
        team_service: Any = None,
        share_repository: Any = None,
    ):
        """
        Initialize report recommendation service.

        Args:
            runbook_kb: RunbookKnowledgeBase for similarity search
            team_service: Optional team-membership resolver for the requester's
                team arm. None in standalone — teams do not exist there, so the
                arm is empty by construction (a skip, not a narrowed search).
            share_repository: Optional ``IShareRepository`` backing that arm.
        """
        self.runbook_kb = runbook_kb
        self._team_service = team_service
        self._share_repository = share_repository

    @trace("get_available_report_types")
    async def get_available_report_types(
        self,
        case: Case,
        *,
        requester_user_id: str,
        requester_organization_id: Optional[str] = None,
    ) -> ReportRecommendation:
        """
        Determine which report types to offer for case, for one requester.

        Logic:
        - Terminal summary: ALWAYS available (unique to this incident)
        - Runbook: CONDITIONAL (check for existing similar runbooks)

        Args:
            case: Case object with investigation context
            requester_user_id: The authenticated caller. The similarity search
                is scoped to what THIS principal can read.
            requester_organization_id: The caller's tenant claim; consumed only
                by the team arm (``resolve_shared_kb_ids`` collapses the
                Standalone sentinel under multi-tenant itself).

        Returns:
            ReportRecommendation with available types and runbook suggestion

        Raises:
            KnowledgeBaseError: When the similarity search could not run or
                could not be read — including a requester scope that could not
                be resolved. Never rendered as "no similar runbooks found";
                the route turns it into a 503 refusal (#944).
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

        # Check for existing similar runbooks, within the requester's scope
        existing_runbooks = await self._find_similar_runbooks(
            case,
            requester_user_id=requester_user_id,
            requester_organization_id=requester_organization_id,
        )

        # Generate runbook recommendation based on similarity
        runbook_rec = self._generate_runbook_recommendation(existing_runbooks)

        # Both actions leave generation available — the user judges. (The
        # retired "reuse" action was the only one that withheld it.)
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

    async def _resolve_requester_scope(
        self, requester_user_id: str, requester_organization_id: Optional[str]
    ) -> dict:
        """Resolve the requester's KB read scope for the similarity search.

        global ∪ the requester's own items ∪ items shared to the requester's
        teams — the same allowlist shape as KB retrieval (ADR-011 D3), built
        from the caller's OWN ids so a filter built for user A can never
        surface user B's personal runbooks.

        A team-arm resolution failure is surfaced as a typed refusal, not
        swallowed: a silently narrowed search would underpin a "checked,
        nothing similar" claim it did not establish. The route renders the
        refusal as its 503, alongside every other search-unavailable cause.
        """
        try:
            team_ids: List[str] = []
            if self._team_service:
                team_ids = await self._team_service.list_all_user_team_ids(
                    requester_user_id
                )
            shared_ids = await resolve_shared_kb_ids(
                self._share_repository, team_ids, requester_organization_id
            )
        except Exception as e:
            logger.warning(
                f"Requester scope resolution failed for runbook dedup: {e}",
                exc_info=True,
            )
            raise KnowledgeBaseError(
                "Could not resolve the requester's knowledge-base scope; "
                "refusing to run the runbook similarity search against a "
                "narrower scope than the requester can read",
                error_code="RUNBOOK_SCOPE_RESOLUTION_FAILED",
            ) from e
        return build_kb_scope_filter(requester_user_id, shared_ids)

    async def _find_similar_runbooks(
        self,
        case: Case,
        *,
        requester_user_id: str,
        requester_organization_id: Optional[str],
    ) -> List[RunbookMatch]:
        """
        Find existing runbooks similar to current case, within the requester's
        read scope.

        Uses semantic similarity search on the case's problem description.

        Returns:
            List of similar runbooks sorted by similarity score (descending)
        """
        scope_filter = await self._resolve_requester_scope(
            requester_user_id, requester_organization_id
        )

        # No try/except around this. Every failure mode here — an unavailable
        # embedder, an unreachable ChromaDB — used to collapse into [], which
        # `_generate_runbook_recommendation` reads as "no similar runbooks
        # found" and turns into action="generate". That made the endpoint
        # assert the KB holds nothing similar whenever it was simply unable to
        # look, so the answer was always "generate" and duplicates accumulated
        # (#944). The caller turns the typed error into a refusal.
        similar_runbooks = await self.runbook_kb.search_by_text(
            query_text=self._build_case_query_text(case),
            scope_filter=scope_filter,
            top_k=5,  # Get top 5 matches
            min_similarity=0.65,  # Minimum 65% similarity threshold
        )

        if similar_runbooks:
            logger.info(
                f"Found {len(similar_runbooks)} similar runbooks",
                extra={
                    "case_id": case.case_id,
                    "metric": "runbook.dedup_top_similarity",
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
        self, similar_runbooks: List[RunbookMatch]
    ) -> RunbookRecommendation:
        """
        Generate runbook recommendation based on similarity analysis.

        Single band: a best-chunk match ≥70% is surfaced by title and score
        for the user to judge; below that, generation is recommended without a
        named candidate. There is no auto-"reuse" verdict — see the class
        docstring.

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

        if similarity >= self.REVIEW_SIMILARITY_THRESHOLD:
            return RunbookRecommendation(
                action="review_or_generate",
                existing_runbook=RunbookRef(
                    item_id=best_match.item_id,
                    title=best_match.title,
                    scope=best_match.scope,
                ),
                similarity_score=similarity,
                reason=(
                    f"Found similar runbook ({similarity:.0%} match). "
                    "Review the existing runbook or generate a new one if "
                    "significantly different."
                ),
            )

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
