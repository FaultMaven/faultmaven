"""
Evidence Classification Service

Implements 5-dimensional LLM-based classification of user input to determine:
1. REQUEST MATCHING: Which evidence requests this addresses (0 to N)
2. COMPLETENESS: How complete the evidence is (partial/complete/over_complete)
3. FORM: user_input vs document
4. EVIDENCE TYPE: supportive/refuting/neutral/absence
5. USER INTENT: providing_evidence/asking_question/reporting_unavailable/etc.

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import json
import logging
from typing import List, Optional

from faultmaven.models.evidence import (
    EvidenceRequest,
    EvidenceClassification,
    CompletenessLevel,
    EvidenceForm,
    EvidenceType,
    UserIntent,
)
from faultmaven.infrastructure.llm.router import LLMRouter

logger = logging.getLogger(__name__)


CLASSIFICATION_PROMPT_TEMPLATE = """You are an expert evidence analyst for technical troubleshooting investigations.

TASK: Classify the following user input across 5 dimensions.

USER INPUT:
{user_input}

ACTIVE EVIDENCE REQUESTS:
{evidence_requests_json}

CONVERSATION CONTEXT (last 3 messages):
{conversation_context}

CLASSIFY across 5 dimensions:

1. REQUEST MATCHING
   Which request IDs does this address? (can be multiple, or none)
   Consider semantic similarity, not just keywords.

2. COMPLETENESS
   - partial (0.3-0.7): Some information, but incomplete
   - complete (0.8-1.0): Fully answers this specific request

   NOTE: "over_complete" means the user provided evidence that satisfies
   MULTIPLE evidence requests simultaneously. This is reflected by the
   matched_request_ids list containing >1 request, NOT by a score >1.0.
   Each request maintains its own 0.0-1.0 completeness score.

3. FORM
   - user_input: Text entered by user
   - document: File upload

4. EVIDENCE TYPE
   - supportive: Confirms/supports investigation direction
   - refuting: Contradicts hypothesis or expectation
   - neutral: Doesn't clearly support or contradict
   - absence: User checked but evidence doesn't exist

5. USER INTENT
   - providing_evidence: User answering evidence request
   - asking_question: User asking for clarification/info
   - reporting_unavailable: User cannot provide evidence
   - reporting_status: Update on progress ("working on it")
   - clarifying: User asking what we mean
   - off_topic: Unrelated to investigation

RESPONSE FORMAT (JSON only, no markdown):
{{
    "matched_request_ids": ["req-001", "req-002"],
    "completeness": "complete",
    "completeness_score": 0.9,
    "form": "user_input",
    "evidence_type": "supportive",
    "user_intent": "providing_evidence",
    "rationale": "Brief explanation of classification",
    "follow_up_needed": null
}}

IMPORTANT:
- matched_request_ids can be empty [] if input doesn't address any request
- completeness_score must be between 0.0 and 1.0
- If multiple requests matched, determine if completeness = "over_complete"
- Respond ONLY with valid JSON, no explanation text
"""


async def classify_evidence_multidimensional(
    user_input: str,
    active_requests: List[EvidenceRequest],
    conversation_history: List[str],
    llm_router: LLMRouter,
    form: EvidenceForm = EvidenceForm.USER_INPUT
) -> EvidenceClassification:
    """
    Classify user input across 5 dimensions using LLM.

    Args:
        user_input: Text submitted by user
        active_requests: List of active evidence requests (PENDING or PARTIAL status)
        conversation_history: Last 3 messages for context
        llm_router: LLM client for classification
        form: EvidenceForm (user_input or document)

    Returns:
        EvidenceClassification with all 5 dimensions classified

    Raises:
        ValueError: If LLM returns invalid classification
    """
    try:
        # Prepare evidence requests for LLM
        evidence_requests_json = json.dumps(
            [
                {
                    "request_id": req.request_id,
                    "label": req.label,
                    "description": req.description,
                    "category": req.category.value,
                    "status": req.status.value,
                    "completeness": req.completeness
                }
                for req in active_requests
            ],
            indent=2
        )

        # Prepare conversation context
        conversation_context = "\n".join(
            f"- {msg}" for msg in conversation_history[-3:]
        ) if conversation_history else "No prior context"

        # Build classification prompt
        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
            user_input=user_input,
            evidence_requests_json=evidence_requests_json,
            conversation_context=conversation_context
        )

        # Call LLM for classification (use fast model)
        response = await llm_router.agenerate(
            prompt=prompt,
            model_name="gpt-4o-mini",  # Fast classification model
            temperature=0.2,  # Low temperature for consistent classification
            max_tokens=500
        )

        # Parse LLM response
        try:
            # Clean response (remove markdown code blocks if present)
            cleaned_response = response.strip()
            if cleaned_response.startswith("```"):
                # Extract JSON from markdown code block
                lines = cleaned_response.split("\n")
                json_lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned_response = "\n".join(json_lines)

            classification_dict = json.loads(cleaned_response)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse classification response: {response}")
            # Fallback classification
            return _create_fallback_classification(user_input, active_requests, form)

        # Validate required fields
        required_fields = [
            "matched_request_ids",
            "completeness",
            "completeness_score",
            "form",
            "evidence_type",
            "user_intent"
        ]
        for field in required_fields:
            if field not in classification_dict:
                logger.warning(f"Missing field '{field}' in classification, using fallback")
                return _create_fallback_classification(user_input, active_requests, form)

        # Determine completeness level
        completeness_score = float(classification_dict["completeness_score"])
        matched_count = len(classification_dict["matched_request_ids"])

        if matched_count > 1:
            completeness = CompletenessLevel.OVER_COMPLETE
        elif completeness_score >= 0.8:
            completeness = CompletenessLevel.COMPLETE
        elif completeness_score >= 0.3:
            completeness = CompletenessLevel.PARTIAL
        else:
            # Score too low, likely not evidence
            completeness = CompletenessLevel.PARTIAL
            completeness_score = max(0.0, completeness_score)

        # Create EvidenceClassification
        return EvidenceClassification(
            matched_request_ids=classification_dict["matched_request_ids"],
            completeness=completeness,
            completeness_score=min(1.0, max(0.0, completeness_score)),  # Clamp 0-1
            form=EvidenceForm(form),  # Use provided form (handles documents)
            evidence_type=EvidenceType(classification_dict["evidence_type"]),
            user_intent=UserIntent(classification_dict["user_intent"]),
            rationale=classification_dict.get("rationale"),
            follow_up_needed=classification_dict.get("follow_up_needed")
        )

    except Exception as e:
        logger.error(f"Error in evidence classification: {e}", exc_info=True)
        return _create_fallback_classification(user_input, active_requests, form)


def _create_fallback_classification(
    user_input: str,
    active_requests: List[EvidenceRequest],
    form: EvidenceForm
) -> EvidenceClassification:
    """
    Create a safe fallback classification when LLM fails.

    Strategy:
    - Assumes user is providing evidence (optimistic)
    - Marks as PARTIAL (safe assumption)
    - Attempts simple keyword matching
    """
    logger.warning("Using fallback classification due to LLM failure")

    # Simple keyword matching for request IDs
    matched_ids = []
    user_input_lower = user_input.lower()

    for req in active_requests:
        # Check if any keywords from label/description appear in user input
        label_words = set(req.label.lower().split())
        desc_words = set(req.description.lower().split())
        input_words = set(user_input_lower.split())

        # If significant overlap (>= 2 words), consider it a match
        overlap = (label_words | desc_words) & input_words
        if len(overlap) >= 2:
            matched_ids.append(req.request_id)

    # Determine completeness level
    if len(matched_ids) > 1:
        completeness = CompletenessLevel.OVER_COMPLETE
        completeness_score = 0.5  # Conservative score
    elif len(matched_ids) == 1:
        completeness = CompletenessLevel.PARTIAL
        completeness_score = 0.5
    else:
        # No matches - likely asking a question
        completeness = CompletenessLevel.PARTIAL
        completeness_score = 0.0

    # Detect user intent from keywords
    if any(word in user_input_lower for word in ["can't", "cannot", "don't have", "no access"]):
        user_intent = UserIntent.REPORTING_UNAVAILABLE
    elif any(word in user_input_lower for word in ["?", "what", "how", "why", "when", "where"]):
        user_intent = UserIntent.ASKING_QUESTION
    else:
        user_intent = UserIntent.PROVIDING_EVIDENCE

    return EvidenceClassification(
        matched_request_ids=matched_ids,
        completeness=completeness,
        completeness_score=completeness_score,
        form=form,
        evidence_type=EvidenceType.NEUTRAL,  # Safe default
        user_intent=user_intent,
        rationale="Fallback classification due to LLM error",
        follow_up_needed="Please be more specific about which evidence you're providing"
    )


def validate_classification(classification: EvidenceClassification) -> bool:
    """
    Validate that classification is logically consistent.

    Returns:
        True if valid, False otherwise
    """
    # Completeness score must match completeness level
    if classification.completeness == CompletenessLevel.COMPLETE:
        if classification.completeness_score < 0.8:
            logger.warning(
                f"Inconsistent classification: completeness=COMPLETE but score={classification.completeness_score}"
            )
            return False

    if classification.completeness == CompletenessLevel.PARTIAL:
        if classification.completeness_score >= 0.8:
            logger.warning(
                f"Inconsistent classification: completeness=PARTIAL but score={classification.completeness_score}"
            )
            return False

    # OVER_COMPLETE requires multiple matched requests
    if classification.completeness == CompletenessLevel.OVER_COMPLETE:
        if len(classification.matched_request_ids) < 2:
            logger.warning(
                f"Inconsistent classification: completeness=OVER_COMPLETE but only {len(classification.matched_request_ids)} requests matched"
            )
            return False

    # If user is reporting unavailable, should have matched requests
    if classification.user_intent == UserIntent.REPORTING_UNAVAILABLE:
        if len(classification.matched_request_ids) == 0:
            logger.warning(
                "User reporting unavailable but no requests matched"
            )
            # This is actually OK - user might be proactively saying they can't access something

    return True
