"""
VISUAL_EVIDENCE Extractor

Processes visual evidence (screenshots, diagrams, charts) using multimodal LLM.
This is a placeholder that returns metadata - actual implementation will use
vision-capable LLM (GPT-4V, Claude 3, Gemini Pro Vision) in Phase 3.

The extractor uses the MULTIMODAL_PROVIDER setting from .env, which allows
specifying a different LLM provider for visual processing than text chat.
"""

from typing import Optional


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

    def extract(self, content: str, filename: Optional[str] = None) -> str:
        """
        Extract information from visual evidence

        Phase 2 Implementation: Returns metadata placeholder
        Phase 3 Implementation: Will use multimodal LLM to:
        - Describe screenshot content
        - Extract text from images (OCR)
        - Identify UI elements and error messages
        - Detect graphs/charts and extract key metrics
        - Identify error states in UI screenshots

        Args:
            content: Binary image data (base64 encoded in production)
            filename: Image filename for format detection

        Returns:
            Placeholder message indicating vision processing required
        """
        # Detect image format
        file_ext = ""
        if filename:
            file_ext = filename.split('.')[-1].lower()

        # Phase 2: Return placeholder
        return f"""=== VISUAL EVIDENCE ANALYSIS ===

⚠️  Vision processing not yet implemented (Phase 3)

File Information:
  • Filename: {filename or 'unknown'}
  • Format: {file_ext or 'unknown'}
  • Size: {len(content)} bytes

Phase 3 Implementation:
This extractor will use multimodal LLM configured via MULTIMODAL_PROVIDER to:
  1. Analyze screenshot content
  2. Extract visible text and error messages
  3. Identify UI elements and states
  4. Detect graphs/charts and extract metrics
  5. Generate natural language description

Configuration (.env):
  MULTIMODAL_PROVIDER=openai  # or anthropic, gemini
  OPENAI_API_KEY=your_key      # API key for chosen provider
  OPENAI_MODEL=gpt-4o          # Vision-capable model

Supported Providers:
  • OpenAI: gpt-4o, gpt-4-turbo (GPT-4 Vision)
  • Anthropic: claude-3-5-sonnet-20241022, claude-3-opus-20240229
  • Google: gemini-1.5-pro, gemini-2.0-flash-exp

Current Status: Placeholder - requires vision-capable LLM integration

Recommendation:
For now, please provide textual description of the visual evidence alongside
the image file, or extract text from screenshots manually.
"""
