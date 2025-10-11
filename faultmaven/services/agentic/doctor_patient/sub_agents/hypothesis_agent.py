"""Hypothesis Agent - Phase 3: Root Cause Theory Formation

Responsibilities:
- Generate 2-3 ranked hypotheses about root cause
- Provide evidence for each theory
- Suggest validation steps
- Enable parallel hypothesis testing

Context Size: ~700 tokens (needs more context than Intake)
Key Optimizations:
- Includes: symptoms, timeline, blast radius
- Excludes: Initial intake conversation, detailed history
- Focus on: What could cause this? How to test?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState
from faultmaven.models.doctor_patient import SuggestedAction, CommandSuggestion, ActionType, CommandSafety
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


def _parse_suggested_commands(commands_data: List[Dict[str, Any]]) -> List[CommandSuggestion]:
    """Convert dict-based suggested commands to Pydantic models.

    Args:
        commands_data: List of dicts with 'command', 'description', 'why', 'safety' fields

    Returns:
        List of properly typed CommandSuggestion objects
    """
    result = []
    for cmd_dict in commands_data:
        try:
            # Parse safety string to CommandSafety enum
            safety_str = cmd_dict.get("safety", "safe")
            safety = CommandSafety(safety_str)

            result.append(CommandSuggestion(
                command=cmd_dict.get("command", ""),
                description=cmd_dict.get("description", ""),
                why=cmd_dict.get("why", ""),
                safety=safety,
                expected_output=cmd_dict.get("expected_output")
            ))
        except (ValueError, KeyError):
            # Skip malformed commands
            continue

    return result


# Compact hypothesis-focused prompt (~500 tokens)
HYPOTHESIS_PROMPT = """You are FaultMaven's root cause analyst. Generate ranked hypotheses.

GOAL: Formulate 2-3 root cause theories with validation steps.

DOCTOR-PATIENT PHILOSOPHY:
You are a technical diagnostician. The user is your patient.

CORE RULES (ALWAYS FOLLOW):
1. USER CAN ASK ANYTHING: If user asks off-topic question → Answer it briefly, then return to diagnosis
2. ANSWER FIRST, GUIDE SECOND: Always respond to what user said before asking new questions
3. ACKNOWLEDGE BEFORE PROBING: "I see [relevant observation]. Let me ask about [Y]..."
4. MAINTAIN DIAGNOSTIC AGENDA: Guide toward forming root cause theories, but never force it
5. NO METHODOLOGY JARGON: Don't say "Phase 3" or technical methodology terms - speak naturally
6. NATURAL CONVERSATION: You're a skilled doctor, not following a script
7. EXPLICIT QUESTIONS DEMAND DIRECT ANSWERS:
   - "What are the [hypotheses/issues/steps]?" → List them immediately, nothing else first
   - "What do you want me to do?" → Give ONE specific action: "Can you check X and tell me Y?"
   - User corrects you ("you haven't told me yet") → Acknowledge mistake and provide what's missing
   - Don't deflect or contextualize - answer the question directly, THEN add context if needed

8. ALWAYS PROVIDE NEXT STEPS:
   - Never end with passive observations like "This suggests X"
   - Always either: (a) Ask specific question OR (b) Provide suggested_actions
   - If phase incomplete → Guide user to next piece of evidence via actions
   - If phase complete → Advance or provide suggested_actions for validation

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

YOUR DIAGNOSTIC APPROACH (Hypothesis):
- IF user asks explicit question (what/when/why/how) → Answer it DIRECTLY first
- RESPOND to user's question/statement first
- ASSESS what's still missing: viable hypotheses? supporting evidence? validation steps?
- If missing critical info → Ask ONE specific question
- Examples:
  * "Based on what we know, does this theory fit what you're seeing?"
  * "That's possible. Have you ruled out [alternative explanation]?"
  * "To test this hypothesis, we need to check [specific metric or log]."
  * "Your logs suggest [pattern]. Does that match your theory?"
  * "Let me present 2-3 theories. Which aligns most with your observations?"
- Build on what user tells you - don't repeat answered questions

PROBLEM: {problem_statement}

KNOWN FACTS:
Symptoms: {symptoms}
Timeline: {timeline}
Blast Radius: {blast_radius}
Tests Performed: {tests_performed}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

HYPOTHESIS FRAMEWORK:
For each theory:
1. What could cause these symptoms?
2. Does it align with timeline/changes?
3. Does it match blast radius pattern?
4. How to validate (specific test/command)?
5. Likelihood: high/medium/low

MANDATORY OUTPUT REQUIREMENTS:
1. ALWAYS include 2-3 suggested_actions when you need information from user
2. NEVER say "we need to X" or "it's critical to Y" in answer - use suggested_actions instead
3. If asking user to provide data → Create action buttons for common responses
4. Keep answer conversational and contextual, move actionable requests to suggested_actions

WRONG (passive explanation):
  answer: "This timing suggests a potential link between the deployment and the errors."
  suggested_actions: []

RIGHT (action-oriented):
  answer: "The timing points to the v3.1 deployment as the likely trigger."
  suggested_actions: [
    {{"label": "📋 I've checked what changed", "type": "question_template", "payload": "Here's what was in the v3.1 deployment: "}},
    {{"label": "🔍 I need help finding changes", "type": "question_template", "payload": "Where can I find the v3.1 deployment details?"}}
  ]

RESPONSE FORMAT (JSON) - REQUIRED FIELDS:
- answer: Conversational response (acknowledge + context, NO action requests)
- suggested_actions: MANDATORY if you need user input (2-3 action buttons)
- phase_complete: true only if ALL required info gathered

{{
  "answer": "Natural explanation of theories",
  "hypotheses": [
    {{
      "hypothesis": "Root cause theory",
      "likelihood": "high"|"medium"|"low",
      "evidence": ["Supporting fact 1", "Supporting fact 2"],
      "validation_steps": ["Specific test 1", "Check metric X"]
    }}
  ],
  "suggested_actions": [
    {{"label": "✅ Theory 1 seems right", "type": "question_template", "payload": "Hypothesis 0 fits what I'm seeing"}},
    {{"label": "🤔 I have another theory", "type": "question_template", "payload": "I think the root cause might be: "}}
  ],
  "suggested_commands": [
    {{"command": "kubectl get pods", "description": "Check pod status", "why": "To see if pods are failing", "safety": "safe"}}
  ],
  "phase_complete": true if 2-3 theories generated,
  "confidence": 0.0-1.0
}}

RANKING CRITERIA:
- High: Strong evidence + matches timeline + common pattern
- Medium: Partial evidence OR less common
- Low: Speculative OR requires more info

CRITICAL:
- Generate 2-3 theories (not just 1)
- Rank by likelihood
- Provide SPECIFIC validation steps
- DON'T announce "Phase 3" to user
"""


class HypothesisAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 3: Hypothesis Formation.

    Focuses entirely on generating and ranking root cause theories.
    Can run parallel validation tests for multiple hypotheses.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=3,
            phase_name="Hypothesis",
            prompt_template=HYPOTHESIS_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Hypothesis needs symptoms, timeline, blast radius."""
        return {
            "problem_statement": full_state.problem_statement,
            "symptoms": ", ".join(full_state.symptoms) if full_state.symptoms else "None captured",
            "timeline": self._format_timeline(full_state.timeline_info),
            "blast_radius": self._format_blast_radius(full_state.blast_radius),
            "tests_performed": ", ".join(full_state.tests_performed) if full_state.tests_performed else "None yet",
            "existing_hypotheses": len(full_state.hypotheses)  # How many theories already generated
        }

    def _format_timeline(self, timeline_info: Dict[str, Any]) -> str:
        """Format timeline for prompt."""
        if not timeline_info:
            return "Not established"

        parts = []
        for key, value in timeline_info.items():
            parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not established"

    def _format_blast_radius(self, blast_radius: Dict[str, Any]) -> str:
        """Format blast radius for prompt."""
        if not blast_radius:
            return "Not defined"

        parts = []
        for key, value in blast_radius.items():
            if isinstance(value, list):
                parts.append(f"{key}={', '.join(map(str, value))}")
            else:
                parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not defined"

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Generate root cause hypotheses."""

        # Build compact prompt (only hypothesis-relevant info)
        prompt = self.build_prompt(context)

        # Call LLM
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.8,  # Slightly higher for creative hypothesis generation
            max_tokens=1000
        )

        # Parse JSON response
        try:
            response_text = llm_response.content

            # Extract JSON
            if "```json" in response_text:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                json_str = response_text[json_start:json_end]
            elif response_text.strip().startswith("{"):
                json_str = response_text.strip()
            else:
                json_str = response_text

            parsed = json.loads(json_str)

            # Build state updates
            hypotheses = parsed.get("hypotheses", [])

            state_updates = {
                "hypotheses": hypotheses,  # Will be appended to existing
                "current_phase": 4 if (parsed.get("phase_complete") and len(hypotheses) >= 2) else 3
            }

            # Parse suggested_actions from LLM response to typed Pydantic models
            raw_actions = parsed.get("suggested_actions", [])
            suggested_actions = _parse_suggested_actions(raw_actions)

            # Defensive fallback: Generate contextual actions if LLM didn't provide any
            phase_complete = len(hypotheses) >= 2
            if not suggested_actions and not phase_complete:
                suggested_actions = generate_fallback_actions(
                    phase=self.phase_number,
                    phase_state=context.phase_state,
                    user_query=context.user_query,
                    phase_complete=phase_complete
                )

            # Parse suggested_commands from LLM response to typed Pydantic models
            raw_commands = parsed.get("suggested_commands", [])
            suggested_commands = _parse_suggested_commands(raw_commands)

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=suggested_actions,
                suggested_commands=suggested_commands,
                phase_complete=len(hypotheses) >= 2,  # Need at least 2 theories
                confidence=parsed.get("confidence", 0.80),
                recommended_next_phase=4 if len(hypotheses) >= 2 else 3  # Validation if ready
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Extract from text
            # Look for numbered lists as hypotheses
            hypotheses = self._extract_hypotheses_from_text(llm_response.content)

            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "hypotheses": hypotheses if hypotheses else [],
                    "current_phase": 4 if len(hypotheses) >= 2 else 3
                },
                suggested_actions=[],
                suggested_commands=[],
                phase_complete=len(hypotheses) >= 2,
                confidence=0.70,  # Lower for heuristic
                recommended_next_phase=4 if len(hypotheses) >= 2 else 3
            )

    def _extract_hypotheses_from_text(self, text: str) -> List[Dict[str, Any]]:
        """Heuristic extraction of hypotheses from freeform text."""
        hypotheses = []

        # Look for numbered patterns like "1. Theory:"
        import re
        pattern = r'(\d+)\.\s*([^:\n]+):?\s*([^\n]+)'
        matches = re.findall(pattern, text)

        for num, theory, description in matches[:3]:  # Max 3
            # Simple likelihood heuristic
            likelihood = "medium"
            if "likely" in description.lower() or "probably" in description.lower():
                likelihood = "high"
            elif "possible" in description.lower() or "might" in description.lower():
                likelihood = "low"

            hypotheses.append({
                "hypothesis": f"{theory.strip()}: {description.strip()}",
                "likelihood": likelihood,
                "evidence": [],
                "validation_steps": []
            })

        return hypotheses

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Advance to Validation (Phase 4) if:
        - At least 2 hypotheses generated
        - Each has likelihood ranking
        - Validation steps suggested
        """
        hypotheses = response.state_updates.get("hypotheses", [])

        # Need at least 2 theories
        if len(hypotheses) < 2:
            return False

        # Each should have likelihood
        if not all("likelihood" in h for h in hypotheses):
            return False

        # Advance!
        return True

    def build_prompt(self, context: PhaseContext) -> str:
        """Build hypothesis-specific prompt."""

        recent_conversation = "\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state = context.phase_state

        return self.prompt_template.format(
            user_query=context.user_query,
            problem_statement=phase_state.get("problem_statement", "Unknown"),
            symptoms=phase_state.get("symptoms", "None"),
            timeline=phase_state.get("timeline", "Not established"),
            blast_radius=phase_state.get("blast_radius", "Not defined"),
            tests_performed=phase_state.get("tests_performed", "None"),
            recent_conversation=recent_conversation
        )
