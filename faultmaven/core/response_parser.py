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
        """Initialize response parser with detailed statistics tracking"""
        self.stats = {
            "tier1_success": 0,                    # Function calling succeeded
            "tier2_direct_json": 0,                # Pure JSON parsed successfully
            "tier2_markdown_extraction": 0,        # Had to extract from markdown blocks
            "tier2_brace_extraction": 0,           # Had to extract from {...} pattern
            "tier3_success": 0,                    # Heuristic extraction succeeded
            "total_failures": 0,                   # All tiers failed
            "total_attempts": 0,                   # Total parse attempts
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
            logger.debug("Attempting Tier 1: Function Calling")
            result = self._tier1_function_calling(raw_response, expected_schema)
            if result:
                self.stats["tier1_success"] += 1
                logger.debug("Tier 1 (function calling) succeeded")
                return result
            logger.debug("Tier 1 failed, falling back to Tier 2")

        # Tier 2: JSON Parsing
        if isinstance(raw_response, str):
            logger.debug("Attempting Tier 2: JSON Parsing")
            result = self._tier2_json_parsing(raw_response, expected_schema)
            if result:
                # Stats already tracked by _tier2_json_parsing method
                logger.debug("Tier 2 (JSON parsing) succeeded")
                return result
            logger.debug("Tier 2 failed, falling back to Tier 3")

            # Tier 3: Heuristic Extraction
            logger.debug("Attempting Tier 3: Heuristic Extraction")
            result = self._tier3_heuristic_extraction(raw_response, expected_schema)
            if result:
                self.stats["tier3_success"] += 1
                logger.debug("Tier 3 (heuristic extraction) succeeded")
                return result
            logger.debug("Tier 3 failed, using minimal fallback")

        # Complete failure - use minimal fallback
        self.stats["total_failures"] += 1
        logger.warning(
            "All parsing tiers failed, using minimal fallback",
            extra={
                "raw_response_preview": str(raw_response)[:200],
                "raw_response_type": type(raw_response).__name__,
                "expected_schema": expected_schema.__name__
            },
        )

        # Extract answer text as best effort
        answer = self._extract_answer_text(raw_response)
        logger.warning(
            f"Minimal fallback created, answer_preview={answer[:100] if answer else 'EMPTY'}",
            extra={"answer_length": len(answer) if answer else 0}
        )
        return create_minimal_response(answer)  # type: ignore

    def _fix_double_encoding(self, validated: T, tier_name: str) -> T:
        """Fix double-encoded JSON in answer field (recursive)

        Sometimes LLMs put the entire response structure inside the answer field.
        This can happen multiple times (triple-encoding, etc.), so recurse until
        we get plain text.

        Args:
            validated: Validated response object
            tier_name: Name of tier for logging

        Returns:
            Fixed response object
        """
        if hasattr(validated, 'answer') and isinstance(validated.answer, str):
            max_iterations = 5  # Prevent infinite loop
            iteration = 0
            while iteration < max_iterations and validated.answer.strip().startswith('{'):
                try:
                    parsed_inner = json.loads(validated.answer)
                    if isinstance(parsed_inner, dict) and 'answer' in parsed_inner:
                        logger.warning(
                            f"{tier_name}: Detected double-encoded JSON in answer field (iteration {iteration + 1}) - extracting inner answer",
                            extra={
                                "outer_answer_preview": validated.answer[:100],
                                "inner_answer_preview": str(parsed_inner['answer'])[:100]
                            }
                        )
                        # Extract the actual answer from the inner JSON
                        if isinstance(parsed_inner['answer'], (dict, list)):
                            # Still nested - convert to JSON string to continue unwrapping
                            validated.answer = json.dumps(parsed_inner['answer'])
                            logger.error(
                                f"🐛 DOUBLE ENCODING BUG: answer field contained nested dict/list at iteration {iteration + 1}",
                                extra={"nested_content": str(parsed_inner['answer'])[:200]}
                            )
                        else:
                            # Plain string/value - extract it
                            validated.answer = str(parsed_inner['answer'])
                        iteration += 1
                    elif isinstance(parsed_inner, dict):
                        # JSON doesn't have 'answer' field - this is the entire response object!
                        # This is the BUG - extract a text representation instead of leaving JSON
                        logger.error(
                            f"🐛 CRITICAL BUG: Inner JSON has no 'answer' field - full response object in answer field!",
                            extra={
                                "parsed_inner_keys": list(parsed_inner.keys()),
                                "parsed_inner_preview": str(parsed_inner)[:200]
                            }
                        )
                        # Try to extract meaningful text from the dict
                        if 'clarifying_questions' in parsed_inner or 'suggested_actions' in parsed_inner:
                            # This is a ConsultantResponse/OODAResponse - should never be here!
                            error_msg = f"ERROR: Full response object found in answer field. Keys: {list(parsed_inner.keys())}"
                            validated.answer = error_msg
                        else:
                            # Unknown structure - convert to readable string
                            validated.answer = json.dumps(parsed_inner, indent=2)
                        break
                    else:
                        # Not a dict - unexpected
                        break
                except (json.JSONDecodeError, KeyError):
                    # Not double-encoded, keep as-is
                    break

        return validated

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

            # Check for double-encoding and fix if needed
            validated = self._fix_double_encoding(validated, "Tier 1")

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
        """Tier 2: Extract and parse JSON from text (with detailed tracking)

        LLMs often wrap JSON in markdown code blocks:
        ```json
        {"answer": "..."}
        ```

        This tier handles:
        - Pure JSON strings (tracks as tier2_direct_json)
        - JSON in markdown code blocks (tracks as tier2_markdown_extraction)
        - JSON with surrounding text (tracks as tier2_brace_extraction)

        Args:
            response_text: Raw text response
            expected_schema: Expected Pydantic model

        Returns:
            Validated response object or None if parsing fails
        """
        try:
            # Try direct JSON parsing first (IDEAL - LLM followed instructions)
            try:
                data = json.loads(response_text)
                validated = expected_schema(**data)
                # Check for double-encoding and fix if needed
                validated = self._fix_double_encoding(validated, "Tier 2 (direct)")
                self.stats["tier2_direct_json"] += 1
                logger.debug("Tier 2: Direct JSON parse succeeded (LLM returned clean JSON)")
                return validated
            except json.JSONDecodeError:
                pass

            # Extract JSON from markdown code blocks (DEFENSIVE - LLM wrapped in markdown)
            json_pattern = r"```(?:json)?\s*\n(.*?)\n```"
            matches = re.findall(json_pattern, response_text, re.DOTALL)

            for match in matches:
                try:
                    data = json.loads(match)
                    validated = expected_schema(**data)
                    # Check for double-encoding and fix if needed
                    validated = self._fix_double_encoding(validated, "Tier 2 (markdown)")
                    self.stats["tier2_markdown_extraction"] += 1
                    logger.warning(
                        "Tier 2: Markdown extraction used - LLM wrapped JSON in code blocks despite instructions",
                        extra={"preview": response_text[:100]}
                    )
                    return validated
                except (json.JSONDecodeError, ValidationError):
                    continue

            # Try finding JSON object in text (DEFENSIVE - JSON buried in text)
            brace_pattern = r"\{.*\}"
            matches = re.findall(brace_pattern, response_text, re.DOTALL)

            for match in matches:
                try:
                    data = json.loads(match)
                    validated = expected_schema(**data)
                    # Check for double-encoding and fix if needed
                    validated = self._fix_double_encoding(validated, "Tier 2 (brace)")
                    self.stats["tier2_brace_extraction"] += 1
                    logger.warning(
                        "Tier 2: Brace extraction used - LLM buried JSON in surrounding text",
                        extra={"preview": response_text[:100]}
                    )
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

            # Check for double-encoding and fix if needed
            validated = self._fix_double_encoding(validated, "Tier 3")

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
            # Try to find answer field (case-insensitive)
            for key in ["answer", "Answer", "content", "Content", "response", "Response"]:
                if key in raw_response and isinstance(raw_response[key], str):
                    # Check if the answer itself is JSON (double-encoding issue)
                    answer_value = raw_response[key]
                    try:
                        # If it's a JSON string, try to extract the actual answer from it
                        parsed_inner = json.loads(answer_value)
                        if isinstance(parsed_inner, dict) and "answer" in parsed_inner:
                            logger.warning(
                                "Detected double-encoded JSON in answer field - extracting inner answer",
                                extra={"outer_key": key, "inner_has_answer": True}
                            )
                            return str(parsed_inner["answer"])
                    except (json.JSONDecodeError, TypeError):
                        # Not JSON or can't parse - use as-is
                        pass
                    return answer_value
            # If no answer field found, return first string value
            for value in raw_response.values():
                if isinstance(value, str) and len(value) > 10:  # Skip short metadata
                    return value
            # Last resort: stringify the dict
            logger.warning(f"No answer field found in dict, keys: {list(raw_response.keys())}")
            return str(raw_response)

        if isinstance(raw_response, str):
            # Check if the string itself is JSON (single-encoded but not parsed yet)
            try:
                parsed = json.loads(raw_response)
                if isinstance(parsed, dict) and "answer" in parsed:
                    logger.warning(
                        "Detected unparsed JSON string in _extract_answer_text - extracting answer field",
                        extra={"preview": raw_response[:100]}
                    )
                    # Recursively call to handle potential double-encoding
                    return self._extract_answer_text(parsed)
            except (json.JSONDecodeError, TypeError):
                # Not JSON - treat as plain text
                pass

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
        # Compute tier2_success as sum of all tier2 variants for backward compatibility
        tier2_success = (
            self.stats["tier2_direct_json"]
            + self.stats["tier2_markdown_extraction"]
            + self.stats["tier2_brace_extraction"]
        )

        if self.stats["total_attempts"] == 0:
            return {
                **self.stats,
                "tier2_success": tier2_success,
                "overall_success_rate": 0.0,
            }

        overall_success = self.stats["total_attempts"] - self.stats["total_failures"]
        return {
            **self.stats,
            "tier2_success": tier2_success,
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
