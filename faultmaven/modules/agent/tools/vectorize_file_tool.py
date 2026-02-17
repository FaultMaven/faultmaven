"""Vectorize File Tool — Tier 4 On-Demand Vectorization (v4.0)

Chunks the evidence's structural index, generates embeddings, and stores
them in ChromaDB for semantic search. Only called when cheaper tiers
(search_file, deep_analysis) are insufficient and the user approves.

Design Reference: docs/working/DRAFT-data-preprocessing-spec-v4.md Section 5
"""

import logging
from typing import Any, Dict

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

# Size gates for vectorization eligibility
VECTORIZATION_MIN_SIZE_BYTES = 50_000  # 50KB
VECTORIZATION_MAX_SIZE_BYTES = 50_000_000  # 50MB


class VectorizeFileTool(AgentTool):
    """Tier 4 on-demand vectorization of evidence files.

    Chunks the evidence's structural index, generates embeddings, and
    stores them in ChromaDB. After vectorization, the file's content
    is searchable via knowledge_base_search.

    Prerequisites (enforced):
    - File must be >50KB
    - File must be <50MB
    - Agent should have already tried cheaper tiers first
    - Agent should have confirmed with the user before calling
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
            "Chunks the file's structural index, generates embeddings, and stores "
            "them in ChromaDB. After vectorization, use knowledge_base_search to "
            "find content semantically. IMPORTANT: Only call this after confirming "
            "with the user — vectorization takes 10-60 seconds. File must be >50KB "
            "and <50MB."
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
            # Get evidence metadata
            evidence = await context.evidence_service.get_evidence(
                evidence_id=evidence_id,
                organization_id=context.organization_id,
            )

            if not evidence:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence not found: {evidence_id}",
                )

            if evidence.case_id != context.case_id:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence {evidence_id} does not belong to case {context.case_id}",
                )

            # Check size gates
            content_size = getattr(evidence, "content_size_bytes", 0) or 0

            if content_size < VECTORIZATION_MIN_SIZE_BYTES:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"File is too small for vectorization ({content_size} bytes). "
                        f"Minimum is {VECTORIZATION_MIN_SIZE_BYTES} bytes (50KB). "
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
                f"vectorize_file: {evidence_id}, "
                f"content_size={content_size}, data_type={data_type_str}"
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
