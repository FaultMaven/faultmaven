"""Vectorize File Tool — On-Demand Vectorization for Semantic Search

This tool is the **only** path into a case's ChromaDB collection:
``store_in_vector_db_background()``
(``faultmaven/core/preprocessing/vector_storage.py``) has no other caller.
Uploading evidence does not index it — despite that function's name, nothing
runs at upload time.

Indexing is therefore always agent-driven, via the orchestration layer:
proactively as a background task for evidence above the size threshold, or
reactively when directed analysis fails on a large file. Nothing else will
cover a file that this tool does not.

That makes what this tool *reports* load-bearing. A file it did not index is
not in the collection, so a later ``case_evidence_search`` cannot find it —
and if the tool claimed otherwise, the model reads that empty search as a
statement about the file's contents (#941).

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

#: What the investigating model is told when a file becomes searchable. This
#: is the surface — everything else about vectorization is an intermediate
#: value the model never sees.
VECTORIZED_SYSTEM_MESSAGE = (
    "\n\n[SYSTEM] This file has been automatically indexed for semantic "
    "search. Use case_evidence_search to find content by meaning rather "
    "than keywords."
)


def append_vectorization_advisory(text: str, indexed: bool) -> str:
    """Append the searchability advisory, and only if the file really is indexed.

    The rule lives here rather than at each emission site because it was
    previously restated across different orchestration layers
    with the message text copied inline. Multiple
    copies of a rule are chances to keep the ``if`` and lose the condition,
    and the advisory is not a cosmetic string: it sends the model to
    ``case_evidence_search``, so claiming it for a file that was never written
    guarantees an empty search the model then reads as a statement about the
    file's contents (#941).

    Taking ``indexed`` as an argument means the caller's surrounding ``if``
    cannot carry the claim on its own — this returns ``text`` unchanged for a
    file that indexed nothing however it was reached.
    """
    if not indexed or VECTORIZED_SYSTEM_MESSAGE in text:
        return text
    return text + VECTORIZED_SYSTEM_MESSAGE


class VectorizeFileTool(AgentTool):
    """On-demand vectorization of evidence files for semantic search.

    Chunks evidence content, generates embeddings, and stores them in
    ChromaDB. The only writer to a case's collection (see module
    docstring). Auto-triggered by the orchestration layer — proactively
    for evidence above the size threshold, reactively when directed
    analysis fails on a large file.

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
            "ChromaDB. After vectorization, use case_evidence_search to find "
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
                VectorIndexOutcome,
                store_in_vector_db_background,
            )

            outcome = await store_in_vector_db_background(
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
                "vectorize_file %s: %s, content_size=%d, data_type=%s",
                outcome.value,
                evidence_id,
                content_size,
                data_type_str,
            )

            # Only INDEXED makes the file searchable, and only INDEXED may say
            # so. Reporting the others as success announced an index that does
            # not exist; the model then searches, gets nothing back, and reads
            # it as "this file does not contain that" — an unwritten index
            # laundered into a finding about the evidence (#941).
            if outcome is not VectorIndexOutcome.INDEXED:
                return self._not_indexed(outcome, evidence_id, data_type_str)

            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "content_size_bytes": content_size,
                    "data_type": data_type_str,
                    # Stated on BOTH success arms, not just the negative one:
                    # the engine reads this key rather than the message text,
                    # and a check that depends on a key being absent passes for
                    # any caller that forgets to set it (#941).
                    "indexed": True,
                    "message": (
                        "File has been vectorized and is now searchable via "
                        "case_evidence_search. You can search for specific "
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

    @staticmethod
    def _not_indexed(outcome: Any, evidence_id: str, data_type_str: str) -> ToolResult:
        """Render a non-``INDEXED`` outcome without claiming an index exists.

        Each arm says what happened and, where the file is genuinely absent
        from the index, says what may NOT be concluded from a later empty
        ``case_evidence_search`` — same rule the KB adapters follow (#943): a
        retrieval layer that could not do its job establishes nothing about the
        evidence.
        """
        from faultmaven.core.preprocessing.vector_storage import VectorIndexOutcome

        # Not a failure: the file really has no chunkable content, which is a
        # fact about the file and safe for the model to act on.
        if outcome is VectorIndexOutcome.NOTHING_TO_INDEX:
            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "data_type": data_type_str,
                    "indexed": False,
                    "message": (
                        "This file has no chunkable content, so there is "
                        "nothing to index and case_evidence_search will not "
                        "return it. Read it directly with read_file or "
                        "search_file instead."
                    ),
                },
                error=None,
            )

        reason = (
            "the embedding model is unavailable"
            if outcome is VectorIndexOutcome.EMBEDDER_UNAVAILABLE
            else "indexing failed"
        )
        return ToolResult(
            success=False,
            data=None,
            error=(
                f"This file was NOT vectorized: {reason}. It is not in the "
                f"case evidence index, so case_evidence_search cannot find it "
                f"— an empty result for this file says nothing about its "
                f"contents. Read it directly with read_file or search_file."
            ),
        )
