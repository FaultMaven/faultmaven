"""Redis Report Store Implementation

Purpose: Redis + ChromaDB hybrid implementation of IReportStore interface

This module provides efficient report storage using:
- Redis for metadata and fast querying
- ChromaDB for content storage and runbook similarity search

Architecture:
- Report metadata: Redis hashes (fast O(1) lookups)
- Report content: ChromaDB documents (efficient text storage)
- Indexes: Redis sorted sets (version ordering, case lookups)
- Runbooks: Automatically indexed in RunbookKnowledgeBase

Key Features:
- Fast metadata queries via Redis
- Efficient content storage via ChromaDB
- Report versioning support (up to 5 versions per type)
- Automatic runbook indexing for similarity search
- TTL support (90 days post-closure)
"""

import json
import logging
from datetime import datetime
from faultmaven.models import parse_utc_timestamp
from typing import List, Optional, Dict, Any

import redis.asyncio as redis

from faultmaven.models.report import (
    CaseReport,
    ReportType,
    ReportStatus,
    RunbookSource
)
from faultmaven.models.interfaces_report import IReportStore
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.exceptions import ServiceException, ValidationException


logger = logging.getLogger(__name__)


class RedisReportStore(IReportStore):
    """
    Redis + ChromaDB hybrid implementation of report storage.

    Storage Strategy:
    - Metadata in Redis (report_id, case_id, type, version, timestamps)
    - Content in ChromaDB (full markdown text)
    - Runbooks automatically indexed in RunbookKnowledgeBase

    Redis Key Schema:
    - case:{case_id}:reports              → Sorted set (all reports by timestamp)
    - report:{report_id}:metadata         → Hash (report metadata)
    - case:{case_id}:reports:{type}       → Sorted set (reports by type, version desc)
    - case:{case_id}:reports:current      → Hash (type → current report_id)
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        vector_store: ChromaDBVectorStore,
        runbook_kb: Optional[RunbookKnowledgeBase] = None
    ):
        """
        Initialize Redis report store.

        Args:
            redis_client: Redis client instance
            vector_store: ChromaDB vector store for content storage
            runbook_kb: Optional RunbookKnowledgeBase for auto-indexing runbooks
        """
        self.redis = redis_client
        self.vector_store = vector_store
        self.runbook_kb = runbook_kb
        self.logger = logging.getLogger(__name__)

        # ChromaDB collection for non-runbook reports
        self.reports_collection = "faultmaven_case_reports"

    async def save_report(self, report: CaseReport) -> bool:
        """
        Save report with content to storage.

        Process:
        1. Mark previous version as not current (if exists)
        2. Store content in ChromaDB
        3. Store metadata in Redis
        4. Update case report indexes
        5. If runbook, auto-index in RunbookKnowledgeBase

        Args:
            report: CaseReport object with full content

        Returns:
            True if saved successfully

        Raises:
            ServiceException: If storage operation fails
        """
        try:
            case_id = report.case_id
            report_id = report.report_id
            report_type = report.report_type

            self.logger.info(
                f"Saving report",
                extra={
                    "report_id": report_id,
                    "case_id": case_id,
                    "report_type": report_type.value,
                    "version": report.version
                }
            )

            # Use Redis pipeline for atomic operations
            pipe = self.redis.pipeline()

            # 1. Mark previous version as not current
            current_key = f"case:{case_id}:reports:current"
            old_report_id = await self.redis.hget(current_key, report_type.value)

            if old_report_id:
                old_report_id = old_report_id.decode() if isinstance(old_report_id, bytes) else old_report_id
                old_metadata_key = f"report:{old_report_id}:metadata"
                pipe.hset(old_metadata_key, "is_current", "false")

            # 2. Store content in ChromaDB
            await self._store_content_in_chromadb(report)

            # 3. Store metadata in Redis
            metadata_key = f"report:{report_id}:metadata"
            metadata = self._serialize_report_metadata(report)
            pipe.hset(metadata_key, mapping=metadata)

            # 4. Update case report indexes
            # Add to case reports sorted set (sorted by timestamp)
            reports_key = f"case:{case_id}:reports"
            timestamp_score = parse_utc_timestamp(report.generated_at).timestamp()
            pipe.zadd(reports_key, {report_id: timestamp_score})

            # Add to type-specific sorted set (sorted by version desc)
            type_key = f"case:{case_id}:reports:{report_type.value}"
            pipe.zadd(type_key, {report_id: -report.version})  # Negative for descending

            # Update current report for this type
            pipe.hset(current_key, report_type.value, report_id)

            # Execute pipeline
            await pipe.execute()

            # 5. If runbook, auto-index in KB
            if report_type == ReportType.RUNBOOK and self.runbook_kb:
                await self._index_runbook(report)

            self.logger.info(
                f"Report saved successfully",
                extra={"report_id": report_id, "case_id": case_id}
            )

            return True

        except Exception as e:
            self.logger.error(f"Failed to save report: {e}", exc_info=True)
            raise ServiceException(f"Report storage failed: {str(e)}")

    async def get_report(self, report_id: str) -> Optional[CaseReport]:
        """
        Retrieve report by ID with full content.

        Args:
            report_id: Report identifier

        Returns:
            CaseReport object or None
        """
        try:
            # Get metadata from Redis
            metadata_key = f"report:{report_id}:metadata"
            metadata_raw = await self.redis.hgetall(metadata_key)

            if not metadata_raw:
                return None

            # Deserialize metadata
            metadata = self._deserialize_report_metadata(metadata_raw)

            # Get content from ChromaDB
            content = await self._retrieve_content_from_chromadb(
                report_id,
                metadata.get("report_type")
            )

            if not content:
                self.logger.warning(f"Content not found for report {report_id}")
                return None

            # Construct CaseReport object
            report = CaseReport(
                report_id=report_id,
                case_id=metadata["case_id"],
                report_type=ReportType(metadata["report_type"]),
                title=metadata["title"],
                content=content,
                format=metadata.get("format", "markdown"),
                generation_status=ReportStatus(metadata["generation_status"]),
                generated_at=metadata["generated_at"],
                generation_time_ms=int(metadata["generation_time_ms"]),
                is_current=metadata["is_current"] == "true",
                version=int(metadata["version"]),
                linked_to_closure=metadata.get("linked_to_closure", "false") == "true",
                metadata=json.loads(metadata.get("metadata_json", "null"))
            )

            return report

        except Exception as e:
            self.logger.error(f"Failed to retrieve report {report_id}: {e}", exc_info=True)
            return None

    async def get_case_reports(
        self,
        case_id: str,
        include_history: bool = False,
        report_type: Optional[ReportType] = None
    ) -> List[CaseReport]:
        """
        Get reports for a case.

        Args:
            case_id: Case identifier
            include_history: Return all versions or only current
            report_type: Optional filter by type

        Returns:
            List of CaseReport objects
        """
        try:
            report_ids = []

            if include_history:
                # Get all reports for case
                if report_type:
                    # Filter by type
                    type_key = f"case:{case_id}:reports:{report_type.value}"
                    # Get all (sorted by version desc)
                    report_ids_bytes = await self.redis.zrange(type_key, 0, -1)
                else:
                    # Get all reports (sorted by timestamp)
                    reports_key = f"case:{case_id}:reports"
                    report_ids_bytes = await self.redis.zrange(reports_key, 0, -1)

                report_ids = [
                    rid.decode() if isinstance(rid, bytes) else rid
                    for rid in report_ids_bytes
                ]
            else:
                # Get only current versions
                current_key = f"case:{case_id}:reports:current"

                if report_type:
                    # Get current for specific type
                    rid = await self.redis.hget(current_key, report_type.value)
                    if rid:
                        report_ids = [rid.decode() if isinstance(rid, bytes) else rid]
                else:
                    # Get all current reports
                    current_map = await self.redis.hgetall(current_key)
                    report_ids = [
                        v.decode() if isinstance(v, bytes) else v
                        for v in current_map.values()
                    ]

            # Retrieve each report
            reports = []
            for report_id in report_ids:
                report = await self.get_report(report_id)
                if report:
                    reports.append(report)

            # Sort by report type and version (desc)
            reports.sort(key=lambda r: (r.report_type.value, -r.version))

            self.logger.info(
                f"Retrieved {len(reports)} reports for case",
                extra={"case_id": case_id, "include_history": include_history}
            )

            return reports

        except Exception as e:
            self.logger.error(f"Failed to retrieve reports for case {case_id}: {e}", exc_info=True)
            return []

    async def get_latest_reports_for_closure(self, case_id: str) -> List[CaseReport]:
        """Get current version of all report types for case closure."""
        return await self.get_case_reports(case_id, include_history=False)

    async def mark_reports_linked_to_closure(
        self,
        case_id: str,
        report_ids: List[str]
    ) -> bool:
        """
        Mark reports as linked to case closure.

        Args:
            case_id: Case identifier
            report_ids: List of report IDs

        Returns:
            True if successful
        """
        try:
            pipe = self.redis.pipeline()

            for report_id in report_ids:
                metadata_key = f"report:{report_id}:metadata"
                pipe.hset(metadata_key, "linked_to_closure", "true")

            await pipe.execute()

            self.logger.info(
                f"Marked {len(report_ids)} reports as linked to closure",
                extra={"case_id": case_id}
            )

            return True

        except Exception as e:
            self.logger.error(f"Failed to mark reports as linked: {e}", exc_info=True)
            return False

    async def delete_case_reports(self, case_id: str) -> bool:
        """
        Delete incident reports and post-mortems for a case (cascade delete).

        IMPORTANT: Runbooks are NOT deleted - they remain independent and
        continue to exist in the knowledge base for similarity search.

        Data Lifecycle Strategy:
        - RUNBOOK: Independent, persists beyond case lifecycle
        - INCIDENT_REPORT: Cascade delete with case
        - POST_MORTEM: Cascade delete with case

        Removes:
        - Incident report and post-mortem metadata from Redis
        - Incident report and post-mortem content from ChromaDB
        - All indexes (but runbooks remain in RunbookKnowledgeBase)

        Args:
            case_id: Case identifier

        Returns:
            True if successful
        """
        try:
            # Get all report IDs for case
            reports_key = f"case:{case_id}:reports"
            report_ids_bytes = await self.redis.zrange(reports_key, 0, -1)
            all_report_ids = [
                rid.decode() if isinstance(rid, bytes) else rid
                for rid in report_ids_bytes
            ]

            if not all_report_ids:
                return True

            # Filter: Only delete incident_reports and post_mortems (keep runbooks)
            reports_to_delete = []
            runbook_ids = []

            for report_id in all_report_ids:
                metadata_key = f"report:{report_id}:metadata"
                metadata_raw = await self.redis.hgetall(metadata_key)

                if metadata_raw:
                    metadata = self._deserialize_report_metadata(metadata_raw)
                    report_type = metadata.get("report_type")

                    if report_type == ReportType.RUNBOOK.value:
                        runbook_ids.append(report_id)
                    else:
                        reports_to_delete.append(report_id)

            if not reports_to_delete:
                self.logger.info(
                    f"No incident_reports or post_mortems to delete for case (runbooks preserved)",
                    extra={"case_id": case_id, "runbook_count": len(runbook_ids)}
                )
                return True

            pipe = self.redis.pipeline()

            # Delete report metadata (only for incident_reports and post_mortems)
            for report_id in reports_to_delete:
                metadata_key = f"report:{report_id}:metadata"
                pipe.delete(metadata_key)

            # Update indexes to remove deleted reports (but keep runbooks)
            # Remove deleted reports from main sorted set
            for report_id in reports_to_delete:
                pipe.zrem(reports_key, report_id)

            # Update current reports hash (remove non-runbook entries)
            for report_id in reports_to_delete:
                # Determine report type from ID or fetch from metadata
                metadata_key = f"report:{report_id}:metadata"
                metadata_raw = await self.redis.hgetall(metadata_key)
                if metadata_raw:
                    metadata = self._deserialize_report_metadata(metadata_raw)
                    report_type = metadata.get("report_type")
                    if report_type:
                        pipe.hdel(f"case:{case_id}:reports:current", report_type)

            # Delete type-specific indexes for incident_report and post_mortem
            pipe.delete(f"case:{case_id}:reports:{ReportType.INCIDENT_REPORT.value}")
            pipe.delete(f"case:{case_id}:reports:{ReportType.POST_MORTEM.value}")
            # NOTE: runbook index is preserved

            await pipe.execute()

            # Delete content from ChromaDB (only for non-runbooks)
            await self.vector_store.delete_documents(reports_to_delete)

            self.logger.info(
                f"Deleted {len(reports_to_delete)} reports for case (cascade delete), "
                f"{len(runbook_ids)} runbooks preserved",
                extra={
                    "case_id": case_id,
                    "deleted_count": len(reports_to_delete),
                    "preserved_runbook_count": len(runbook_ids)
                }
            )

            return True

        except Exception as e:
            self.logger.error(f"Failed to delete reports for case {case_id}: {e}", exc_info=True)
            return False

    async def get_report_count(self, case_id: str) -> int:
        """Get total number of reports for case (all versions)."""
        try:
            reports_key = f"case:{case_id}:reports"
            count = await self.redis.zcard(reports_key)
            return count or 0
        except Exception as e:
            self.logger.error(f"Failed to get report count: {e}")
            return 0

    # Helper methods

    async def _store_content_in_chromadb(self, report: CaseReport) -> None:
        """Store report content in ChromaDB."""
        documents = [{
            "id": report.report_id,
            "content": report.content,
            "metadata": {
                "report_id": report.report_id,
                "case_id": report.case_id,
                "report_type": report.report_type.value,
                "title": report.title,
                "generated_at": report.generated_at,
                "version": report.version,
            }
        }]

        await self.vector_store.add_documents(documents)

    async def _retrieve_content_from_chromadb(
        self,
        report_id: str,
        report_type: Optional[str] = None
    ) -> Optional[str]:
        """Retrieve report content from ChromaDB."""
        try:
            # Query by document ID
            # Note: ChromaDB doesn't have direct get_by_id in async interface
            # Use query with where filter
            results = await self.vector_store.query_by_embedding(
                query_embedding=[0.0] * 1024,  # Dummy embedding for ID lookup
                where={"report_id": report_id},
                top_k=1
            )

            if results and "documents" in results and results["documents"]:
                docs = results["documents"][0]
                if docs:
                    return docs[0]

            return None

        except Exception as e:
            self.logger.warning(f"Failed to retrieve content from ChromaDB: {e}")
            return None

    async def _index_runbook(self, report: CaseReport) -> None:
        """Auto-index runbook in RunbookKnowledgeBase."""
        if not self.runbook_kb:
            return

        try:
            await self.runbook_kb.index_runbook(
                runbook=report,
                source=RunbookSource.INCIDENT_DRIVEN,
                case_title=report.title,
                domain=report.metadata.domain if report.metadata else "general",
                tags=report.metadata.tags if report.metadata else []
            )
        except Exception as e:
            self.logger.warning(f"Failed to index runbook: {e}")
            # Don't fail report storage if indexing fails

    def _serialize_report_metadata(self, report: CaseReport) -> Dict[str, str]:
        """Serialize report metadata for Redis storage."""
        return {
            "report_id": report.report_id,
            "case_id": report.case_id,
            "report_type": report.report_type.value,
            "title": report.title,
            "format": report.format,
            "generation_status": report.generation_status.value,
            "generated_at": report.generated_at,
            "generation_time_ms": str(report.generation_time_ms),
            "is_current": "true" if report.is_current else "false",
            "version": str(report.version),
            "linked_to_closure": "true" if report.linked_to_closure else "false",
            "metadata_json": json.dumps(report.metadata.dict() if report.metadata else None)
        }

    def _deserialize_report_metadata(self, metadata_raw: Dict[bytes, bytes]) -> Dict[str, Any]:
        """Deserialize report metadata from Redis."""
        metadata = {}
        for k, v in metadata_raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            value = v.decode() if isinstance(v, bytes) else v
            metadata[key] = value
        return metadata
