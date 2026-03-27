"""Inquiry Turn Handler

Implements the INQUIRY -> INVESTIGATING transition logic with iterative
problem statement refinement.

Reference: investigation-lifecycle-logic.md Section 1.2
"""

import logging
import re
from datetime import UTC, datetime

from faultmaven.modules.case.contracts import Case, CaseAction, CaseStatus

logger = logging.getLogger(__name__)


async def handle_inquiry_turn(case: Case, user_message: str, llm_provider) -> str:
    """
    Process inquiry turn and manage problem statement workflow.

    ITERATIVE REFINEMENT PATTERN (Section 1.7, line 773):
    1. Agent generates proposed_problem_statement from conversation
    2. Agent presents statement for confirmation
    3. User confirms OR provides corrections
    4. If corrections: Update proposed_problem_statement and repeat step 2
    5. If confirmed: Set problem_statement_confirmed = True

    Args:
        case: Current case in INQUIRY status
        user_message: User's message this turn
        llm_provider: LLM provider for generating problem statement

    Returns:
        Agent response (either confirmation request or transition notification)
    """
    if case.status != CaseStatus.INQUIRY:
        raise ValueError(
            f"Can only handle inquiry turns for INQUIRY status, got {case.status}"
        )

    # Check if user is providing corrections
    user_provides_corrections_flag = _user_provides_corrections(user_message)

    # Generate or update proposed_problem_statement
    if not case.inquiry.proposed_problem_statement or user_provides_corrections_flag:
        case.inquiry.proposed_problem_statement = await _llm_generate_problem_statement(
            llm_provider,
            conversation_history=case.messages,
            problem_confirmation=case.inquiry.problem_confirmation,
            user_corrections=(
                _extract_corrections(user_message)
                if user_provides_corrections_flag
                else None
            ),
        )

    # Check if user confirms statement
    if user_confirms(user_message):
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
        case.inquiry.decided_to_investigate = True
        case.inquiry.decision_made_at = datetime.now(UTC)

        # Now can_start_investigation returns True
        return await _transition_to_investigating(case)
    else:
        # Present statement for confirmation
        return f"""Based on our conversation, the problem is:

{case.inquiry.proposed_problem_statement}

Is this what you want me to investigate?

[✅ Yes]  [❌ No]

💡 Tip: Click a button or type to clarify"""


def user_confirms(user_message: str) -> bool:
    """
    Mechanical fallback: detect explicit confirmation phrases.

    This covers the easy cases (short, unambiguous replies). The LLM handles
    nuanced implicit confirmation — diagnostic questions, evidence submission,
    urgency signals — because those require conversational context.

    Uses word-boundary matching to avoid false positives (e.g., "started" ≠ "start").
    Only triggers on short messages (< 100 chars) — long messages are providing
    information, not confirming.
    """
    message_lower = user_message.lower().strip()

    # Long messages are providing information, not confirming
    if len(message_lower) > 100:
        return False

    # Unambiguous confirmation phrases only. Broad terms like "start",
    # "do it", "go ahead", "investigate" are excluded — they appear in
    # normal conversation (e.g., "where do I start").
    confirmation_phrases = [
        "yes",
        "correct",
        "that's right",
        "proceed",
        "let's investigate",
        "yes, investigate",
        "looks good",
        "sounds good",
        "go ahead",
    ]

    return any(
        re.search(r"\b" + re.escape(phrase) + r"\b", message_lower)
        for phrase in confirmation_phrases
    )


def _user_provides_corrections(user_message: str) -> bool:
    """
    Check if user is providing corrections to the problem statement.

    Correction signals:
    - "No", "Not quite", "Actually"
    - "It's more like...", "I meant..."
    - Contains contradictions or clarifications
    """
    message_lower = user_message.lower().strip()

    correction_phrases = [
        "no",
        "not quite",
        "actually",
        "it's more",
        "its more",
        "i meant",
        "what i meant",
        "more specifically",
        "to clarify",
    ]

    return any(phrase in message_lower for phrase in correction_phrases)


def _extract_corrections(user_message: str) -> str:
    """
    Extract the correction/clarification from user message.

    Currently returns the full message, as the LLM will identify
    the relevant corrections when regenerating the statement.
    """
    return user_message


async def _llm_generate_problem_statement(
    llm_provider,
    conversation_history: list,
    problem_confirmation: any | None,
    user_corrections: str | None,
) -> str:
    """
    Generate proposed problem statement from conversation history.

    Uses LLM to synthesize:
    - User's initial problem description
    - Agent's follow-up questions and responses
    - User corrections (if provided)

    Args:
        llm_provider: LLM provider
        conversation_history: Full conversation so far
        problem_confirmation: Agent's preliminary understanding
        user_corrections: User's corrections to previous statement (optional)

    Returns:
        Proposed problem statement string
    """
    # Build context for LLM
    context_parts = ["Generate a concise problem statement from this conversation:\n"]

    # Add conversation history
    for msg in conversation_history[-5:]:  # Last 5 messages for context
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        context_parts.append(f"{role.upper()}: {content}\n")

    # Add user corrections if provided
    if user_corrections:
        context_parts.append(f"\nUser's corrections: {user_corrections}\n")

    context_parts.append("\nGenerate a 1-2 sentence problem statement capturing:")
    context_parts.append("- What is broken/degraded")
    context_parts.append("- Impact (users/systems affected)")
    context_parts.append("- Timeline (when it started)")
    context_parts.append("\nProblem statement:")

    prompt = "\n".join(context_parts)

    # Call LLM (simple completion, not structured output)
    response = await llm_provider.generate_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.3,
    )

    problem_statement = response.content.strip()

    logger.info(f"Generated problem statement: {problem_statement}")

    return problem_statement


async def _transition_to_investigating(case: Case) -> str:
    """
    Execute transition from INQUIRY -> INVESTIGATING.

    Creates ProblemVerification from confirmed problem statement.
    Updates status and records transition.

    Args:
        case: Case with confirmed problem statement

    Returns:
        Agent response confirming transition
    """
    if not case.inquiry.proposed_problem_statement:
        raise ValueError("Cannot transition without confirmed problem statement")

    # Create ProblemVerification with confirmed statement
    from faultmaven.modules.case.contracts import ProblemVerification

    case.problem_verification = ProblemVerification(
        symptom_statement=case.inquiry.proposed_problem_statement
        # LLM will fill other fields during investigation
    )

    # Update status
    old_status = case.status
    case.status = CaseStatus.INVESTIGATING
    case.status_transitioned_at = datetime.now(UTC)

    # Record case action
    case.action_history.append(
        CaseAction(
            from_status=old_status,
            to_status=CaseStatus.INVESTIGATING,
            triggered_at=datetime.now(UTC),
            triggered_by="system",
            reason="User confirmed problem and decided to investigate",
        )
    )

    logger.info(f"Transitioned case {case.case_id} from {old_status} -> INVESTIGATING")

    return f"""✅ Investigation started!

Problem: {case.problem_verification.symptom_statement}

Let's gather evidence to understand what's happening. What data can you share?
- Logs or error messages
- Metrics or dashboards
- Timeline of recent changes
- Affected systems or users"""
