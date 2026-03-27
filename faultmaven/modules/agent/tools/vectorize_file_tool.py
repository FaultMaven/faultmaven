"""Vectorize File Tool — On-Demand Vectorization for Semantic Search

Chunks evidence content, generates embeddings, and stores them in
ChromaDB for semantic search. Auto-triggered by the orchestration layer
when directed analysis fails on files exceeding the size threshold.

Design Reference: docs/architecture/data-processing/README.md
"""

import logging
from typing import Any, Dict

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

VECTORIZATION_MAX_SIZE_BYTES = 50_000_000  # 50MB hard cap


class VectorizeFileTool(AgentTool):
    """On-demand vectorization of evidence files for semantic search.

    Chunks evidence content, generates embeddings, and stores them in
    ChromaDB. After vectorization, the file's content is searchable via
    knowledge_base_search. Auto-triggered by the orchestration layer when
    directed analysis fails on files exceeding the size threshold.

    Size gates (enforced):
    - File must exceed VECTORIZATION_MIN_SIZE_BYTES (configurable, default 50KB)
    - File must be <50MB
    """

    def __init__(
        self,
        case_vector_store: Any = None,
        storage_service: Any = None,
    ):
        self.case_vector_store = case_vector_store
        self.storage_service = storage_service

    @property
    def name(self) -> str:
        return "vectorize_file"

    @property
    def description(self) -> str:
        return (
            "Vectorize a previously uploaded evidence file for semantic search. "
            "Chunks the file content, generates embeddings, and stores them in "
            "ChromaDB. After vectorization, use knowledge_base_search to find "
            "content semantically. Triggered automatically when directed analysis "
            "fails on large files."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": "The ID of the evidence artifact to vectorize",
                },
            },
            "required": ["evidence_id"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute vectorization."""
        evidence_id = params.get("evidence_id")

        if not evidence_id:
            return ToolResult(success=False, data=None, error="evidence_id is required")

        if not context.evidence_service:
            return ToolResult(
                success=False, data=None, error="Evidence service not available"
            )

        if not self.case_vector_store:
            return ToolResult(
                success=False,
                data=None,
                error="Vector store not available — vectorization is disabled",
            )

        try:
            # Get evidence metadata (dual-path: standalone table → case-embedded)
            evidence = await context.evidence_service.get_evidence(
                evidence_id=evidence_id,
            )

            # Fallback: case-embedded evidence (unified ingestion pipeline)
            if not evidence:
                case_repo = getattr(context.evidence_service, "case_repository", None)
                if case_repo:
                    case = await case_repo.get(context.case_id)
                    if case:
                        for ev in getattr(case, "evidence", []):
                            if getattr(ev, "evidence_id", None) == evidence_id:
                                evidence = ev
                                break

            if not evidence:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence not found: {evidence_id}",
                )

            if (
                hasattr(evidence, "case_id")
                and evidence.case_id
                and evidence.case_id != context.case_id
            ):
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence {evidence_id} does not belong to case {context.case_id}",
                )

            # Check size gates
            content_size = getattr(evidence, "content_size_bytes", 0) or 0
            settings = get_settings()
            min_size = settings.agent.vectorization_min_size_bytes

            if content_size < min_size:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"File is too small for vectorization ({content_size} bytes). "
                        f"Minimum is {min_size} bytes. "
                        f"Use search_file or deep_analysis instead."
                    ),
                )

            if content_size > VECTORIZATION_MAX_SIZE_BYTES:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"File is too large for vectorization ({content_size} bytes). "
                        f"Maximum is {VECTORIZATION_MAX_SIZE_BYTES} bytes (50MB). "
                        f"Use search_file for targeted searches instead."
                    ),
                )

            # Get the structural index content to vectorize
            structural_index = getattr(evidence, "preprocessed_content", None)
            if not structural_index:
                return ToolResult(
                    success=False,
                    data=None,
                    error="Evidence has no preprocessed content to vectorize",
                )

            # Determine data type for chunking strategy
            from faultmaven.core.preprocessing.models import UnifiedDataType

            data_type_str = getattr(evidence, "data_type", "text")
            try:
                data_type = UnifiedDataType(data_type_str)
            except (ValueError, KeyError):
                data_type = UnifiedDataType.TEXT

            # Run vectorization
            from faultmaven.core.preprocessing.vector_storage import (
                store_in_vector_db_background,
            )

            await store_in_vector_db_background(
                case_id=context.case_id,
                evidence_id=evidence_id,
                structural_index=structural_index,
                data_type=data_type,
                metadata={
                    "evidence_id": evidence_id,
                    "case_id": context.case_id,
                    "data_type": data_type_str,
                },
                case_vector_store=self.case_vector_store,
            )

            logger.info(
                "vectorize_file completed: %s, content_size=%d, data_type=%s",
                evidence_id,
                content_size,
                data_type_str,
            )

            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "content_size_bytes": content_size,
                    "data_type": data_type_str,
                    "message": (
                        "File has been vectorized and is now searchable via "
                        "knowledge_base_search. You can search for specific "
                        "content semantically."
                    ),
                },
            )

        except Exception as e:
            logger.exception(f"vectorize_file failed for {evidence_id}: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=f"Vectorization failed: {str(e)}",
            )
