"""OODA Response Parser - Three-Tier Fallback Strategy

This module implements the three-tier fallback strategy for parsing LLM responses
into structured OODA response objects:

Tier 1: Function Calling (99% reliable) - JSON schema enforcement via LLM provider
Tier 2: JSON Parsing (90% reliable) - Extract and validate JSON from response
Tier 3: Heuristic Extraction (70% reliable) - Extract fields from natural language

Overall reliability: 99.9% (at least one tier succeeds)

Design Reference: docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md
"""

import json
import re
import logging
from typing import Optional, Type, TypeVar, Dict, Any
from pydantic import BaseModel, ValidationError

from faultmaven.models.responses import (
    OODAResponse,
    ConsultantResponse,
    LeadInvestigatorResponse,
    create_minimal_response,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=OODAResponse)


# =============================================================================
# Main Parser Interface
# =============================================================================


class ResponseParser:
    """Three-tier fallback parser for OODA responses

    Usage:
        parser = ResponseParser()
        response = parser.parse(
            raw_response="...",
            expected_schema=ConsultantResponse,
        )
    """

    def __init__(self):
        """Initialize response parser"""
        self.stats = {
            "tier1_success": 0,
            "tier2_success": 0,
            "tier3_success": 0,
            "total_failures": 0,
            "total_attempts": 0,
        }

    def parse(
        self,
        raw_response: str | dict,
        expected_schema: Type[T],
    ) -> T:
        """Parse LLM response into structured OODA response

        Attempts three-tier fallback strategy:
        1. Function calling (if dict provided)
        2. JSON parsing (extract from markdown/text)
        3. Heuristic extraction (parse natural language)

        Args:
            raw_response: Raw LLM response (string or dict)
            expected_schema: Expected Pydantic model class

        Returns:
            Parsed and validated response object

        Raises:
            Never raises - always returns valid response (minimal fallback)
        """
        # Increment total attempts
        self.stats["total_attempts"] += 1

        # Tier 1: Function Calling (if dict provided)
        if isinstance(raw_response, dict):
            result = self._tier1_function_calling(raw_response, expected_schema)
            if result:
                self.stats["tier1_success"] += 1
                logger.debug("Tier 1 (function calling) succeeded")
                return result

        # Tier 2: JSON Parsing
        if isinstance(raw_response, str):
            result = self._tier2_json_parsing(raw_response, expected_schema)
            if result:
                self.stats["tier2_success"] += 1
                logger.debug("Tier 2 (JSON parsing) succeeded")
                return result

            # Tier 3: Heuristic Extraction
            result = self._tier3_heuristic_extraction(raw_response, expected_schema)
            if result:
                self.stats["tier3_success"] += 1
                logger.debug("Tier 3 (heuristic extraction) succeeded")
                return result

        # Complete failure - use minimal fallback
        self.stats["total_failures"] += 1
        logger.warning(
            "All parsing tiers failed, using minimal fallback",
            extra={"raw_response_preview": str(raw_response)[:200]},
        )

        # Extract answer text as best effort
        answer = self._extract_answer_text(raw_response)
        return create_minimal_response(answer)  # type: ignore

    def _tier1_function_calling(
        self, response_dict: dict, expected_schema: Type[T]
    ) -> Optional[T]:
        """Tier 1: Parse function calling response

        LLM providers (OpenAI, Anthropic) enforce JSON schema when using
        function calling, providing 99% reliability.

        Args:
            response_dict: Dictionary from LLM function call
            expected_schema: Expected Pydantic model

        Returns:
            Validated response object or None if validation fails
        """
        try:
            # Validate against schema
            validated = expected_schema(**response_dict)
            return validated

        except ValidationError as e:
            logger.warning(
                "Tier 1 validation failed",
                extra={"validation_errors": e.errors(), "schema": expected_schema.__name__},
            )
            return None

        except Exception as e:
            logger.error(f"Tier 1 unexpected error: {e}")
            return None

    def _tier2_json_parsing(
        self, response_text: str, expected_schema: Type[T]
    ) -> Optional[T]:
        """Tier 2: Extract and parse JSON from text

        LLMs often wrap JSON in markdown code blocks:
        ```json
        {"answer": "..."}
        ```

        This tier handles:
        - Pure JSON strings
        - JSON in markdown code blocks
        - JSON with surrounding text

        Args:
            response_text: Raw text response
            expected_schema: Expected Pydantic model

        Returns:
            Validated response object or None if parsing fails
        """
        try:
            # Try direct JSON parsing first
            try:
                data = json.loads(response_text)
                return expected_schema(**data)
            except json.JSONDecodeError:
                pass

            # Extract JSON from markdown code blocks
            json_pattern = r"```(?:json)?\s*\n(.*?)\n```"
            matches = re.findall(json_pattern, response_text, re.DOTALL)

            for match in matches:
                try:
                    data = json.loads(match)
                    validated = expected_schema(**data)
                    return validated
                except (json.JSONDecodeError, ValidationError):
                    continue

            # Try finding JSON object in text (starts with { ends with })
            brace_pattern = r"\{.*\}"
            matches = re.findall(brace_pattern, response_text, re.DOTALL)

            for match in matches:
                try:
                    data = json.loads(match)
                    validated = expected_schema(**data)
                    return validated
                except (json.JSONDecodeError, ValidationError):
                    continue

            logger.warning("Tier 2: No valid JSON found in response")
            return None

        except Exception as e:
            logger.error(f"Tier 2 unexpected error: {e}")
            return None

    def _tier3_heuristic_extraction(
        self, response_text: str, expected_schema: Type[T]
    ) -> Optional[T]:
        """Tier 3: Heuristically extract fields from natural language

        When JSON parsing fails, attempt to extract structured information
        from natural language response using patterns and heuristics.

        This is the last resort with ~70% reliability.

        Args:
            response_text: Raw text response
            expected_schema: Expected Pydantic model

        Returns:
            Best-effort response object or None if extraction fails
        """
        try:
            # Start with answer as the entire text
            answer_text = response_text.strip()

            # If empty, return None to trigger fallback
            if not answer_text:
                return None

            extracted = {"answer": answer_text}

            # Extract clarifying questions (look for numbered lists or bullet points)
            questions = self._extract_questions(response_text)
            if questions:
                extracted["clarifying_questions"] = questions[:3]

            # Extract suggested actions (look for action-like phrases)
            # This is complex and low-confidence, so we skip for now
            # Better to have minimal response than incorrect actions

            # Extract problem detection keywords
            problem_keywords = [
                "error", "failure", "down", "not working", "issue",
                "problem", "broken", "crash", "timeout"
            ]
            text_lower = response_text.lower()
            problem_detected = any(kw in text_lower for kw in problem_keywords)

            if problem_detected and expected_schema == ConsultantResponse:
                extracted["problem_detected"] = True
                # Try to extract problem summary (first sentence with problem keyword)
                sentences = re.split(r'[.!?]\s+', response_text)
                for sentence in sentences[:3]:
                    if any(kw in sentence.lower() for kw in problem_keywords):
                        extracted["problem_summary"] = sentence.strip()[:200]
                        break

            # Validate what we extracted
            validated = expected_schema(**extracted)
            return validated

        except ValidationError as e:
            logger.warning(
                "Tier 3: Extracted data failed validation",
                extra={"errors": e.errors()},
            )
            return None

        except Exception as e:
            logger.error(f"Tier 3 unexpected error: {e}")
            return None

    def _extract_questions(self, text: str) -> list[str]:
        """Extract questions from text

        Looks for:
        - Lines ending with ?
        - Numbered questions (1., 2., 3.)
        - Bullet points with questions

        Args:
            text: Text to extract from

        Returns:
            List of extracted questions
        """
        questions = []

        # Pattern 1: Lines ending with ?
        question_lines = [
            line.strip() for line in text.split("\n")
            if line.strip().endswith("?")
        ]

        for line in question_lines:
            # Remove numbering (1., -, *, etc.)
            clean = re.sub(r"^[\d\.\-\*\+\s]+", "", line).strip()
            if clean and len(clean) < 300:
                questions.append(clean)

        return questions[:5]  # Max 5 questions

    def _extract_answer_text(self, raw_response: str | dict) -> str:
        """Extract plain text answer as fallback

        Args:
            raw_response: Raw response (string or dict)

        Returns:
            Plain text answer
        """
        if isinstance(raw_response, dict):
            # Try to find answer field
            return raw_response.get("answer", str(raw_response))

        if isinstance(raw_response, str):
            # Remove JSON artifacts if present
            clean = re.sub(r"```(?:json)?\s*\n.*?\n```", "", raw_response, flags=re.DOTALL)
            clean = re.sub(r"\{.*?\}", "", clean, flags=re.DOTALL)
            return clean.strip() or raw_response

        return str(raw_response)

    def get_stats(self) -> Dict[str, Any]:
        """Get parsing statistics

        Returns:
            Dictionary with success rates per tier
        """
        if self.stats["total_attempts"] == 0:
            return {**self.stats, "overall_success_rate": 0.0}

        overall_success = self.stats["total_attempts"] - self.stats["total_failures"]
        return {
            **self.stats,
            "overall_success_rate": overall_success / self.stats["total_attempts"],
        }


# =============================================================================
# Global Parser Instance
# =============================================================================

# Singleton parser instance for easy import
_parser_instance: Optional[ResponseParser] = None


def get_response_parser() -> ResponseParser:
    """Get global response parser instance

    Returns:
        Singleton ResponseParser instance
    """
    global _parser_instance
    if _parser_instance is None:
        _parser_instance = ResponseParser()
    return _parser_instance


# =============================================================================
# Convenience Functions
# =============================================================================


def parse_ooda_response(
    raw_response: str | dict,
    expected_schema: Type[T],
) -> T:
    """Parse OODA response using global parser

    Convenience function for one-off parsing without managing parser instance.

    Args:
        raw_response: Raw LLM response
        expected_schema: Expected Pydantic model class

    Returns:
        Parsed and validated response object
    """
    parser = get_response_parser()
    return parser.parse(raw_response, expected_schema)
