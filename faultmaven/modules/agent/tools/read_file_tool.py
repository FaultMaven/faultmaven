"""Read File Tool for Agent Execution (TASK-015)

This tool allows agents to read the contents of evidence artifacts
uploaded to a case during investigation sessions.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import base64
import logging
from typing import Any, Dict, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext, tool_registry

logger = logging.getLogger(__name__)

# Maximum file size to read as text (in bytes)
MAX_TEXT_SIZE = 1024 * 1024  # 1MB

# MIME types that can be read as text
TEXT_MIME_TYPES = {
    "text/plain",
    "text/csv",
    "text/html",
    "text/xml",
    "text/markdown",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "text/x-python",
    "text/x-java-source",
    "text/x-c",
    "text/x-c++",
    "text/x-go",
    "text/x-rust",
    "text/x-typescript",
}


class ReadFileTool(AgentTool):
    """Tool for reading evidence artifact file contents.

    Allows the agent to read the contents of files that have been
    uploaded as evidence artifacts for the current case.

    For text files, returns the file contents as a string.
    For binary files, returns a base64-encoded representation
    or a summary indicating the file type.
    """

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of an evidence file by its ID. "
            "Returns the file contents as text for text-based files, "
            "or a summary for binary files. Use list_evidence first "
            "to find available evidence IDs."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": "The ID of the evidence artifact to read",
                },
                "max_lines": {
                    "type": "integer",
                    "description": (
                        "Maximum number of lines to return (optional, default: all). "
                        "Use for large log files."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Line offset to start reading from (optional, default: 0). "
                        "Use with max_lines for pagination."
                    ),
                },
            },
            "required": ["evidence_id"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Read an evidence file's contents.

        Evidence is case-tied: we look it up on ``case.evidence`` (via
        ``context.case_repository`` / ``context.in_memory_case``), resolve
        the backing file by walking ``evidence.source_file_id`` →
        ``uploaded_files.storage_ref``, and read the raw bytes via
        ``FileStorageService``. Chat-extracted evidence (source_file_id
        IS NULL, source_type=USER_DESCRIPTION) has no stored file and
        is rejected with a "no backing file" error.

        Args:
            params: Parameters including evidence_id, max_lines, offset
            context: Execution context

        Returns:
            ToolResult with file contents or error
        """
        evidence_id = params.get("evidence_id")
        max_lines = params.get("max_lines")
        offset = params.get("offset", 0)

        if not evidence_id:
            return ToolResult(
                success=False,
                data=None,
                error="evidence_id is required",
            )

        try:
            case = context.in_memory_case
            if case is None and context.case_repository is not None:
                case = await context.case_repository.get(context.case_id)
            if case is None:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Case not found: {context.case_id}",
                )

            evidence = None
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    evidence = ev
                    break

            if evidence is None:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence not found: {evidence_id}",
                )

            # Domain Evidence sits on `case.evidence`, so case-scoping is
            # already implicit. Belt-and-braces check kept for parity.
            ev_case_id = getattr(evidence, "case_id", context.case_id)
            if ev_case_id and ev_case_id != context.case_id:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"Evidence {evidence_id} does not belong to "
                        f"case {context.case_id}"
                    ),
                )

            file_meta = case.find_uploaded_file(evidence.source_file_id)
            storage_ref = file_meta.storage_ref if file_meta else None
            filename = (file_meta.filename if file_meta else None) or evidence_id
            # The name the LLM sees. Split from ``filename`` because the
            # latter drives extension sniffing and has to stay the stored
            # one (#666).
            shown_name = (file_meta.display_name if file_meta else None) or evidence_id
            if not storage_ref:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"Evidence {evidence_id} has no backing file "
                        f"(raw file not stored)"
                    ),
                )

            # Load via FileStorageService. We construct it on-demand rather
            # than plumbing a new dependency through ToolContext; it resolves
            # the configured storage backend itself.
            from faultmaven.modules.evidence.domain.services.file_storage_service import (
                FileStorageService,
            )

            storage = FileStorageService()
            try:
                file_data = await storage.retrieve_file(storage_ref)
            except Exception as e:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Could not read evidence file: {e}",
                )

            # Domain Evidence has no mime_type; infer a loose default so the
            # text-vs-binary heuristics in _process_file_content keep working.
            mime_type = "text/plain"

            content = self._process_file_content(
                file_data=file_data,
                filename=filename,
                mime_type=mime_type,
                max_lines=max_lines,
                offset=offset,
                display_name=shown_name,
            )

            logger.info(
                f"Read file {evidence_id} ({filename}, {len(file_data)} bytes) "
                f"for execution {context.execution_id}"
            )

            return ToolResult(
                success=True,
                data={
                    "content": content,
                    "label": shown_name,
                    "mime_type": mime_type,
                    "file_size": len(file_data),
                    "evidence_id": evidence_id,
                    "description": getattr(evidence, "summary", None),
                },
            )

        except Exception as e:
            logger.exception(f"Failed to read evidence {evidence_id}: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=f"Failed to read evidence: {str(e)}",
            )

    def _process_file_content(
        self,
        file_data: bytes,
        filename: str,
        mime_type: str,
        max_lines: Optional[int] = None,
        offset: int = 0,
        display_name: Optional[str] = None,
    ) -> str:
        """Process file content based on type.

        Args:
            file_data: Raw file bytes
            filename: Original filename — the STORED name. Drives the
                text-vs-binary decision, which reads the extension, so it
                has to stay the real one.
            mime_type: File MIME type
            max_lines: Maximum lines to return
            offset: Line offset to start from
            display_name: Name to put in the returned placeholders, which
                the LLM reads. Defaults to ``filename``; the caller passes
                ``UploadedFile.display_name`` so a minted
                ``pasted-content-<ts>.txt`` never reaches the model (#666).

        Returns:
            Processed content as string
        """
        shown = display_name or filename
        # Check if file is too large
        if len(file_data) > MAX_TEXT_SIZE and max_lines is None:
            return self._handle_large_file(file_data, shown, mime_type)

        # Check if it's a text file
        if self._is_text_file(mime_type, filename):
            return self._process_text_file(file_data, max_lines, offset)

        # Check if it's an image
        if mime_type.startswith("image/"):
            return self._summarize_image(shown, mime_type, len(file_data))

        # Check if it's a PDF
        if mime_type == "application/pdf":
            return self._summarize_pdf(shown, len(file_data))

        # For other binary files, return base64 with warning
        if len(file_data) <= 10000:  # Only encode small binary files
            encoded = base64.b64encode(file_data).decode("utf-8")
            return (
                f"[Binary file: {shown}, {len(file_data)} bytes]\n"
                f"Base64 encoded content:\n{encoded}"
            )

        return f"[Binary file: {shown}, {len(file_data)} bytes, content not displayed]"

    def _is_text_file(self, mime_type: str, filename: str) -> bool:
        """Check if file is a text file."""
        # Check MIME type
        if mime_type in TEXT_MIME_TYPES:
            return True
        if mime_type.startswith("text/"):
            return True

        # Check extension
        text_extensions = {
            ".txt",
            ".log",
            ".json",
            ".xml",
            ".yaml",
            ".yml",
            ".csv",
            ".md",
            ".py",
            ".js",
            ".ts",
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".go",
            ".rs",
            ".rb",
            ".sh",
            ".bash",
            ".sql",
            ".html",
            ".css",
            ".conf",
            ".ini",
            ".cfg",
            ".env",
            ".properties",
            ".toml",
        }
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext in text_extensions

    def _process_text_file(
        self,
        file_data: bytes,
        max_lines: Optional[int] = None,
        offset: int = 0,
    ) -> str:
        """Process text file content with optional line limits."""
        try:
            # Try UTF-8 first, then fallback to latin-1
            try:
                content = file_data.decode("utf-8")
            except UnicodeDecodeError:
                content = file_data.decode("latin-1")

            # Apply line limits if specified
            if max_lines is not None or offset > 0:
                lines = content.splitlines()
                total_lines = len(lines)

                # Apply offset and limit
                start = min(offset, total_lines)
                end = start + max_lines if max_lines else total_lines

                selected_lines = lines[start:end]

                # Add line numbers and pagination info
                result_lines = []
                for i, line in enumerate(selected_lines, start=start + 1):
                    result_lines.append(f"{i:6d} | {line}")

                header = (
                    f"[Lines {start + 1}-{min(end, total_lines)} "
                    f"of {total_lines} total]\n"
                )
                return header + "\n".join(result_lines)

            return content

        except Exception as e:
            return f"[Error reading file: {str(e)}]"

    def _handle_large_file(
        self,
        file_data: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        """Handle large files by returning a summary."""
        try:
            content = file_data.decode("utf-8", errors="replace")
            lines = content.splitlines()
            total_lines = len(lines)

            # Return first and last 50 lines
            preview_lines = 50
            first_lines = lines[:preview_lines]
            last_lines = (
                lines[-preview_lines:] if total_lines > preview_lines * 2 else []
            )

            result = [
                f"[Large file: {filename}, {len(file_data)} bytes, {total_lines} lines]",
                f"[Showing first {preview_lines} lines and last {preview_lines} lines]",
                "",
                "--- First lines ---",
            ]
            for i, line in enumerate(first_lines, start=1):
                result.append(f"{i:6d} | {line}")

            if last_lines:
                result.append("")
                result.append(
                    f"--- ... {total_lines - preview_lines * 2} lines omitted ... ---"
                )
                result.append("")
                result.append("--- Last lines ---")
                start_line = total_lines - preview_lines + 1
                for i, line in enumerate(last_lines, start=start_line):
                    result.append(f"{i:6d} | {line}")

            result.append("")
            result.append(
                "[Use max_lines and offset parameters to read specific sections]"
            )

            return "\n".join(result)

        except Exception as e:
            return (
                f"[Large file: {filename}, {len(file_data)} bytes, error reading: {e}]"
            )

    def _summarize_image(
        self,
        filename: str,
        mime_type: str,
        size: int,
    ) -> str:
        """Return summary for image files."""
        return (
            f"[Image file: {filename}]\n"
            f"Type: {mime_type}\n"
            f"Size: {size} bytes\n"
            f"\n"
            f"This is an image file. To analyze its contents, describe what "
            f"you're looking for and I can help interpret the image if it's "
            f"a screenshot, diagram, or other visual content."
        )

    def _summarize_pdf(self, filename: str, size: int) -> str:
        """Return summary for PDF files."""
        return (
            f"[PDF file: {filename}]\n"
            f"Size: {size} bytes\n"
            f"\n"
            f"This is a PDF document. PDF text extraction is not currently "
            f"available. If this contains important information, please "
            f"extract the relevant text and provide it directly."
        )


# Register the tool with the agent tool registry
def register_read_file_tool() -> None:
    """Register the read_file tool with the agent tool registry."""
    try:
        tool_registry.register(ReadFileTool())
    except ValueError:
        # Already registered
        pass
