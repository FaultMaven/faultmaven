"""Intake Agent - Phase 0: Problem Identification

Responsibilities:
- Determine if user has an active problem vs informational query
- Capture initial problem statement
- Detect urgency level
- Offer adaptive guidance (troubleshoot vs learn vs explore)

Context Size: ~400 tokens (vs 1300 in monolithic)
Key Optimizations:
- No hypothesis/timeline info needed
- No technical details yet
- Focus on: Is there a problem? What is it?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState, UrgencyLevel
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Compact prompt (~300 tokens vs 1300)
INTAKE_PROMPT = """You are FaultMaven's intake specialist. Identify if user has a technical problem.

GOAL: Determine problem status and capture initial statement.

CURRENT STATE:
{phase_state}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

URGENCY: {urgency_level}

DECISION TREE:
1. Problem signals: "error", "not working", "failed", "down", "broken"
   → has_active_problem=true, capture problem_statement

2. No problem signals + informational: "how to", "what is", "explain"
   → has_active_problem=false, answer question, offer help

3. Unclear intent:
   → Ask clarifying question

URGENCY DETECTION:
- CRITICAL: "production down", "outage", "data loss", "emergency"
- HIGH: "urgent", "asap", "impacting users", "broken"
- NORMAL: routine questions

RESPONSE FORMAT (JSON):
{{
  "answer": "Natural response to user",
  "has_active_problem": true/false,
  "problem_statement": "One-sentence problem summary" or "",
  "urgency_level": "normal"|"high"|"critical",
  "suggested_actions": [
    {{"label": "I have a problem", "type": "question_template", "payload": "I need help troubleshooting"}},
    {{"label": "Just learning", "type": "question_template", "payload": "Explain this concept"}},
    {{"label": "Need best practices", "type": "question_template", "payload": "What are best practices for X"}}
  ],
  "phase_complete": true/false,
  "confidence": 0.0-1.0
}}

CRITICAL:
- DON'T mention phases or methodology
- DON'T assume every query is a problem
- DO answer user's question first
- DO be conversational
"""


class IntakeAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 0: Problem Intake.

    Ultra-focused: Just determine problem status and urgency.
    No need for hypotheses, timelines, or technical details yet.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=0,
            phase_name="Intake",
            prompt_template=INTAKE_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Intake only needs minimal state."""
        return {
            "has_active_problem": full_state.has_active_problem,
            "problem_statement": full_state.problem_statement or "None yet",
            "current_phase": 0
        }

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Process intake assessment."""

        # Build compact prompt
        prompt = self.build_prompt(context)

        # Call LLM (much smaller context = faster + cheaper)
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.7,
            max_tokens=800  # Smaller than full agent (1500)
        )

        # Parse JSON response
        try:
            # Extract JSON from response
            response_text = llm_response.content
            # Try to find JSON in response
            if "```json" in response_text:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                json_str = response_text[json_start:json_end]
            elif response_text.strip().startswith("{"):
                json_str = response_text.strip()
            else:
                # Fallback: try to parse entire response
                json_str = response_text

            parsed = json.loads(json_str)

            # Build state updates (delta only)
            state_updates = {
                "has_active_problem": parsed.get("has_active_problem", False),
                "problem_statement": parsed.get("problem_statement", ""),
                "urgency_level": parsed.get("urgency_level", "normal"),
                "current_phase": 1 if parsed.get("has_active_problem") and parsed.get("phase_complete") else 0
            }

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=parsed.get("suggested_actions", []),
                suggested_commands=[],  # No commands in intake
                phase_complete=parsed.get("phase_complete", False),
                confidence=parsed.get("confidence", 0.85),
                recommended_next_phase=1 if parsed.get("has_active_problem") else 0
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Use heuristics
            has_problem = any(
                keyword in context.user_query.lower()
                for keyword in ["error", "not working", "failed", "broken", "down", "crash"]
            )

            is_critical = any(
                keyword in context.user_query.lower()
                for keyword in ["production", "outage", "emergency", "critical", "urgent"]
            )

            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "has_active_problem": has_problem,
                    "problem_statement": context.user_query if has_problem else "",
                    "urgency_level": "critical" if is_critical else ("high" if has_problem else "normal"),
                    "current_phase": 1 if has_problem else 0
                },
                suggested_actions=[
                    {"label": "I have a problem", "type": "question_template", "payload": "Help me troubleshoot"},
                    {"label": "Just learning", "type": "question_template", "payload": "Explain this"}
                ],
                suggested_commands=[],
                phase_complete=has_problem,  # Complete if problem detected
                confidence=0.70,  # Lower confidence for heuristic
                recommended_next_phase=1 if has_problem else 0
            )

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Advance to Blast Radius (Phase 1) if:
        - Active problem detected
        - Problem statement captured
        - Not an informational query
        """
        state = response.state_updates

        # Must have active problem to advance
        if not state.get("has_active_problem"):
            return False

        # Must have problem statement
        if not state.get("problem_statement"):
            return False

        # Advance!
        return True
