"""Bounded choice matching for typed responses to offered suggestions.

When the agent presents COOPERATIVE suggestions with intent metadata (e.g.,
"Yes, mark as resolved" with confirmation intent), and the user types a
response instead of clicking, this module determines whether the typed text
is answering one of those choices or is unrelated conversational input.

Design: see docs/architecture/investigation-engine/intent-resolution.md
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Maximum message length to consider for choice matching.
# Longer messages are almost certainly conversational, not short answers.
MAX_MESSAGE_LENGTH = 200


class IntentResolver:
    """Resolves user intent by matching typed text against offered suggestions.

    Two-tier approach:
    1. Fast path: exact/near-exact match against suggestion payloads
    2. LLM path: bounded choice classification via CLASSIFIER_PROVIDER
    """

    def __init__(self, llm_router):
        self.llm_router = llm_router

    async def resolve(
        self,
        user_message: str,
        last_suggestions: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Match user message against last turn's suggestions.

        Args:
            user_message: What the user typed
            last_suggestions: Suggestions from the previous agent turn

        Returns:
            The matched suggestion's intent dict, or None if no match.
        """
        msg = user_message.strip()
        if not msg:
            return None

        # Guard: long messages are conversational, not choice answers
        if len(msg) > MAX_MESSAGE_LENGTH:
            return None

        # Filter to suggestions that carry intent metadata
        choices = [s for s in last_suggestions if s.get("intent")]
        if not choices:
            return None

        # Tier 1: exact/normalized match against payloads
        matched = self._exact_match(msg, choices)
        if matched is not None:
            logger.info(
                f"Intent resolved via exact match: {matched.get('type', 'unknown')}"
            )
            return matched

        # Tier 2: LLM classifier for semantic matching
        matched = await self._classify(msg, choices)
        if matched is not None:
            logger.info(
                f"Intent resolved via classifier: {matched.get('type', 'unknown')}"
            )
        return matched

    def _exact_match(
        self,
        user_message: str,
        choices: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Fast path: check if user typed something very close to a suggestion payload."""
        msg_lower = user_message.lower().strip().rstrip(".!?")

        for choice in choices:
            payload_lower = choice.get("payload", "").lower().strip().rstrip(".!?")
            if not payload_lower:
                continue

            # Exact match
            if msg_lower == payload_lower:
                return choice["intent"]

            # User typed the label instead of the payload
            label_lower = choice.get("label", "").lower().strip().rstrip(".!?")
            if label_lower and msg_lower == label_lower:
                return choice["intent"]

        return None

    async def _classify(
        self,
        user_message: str,
        choices: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """LLM-based bounded choice classification.

        Asks the classifier: "Is this message a response to one of these
        N specific choices, or something else?" Output is a single token.
        """
        prompt = self._build_prompt(user_message, choices)

        try:
            from faultmaven.config.settings import get_settings

            settings = get_settings()
            classifier_model = settings.get_classifier_model()

            response = await self.llm_router.route(
                messages=[{"role": "user", "content": prompt}],
                model=classifier_model,
                max_tokens=10,
                temperature=0.0,
            )

            return self._parse_response(response.content, choices)

        except Exception:
            # Classifier failure → default to no match (safe fallback)
            logger.warning(
                "Intent classifier failed, defaulting to conversation",
                exc_info=True,
            )
            return None

    def _build_prompt(
        self,
        user_message: str,
        choices: List[Dict[str, Any]],
    ) -> str:
        """Build the bounded choice classification prompt."""
        choice_lines = []
        for i, choice in enumerate(choices, 1):
            label = choice.get("label", "")
            body = choice.get("body", "")
            desc = f"{label} — {body}" if body else label
            choice_lines.append(f"{i}. {desc}")

        choices_text = "\n".join(choice_lines)

        return (
            "The assistant just offered these choices:\n\n"
            f"{choices_text}\n\n"
            f'The user typed: "{user_message}"\n\n'
            "Which choice (if any) is the user responding to?\n"
            'Answer with ONLY the choice number, or "none" if the message '
            "is unrelated or unclear. Do not explain."
        )

    def _parse_response(
        self,
        response_text: str,
        choices: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Parse classifier response into a matched intent or None."""
        text = response_text.strip().lower().rstrip(".").strip()

        # "none" → no match
        if text == "none":
            return None

        # Try to extract a number
        try:
            choice_num = int(text)
        except ValueError:
            # Response isn't a clean number — could be "1." or "choice 1"
            # Try to find a digit
            digits = [c for c in text if c.isdigit()]
            if len(digits) == 1:
                choice_num = int(digits[0])
            else:
                logger.debug(f"Classifier returned unparseable response: '{text}'")
                return None

        # Validate range
        if 1 <= choice_num <= len(choices):
            return choices[choice_num - 1].get("intent")

        logger.debug(
            f"Classifier returned out-of-range choice: {choice_num} "
            f"(have {len(choices)} choices)"
        )
        return None
