"""Vectorize File Tool — On-Demand Re-Vectorization for Semantic Search

IMPORTANT: This tool is the **re-vectorization** path, not the primary
ingestion path. Evidence uploaded to a case is vectorized eagerly at
upload time by ``store_in_vector_db_background()``
(``faultmaven/core/preprocessing/vector_storage.py``), which runs after
classification + extraction. That background task populates the case
ChromaDB collection without the agent needing to act.

This tool is invoked by the orchestration layer only when directed
analysis fails on a file that exceeds the size threshold — i.e. to
*re*-index or to index a specific evidence item that wasn't covered by
the primary path. Do not describe this tool as the default vectorization
mechanism.

Design Reference: docs/architecture/data-processing/README.md
"""

import json
import logging
from typing import Any, Dict

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

VECTORIZATION_MAX_SIZE_BYTES = 50_000_000  # 50MB hard cap


class VectorizeFileTool(AgentTool):
    """On-demand re-vectorization of evidence files for semantic search.

    Chunks evidence content, generates embeddings, and stores them in
    ChromaDB. This is the *re-vectorization* path — the primary path
    happens at upload time via ``store_in_vector_db_background()``
    (see module docstring). Auto-triggered by the orchestration layer
    when directed analysis fails on files exceeding the size threshold.

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

        if not self.case_vector_store:
            return ToolResult(
                success=False,
                data=None,
                error="Vector store not available — vectorization is disabled",
            )

        try:
            # Storage redesign 2026-04 phase 2: evidence is case-tied only and
            # accessed via `case.evidence`.
            case = getattr(context, "in_memory_case", None)
            if case is None and context.case_repository is not None:
                case = await context.case_repository.get(context.case_id)

            evidence = None
            if case is not None:
                for ev in getattr(case, "evidence", []) or []:
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
            file_meta = case.find_uploaded_file(evidence.source_file_id)
            content_size = file_meta.size_bytes if file_meta else 0
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

            # Post-010: structural_index lives on uploaded_files (not on
            # evidence.extract — evidence.extract is now the LLM's
            # claim-anchored verbatim quote, much shorter than the full
            # structural index). Pull it from the file row.
            raw_preprocessed = (
                file_meta.structural_index if file_meta is not None else None
            )
            if not raw_preprocessed:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        "Source file has no preprocessed structural_index "
                        "to vectorize (preprocessing may have failed or been "
                        "skipped)."
                    ),
                )
            structural_index = raw_preprocessed
            # Legacy: some preprocessed payloads were stored as a JSON
            # envelope with a `file_extract` key. Strip the envelope so we
            # vectorize prose, not raw JSON.
            try:
                _d = json.loads(raw_preprocessed)
                if isinstance(_d, dict) and "file_extract" in _d:
                    structural_index = _d["file_extract"] or raw_preprocessed
            except (ValueError, TypeError):
                pass  # Plaintext structural_index — vectorize as-is
            if not structural_index:
                return ToolResult(
                    success=False,
                    data=None,
                    error="Source file's structural_index is empty",
                )

            # Determine data type for chunking strategy. Prefer the
            # file-level classification (file_meta.data_type, set at
            # preprocessing time); fall back to evidence.source_type
            # when the file row lacks one.
            from faultmaven.core.preprocessing.models import UnifiedDataType

            data_type_str = (
                file_meta.data_type
                if (file_meta is not None and file_meta.data_type)
                else evidence.source_type.value
            )
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
