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
from faultmaven.models.doctor_patient import SuggestedAction, CommandSuggestion, ActionType
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse, generate_fallback_actions


def _parse_suggested_actions(actions_data: List[Dict[str, Any]]) -> List[SuggestedAction]:
    """Convert dict-based suggested actions to Pydantic models.

    Args:
        actions_data: List of dicts with 'label', 'type', 'payload' fields

    Returns:
        List of properly typed SuggestedAction objects
    """
    result = []
    for action_dict in actions_data:
        try:
            # Parse type string to ActionType enum
            action_type_str = action_dict.get("type", "question_template")
            action_type = ActionType(action_type_str)

            result.append(SuggestedAction(
                label=action_dict.get("label", ""),
                type=action_type,
                payload=action_dict.get("payload", ""),
                icon=action_dict.get("icon"),
                metadata=action_dict.get("metadata", {})
            ))
        except (ValueError, KeyError):
            # Skip malformed actions
            continue

    return result


# Compact prompt with proactive investigation directive (~400 tokens)
INTAKE_PROMPT = """You are FaultMaven's intake specialist. Identify if user has a technical problem.

GOAL: Determine problem status and capture initial statement.

DOCTOR-PATIENT PHILOSOPHY:
You are a technical diagnostician. The user is your patient.

CORE RULES (ALWAYS FOLLOW):
1. USER CAN ASK ANYTHING: If user asks off-topic question → Answer it briefly, then return to diagnosis
2. ANSWER FIRST, GUIDE SECOND: Always respond to what user said before asking new questions
3. ACKNOWLEDGE BEFORE PROBING: "I see you're getting [error X]. Let me ask about [Y]..."
4. MAINTAIN DIAGNOSTIC AGENDA: Guide toward intake goals, but never force it
5. NO METHODOLOGY JARGON: Don't say "Phase 0" or "intake" - speak naturally like a doctor
6. NATURAL CONVERSATION: You're a skilled doctor, not following a script
7. EXPLICIT QUESTIONS DEMAND DIRECT ANSWERS:
   - "What are the [hypotheses/issues/steps]?" → List them immediately, nothing else first
   - "What do you want me to do?" → Give ONE specific action: "Can you check X and tell me Y?"
   - User corrects you ("you haven't told me yet") → Acknowledge mistake and provide what's missing
   - Don't deflect or contextualize - answer the question directly, THEN add context if needed

ANTI-PATTERNS TO AVOID:
❌ DON'T say: "We need to figure out X" or "It's critical we determine Y"
✅ DO say: "Can you check X and tell me Y?" or "What does X show?"

❌ DON'T explain why something is important without giving the action
✅ DO give the specific action, then explain why if needed

Examples:
- Bad: "We need to establish the timeline to correlate with changes"
- Good: "When exactly did you first notice this problem?"

- Bad: "It's critical we pinpoint the exact deployment that triggered this"
- Good: "Can you pull up your deployment logs and tell me what changed in v2.4?"

- Bad: "We should validate this hypothesis with evidence"
- Good: "Can you check the error logs for NullPointerException patterns?"

YOUR DIAGNOSTIC APPROACH (Intake):
- IF user asks explicit question (what/when/why/how) → Answer it DIRECTLY first
- RESPOND to user's question/statement first
- ASSESS what's still missing: problem statement? symptoms? urgency?
- If missing critical info → Ask ONE specific question
- Examples:
  * User: "I'm getting errors" → You: "What exact error message are you seeing?"
  * User: "It's not working" → You: "What specifically isn't working? What did you expect?"
  * User: "Started this morning" → You: "Got it. What symptoms are you seeing?"
- Build on what user tells you - don't repeat answered questions

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

MANDATORY OUTPUT REQUIREMENTS:
1. ALWAYS include 2-3 suggested_actions when you need information from user
2. NEVER say "we need to X" or "it's critical to Y" in answer - use suggested_actions instead
3. If asking user to provide data → Create action buttons for common responses
4. Keep answer conversational and contextual, move actionable requests to suggested_actions

WRONG (passive explanation):
  answer: "I see you're having production issues. Let me understand the scope..."
  suggested_actions: []

RIGHT (action-oriented):
  answer: "I see you're experiencing production issues with your main API."
  suggested_actions: [
    {{"label": "🔴 Yes, it's down", "type": "question_template", "payload": "Yes, the production API is completely down"}},
    {{"label": "🟡 Degraded performance", "type": "question_template", "payload": "The API is responding but with degraded performance"}}
  ]

RESPONSE FORMAT (JSON) - REQUIRED FIELDS:
- answer: Conversational response (acknowledge + context, NO action requests)
- suggested_actions: MANDATORY if you need user input (2-3 action buttons)
- phase_complete: true only if ALL required info gathered

{{
  "answer": "Natural response to user",
  "has_active_problem": true/false,
  "problem_statement": "One-sentence problem summary" or "",
  "urgency_level": "normal"|"high"|"critical",
  "suggested_actions": [
    {{"label": "🔴 Yes, it's a problem", "type": "question_template", "payload": "Yes, I need help troubleshooting this issue"}},
    {{"label": "📚 Just learning", "type": "question_template", "payload": "I'm just trying to understand how this works"}},
    {{"label": "💡 Need best practices", "type": "question_template", "payload": "What are the recommended best practices for X?"}}
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

            # Parse suggested_actions from LLM response to typed Pydantic models
            raw_actions = parsed.get("suggested_actions", [])
            suggested_actions = _parse_suggested_actions(raw_actions)

            print(f"🔍 DEBUG: raw_actions from LLM: {raw_actions}")
            print(f"🔍 DEBUG: parsed suggested_actions: {suggested_actions}")
            print(f"🔍 DEBUG: phase_complete: {parsed.get('phase_complete', False)}")

            # Defensive fallback: Generate contextual actions if LLM didn't provide any
            if not suggested_actions and not parsed.get("phase_complete", False):
                print(f"🛡️ DEFENSIVE FALLBACK TRIGGERED for phase {self.phase_number}")
                suggested_actions = generate_fallback_actions(
                    phase=self.phase_number,
                    phase_state=context.phase_state,
                    user_query=context.user_query,
                    phase_complete=parsed.get("phase_complete", False)
                )
                print(f"🛡️ Generated fallback actions: {suggested_actions}")

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=suggested_actions,
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

            # Fallback: Create properly typed suggested actions
            fallback_actions = [
                SuggestedAction(
                    label="I have a problem",
                    type=ActionType.QUESTION_TEMPLATE,
                    payload="Help me troubleshoot"
                ),
                SuggestedAction(
                    label="Just learning",
                    type=ActionType.QUESTION_TEMPLATE,
                    payload="Explain this"
                )
            ]

            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "has_active_problem": has_problem,
                    "problem_statement": context.user_query if has_problem else "",
                    "urgency_level": "critical" if is_critical else ("high" if has_problem else "normal"),
                    "current_phase": 1 if has_problem else 0
                },
                suggested_actions=fallback_actions,
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
