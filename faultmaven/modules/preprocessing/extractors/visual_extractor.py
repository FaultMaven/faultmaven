"""
VISUAL_EVIDENCE Extractor

Processes visual evidence (screenshots, diagrams, charts) using multimodal LLM.
This is a placeholder that returns metadata - actual implementation will use
vision-capable LLM (GPT-4V, Claude 3, Gemini Pro Vision) in Phase 3.

The extractor uses the MULTIMODAL_PROVIDER setting from .env, which allows
specifying a different LLM provider for visual processing than text chat.
"""

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
)


class VisualEvidenceExtractor:
    """
    Vision-based evidence extraction (requires multimodal LLM)

    NOTE: This is a Phase 2 placeholder. Full implementation in Phase 3
    will integrate with vision-capable LLM providers.

    Configuration:
    - Uses MULTIMODAL_PROVIDER from .env (defaults to CHAT_PROVIDER if not set)
    - Supported providers: openai (GPT-4V/GPT-4o), anthropic (Claude 3.5), gemini (Gemini 1.5 Pro)
    - Access via: settings.llm.get_multimodal_provider(), get_multimodal_api_key(), get_multimodal_model()
    """

    @property
    def strategy_name(self) -> str:
        return "vision"

    @property
    def llm_calls_used(self) -> int:
        # Phase 3 implementation will use 1 LLM call per image
        return 0  # Placeholder returns 0

    def extract(self, content: str) -> ExtractResult:
        """
        Extract information from visual evidence.

        Phase 2 Implementation: Returns metadata placeholder.
        Phase 3 Implementation: Will use multimodal LLM via MULTIMODAL_PROVIDER.

        Per the Extractor Protocol the dispatch only passes ``content``; the
        original filename and content_type live on the upstream Attachment
        and are surfaced in the ``file_meta`` populated by the orchestrator,
        not by this extractor. Phase 3 will receive raw bytes (see
        investigation_service._is_binary_content) rather than a UTF-8-replaced
        string; until then the placeholder runs over the metadata-only string
        produced by _binary_placeholder.

        Args:
            content: Metadata-only placeholder string today; raw image bytes
                in Phase 3.

        Returns:
            Placeholder ExtractResult indicating vision processing is pending.
        """
        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Phase 2: Return placeholder. The content here is the metadata
        # string from _binary_placeholder (filename/content_type/size live in
        # there for human inspection); Phase 3 will swap in real bytes.
        placeholder = f"""=== VISUAL EVIDENCE ANALYSIS ===

⚠️  Vision processing not yet implemented (Phase 3)

Content metadata:
{content}

Phase 3 Implementation:
This extractor will use a multimodal LLM (configured via MULTIMODAL_PROVIDER) to:
  1. Analyze screenshot content
  2. Extract visible text and error messages
  3. Identify UI elements and states
  4. Detect graphs/charts and extract metrics
  5. Generate natural language description

Configuration (.env):
  MULTIMODAL_PROVIDER=openai  # or anthropic, gemini
  <PROVIDER>_API_KEY=...      # API key for chosen provider
  <PROVIDER>_MODEL=...        # vision-capable model

Current Status: Placeholder — requires vision-capable LLM integration.

Recommendation:
For now, provide a textual description alongside the image, or extract text
from the screenshot manually.
"""
        return ExtractResult(
            file_extract=placeholder,
            file_meta={
                "size_bytes": len(content),
                "phase": "phase_2_placeholder",
            },
        )
