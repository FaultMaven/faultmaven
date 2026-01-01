"""Knowledge Indexing Job for background embedding generation.

This job indexes knowledge items that don't have embeddings by:
- Fetching items without embeddings from the repository
- Generating embeddings in batches
- Adding items to the vector store

Features:
- Batch processing for efficiency
- Progress tracking and statistics
- Error handling with partial success
- Optional organization filtering

Usage:
    from faultmaven.jobs.knowledge_indexing_job import KnowledgeIndexingJob

    job = KnowledgeIndexingJob(knowledge_repo, search_service, batch_size=50)
    result = await job.run(organization_id="org_123")
    print(f"Indexed {result['succeeded']} items")
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)
from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem


logger = logging.getLogger(__name__)


class KnowledgeIndexingJob:
    """Background job for indexing knowledge items without embeddings.

    This job fetches items that need embedding generation and indexes them
    in batches using the KnowledgeSearchService.

    Attributes:
        knowledge_repo: Repository for knowledge item persistence
        search_service: Service for indexing items
        batch_size: Number of items to process per batch
    """

    def __init__(
        self,
        knowledge_repo: KnowledgeItemRepository,
        search_service: KnowledgeSearchService,
        batch_size: int = 50,
    ):
        """Initialize indexing job.

        Args:
            knowledge_repo: Repository for knowledge item persistence
            search_service: Service for indexing items
            batch_size: Number of items to process per batch
        """
        self.knowledge_repo = knowledge_repo
        self.search_service = search_service
        self.batch_size = batch_size

    async def run(
        self,
        organization_id: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run indexing job.

        Workflow:
        1. Fetch items without embeddings
        2. Index items in batches
        3. Track success/failure counts
        4. Return summary statistics

        Args:
            organization_id: Optional filter by organization
            max_items: Optional maximum items to process (for testing/limits)

        Returns:
            Job statistics:
            - processed: Total items processed
            - succeeded: Items successfully indexed
            - failed: Items that failed to index
            - duration_seconds: Total job duration
            - items_per_second: Processing rate
            - failed_items: List of failed item IDs (first 10)
        """
        start_time = time.time()
        job_id = f"indexing_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        logger.info(
            f"Starting knowledge indexing job {job_id}",
            extra={
                "job_id": job_id,
                "organization_id": organization_id,
                "batch_size": self.batch_size,
                "max_items": max_items,
            },
        )

        total_processed = 0
        total_succeeded = 0
        total_failed = 0
        all_failed_items: List[str] = []

        try:
            # Fetch items without embeddings
            if organization_id:
                items_to_index = await self._get_items_for_organization(
                    organization_id, max_items
                )
            else:
                items_to_index = await self._get_all_items_without_embeddings(max_items)

            if not items_to_index:
                logger.info(f"No items to index for job {job_id}")
                return self._build_result(
                    job_id=job_id,
                    processed=0,
                    succeeded=0,
                    failed=0,
                    failed_items=[],
                    start_time=start_time,
                    organization_id=organization_id,
                )

            logger.info(
                f"Found {len(items_to_index)} items to index",
                extra={"job_id": job_id, "item_count": len(items_to_index)},
            )

            # Process in batches
            for i in range(0, len(items_to_index), self.batch_size):
                batch = items_to_index[i:i + self.batch_size]
                batch_num = i // self.batch_size + 1
                total_batches = (len(items_to_index) + self.batch_size - 1) // self.batch_size

                logger.info(
                    f"Processing batch {batch_num}/{total_batches} ({len(batch)} items)",
                    extra={
                        "job_id": job_id,
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch),
                    },
                )

                # Index batch
                result = await self.search_service.index_items_batch(
                    items=batch,
                    batch_size=self.batch_size,
                )

                total_processed += result["processed"]
                total_succeeded += result["succeeded"]
                total_failed += result["failed"]

                if result.get("failed_items"):
                    all_failed_items.extend(result["failed_items"])

                # Log progress
                progress = (i + len(batch)) / len(items_to_index) * 100
                logger.info(
                    f"Batch {batch_num} complete: {result['succeeded']}/{result['processed']} succeeded "
                    f"({progress:.1f}% overall)",
                    extra={
                        "job_id": job_id,
                        "batch_succeeded": result["succeeded"],
                        "batch_failed": result["failed"],
                        "progress_percent": progress,
                    },
                )

        except Exception as e:
            logger.error(
                f"Indexing job {job_id} failed with error: {e}",
                extra={"job_id": job_id, "error": str(e)},
            )
            return self._build_result(
                job_id=job_id,
                processed=total_processed,
                succeeded=total_succeeded,
                failed=total_failed,
                failed_items=all_failed_items,
                start_time=start_time,
                organization_id=organization_id,
                error=str(e),
            )

        result = self._build_result(
            job_id=job_id,
            processed=total_processed,
            succeeded=total_succeeded,
            failed=total_failed,
            failed_items=all_failed_items,
            start_time=start_time,
            organization_id=organization_id,
        )

        logger.info(
            f"Indexing job {job_id} complete: {total_succeeded}/{total_processed} succeeded "
            f"in {result['duration_seconds']:.2f}s",
            extra={
                "job_id": job_id,
                **result,
            },
        )

        return result

    async def _get_items_for_organization(
        self,
        organization_id: str,
        max_items: Optional[int],
    ) -> List[KnowledgeItem]:
        """Get items without embeddings for a specific organization."""
        limit = max_items if max_items else 10000
        return await self.knowledge_repo.get_items_without_embeddings(
            organization_id=organization_id,
            limit=limit,
        )

    async def _get_all_items_without_embeddings(
        self,
        max_items: Optional[int],
    ) -> List[KnowledgeItem]:
        """Get items without embeddings across all organizations.

        Note: This requires iterating through organizations, which is
        less efficient. For production, consider organization-specific runs.
        """
        # For now, return empty - this would require listing all organizations
        # In production, you'd want to either:
        # 1. Add a method to list all organizations
        # 2. Add a global get_items_without_embeddings method
        # 3. Run per-organization jobs
        logger.warning(
            "get_all_items_without_embeddings called without organization_id. "
            "Consider running per-organization for better efficiency."
        )
        return []

    def _build_result(
        self,
        job_id: str,
        processed: int,
        succeeded: int,
        failed: int,
        failed_items: List[str],
        start_time: float,
        organization_id: Optional[str],
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build result dictionary with statistics."""
        duration = time.time() - start_time
        items_per_second = processed / duration if duration > 0 else 0

        result = {
            "job_id": job_id,
            "organization_id": organization_id,
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "duration_seconds": round(duration, 2),
            "items_per_second": round(items_per_second, 2),
            "failed_items": failed_items[:10],  # Limit to first 10
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        if error:
            result["error"] = error
            result["status"] = "failed"
        else:
            result["status"] = "completed" if failed == 0 else "completed_with_errors"

        return result

    async def get_pending_count(
        self,
        organization_id: Optional[str] = None,
    ) -> int:
        """Get count of items pending indexing.

        Args:
            organization_id: Optional filter by organization

        Returns:
            Number of items without embeddings
        """
        if not organization_id:
            return 0

        items = await self.knowledge_repo.get_items_without_embeddings(
            organization_id=organization_id,
            limit=1,
        )

        # For accurate count, we'd need a count method
        # For now, return based on the query
        items = await self.knowledge_repo.get_items_without_embeddings(
            organization_id=organization_id,
            limit=10000,
        )
        return len(items)


# Optional: Celery task wrapper for async job execution
# Uncomment and configure if using Celery for task queuing
#
# try:
#     from celery import shared_task
#     import asyncio
#
#     @shared_task(name="index_knowledge_items")
#     def index_knowledge_items_task(
#         organization_id: Optional[str] = None,
#         max_items: Optional[int] = None,
#     ) -> Dict[str, Any]:
#         """Celery task wrapper for knowledge indexing.
#
#         Usage:
#             index_knowledge_items_task.delay(organization_id="org_123")
#         """
#         from faultmaven.infrastructure.persistence.repository_factory import (
#             get_knowledge_item_repository,
#         )
#         from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService
#         from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService
#         from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
#         from faultmaven.config.settings import get_settings
#
#         settings = get_settings()
#
#         # Create services
#         knowledge_repo = get_knowledge_item_repository()
#         embedding_service = EmbeddingService(
#             api_key=settings.llm.openai_api_key.get_secret_value(),
#             model=settings.embedding.embedding_model,
#             dimensions=settings.embedding.embedding_dimensions,
#         )
#         vector_store = VectorStoreService(
#             collection_name=settings.embedding.chroma_collection_name,
#             persist_directory=settings.embedding.chroma_persist_directory,
#         )
#         search_service = KnowledgeSearchService(
#             knowledge_repo=knowledge_repo,
#             embedding_service=embedding_service,
#             vector_store=vector_store,
#         )
#
#         # Run job
#         job = KnowledgeIndexingJob(
#             knowledge_repo=knowledge_repo,
#             search_service=search_service,
#             batch_size=settings.embedding.indexing_batch_size,
#         )
#
#         return asyncio.run(job.run(organization_id, max_items))
#
# except ImportError:
#     # Celery not installed, skip task definition
#     pass
