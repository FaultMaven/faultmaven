"""Blast Radius Agent - Phase 1: Impact Assessment

Responsibilities:
- Define scope of affected systems/services
- Assess user/business impact
- Map dependencies and correlations
- Identify error patterns

Context Size: ~600 tokens (more than Intake, less than full context)
Key Optimizations:
- Includes: problem statement, symptoms, initial observations
- Excludes: Detailed timeline, full conversation history
- Focus on: What's affected? How widespread? What patterns?
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


# Compact blast radius prompt (~600 tokens)
BLAST_RADIUS_PROMPT = """You are FaultMaven's impact assessment specialist. Define the blast radius.

GOAL: Determine scope and severity of the problem.

DOCTOR-PATIENT PHILOSOPHY:
You are a technical diagnostician. The user is your patient.

CORE RULES (ALWAYS FOLLOW):
1. USER CAN ASK ANYTHING: If user asks off-topic question → Answer it briefly, then return to diagnosis
2. ANSWER FIRST, GUIDE SECOND: Always respond to what user said before asking new questions
3. ACKNOWLEDGE BEFORE PROBING: "I see [X is affected]. Let me understand the scope..."
4. MAINTAIN DIAGNOSTIC AGENDA: Guide toward understanding impact, but never force it
5. NO METHODOLOGY JARGON: Don't say "blast radius" or "Phase 1" - speak naturally
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

YOUR DIAGNOSTIC APPROACH (Impact Assessment):
- IF user asks explicit question (what/when/why/how) → Answer it DIRECTLY first
- RESPOND to user's question/statement first
- ASSESS what's still missing: scope? severity? affected systems?
- If missing critical info → Ask ONE specific question
- Examples:
  * User: "API is down" → You: "Is this affecting all users or just some?"
  * User: "Some users can't login" → You: "Can you identify a pattern? Which users specifically?"
  * User: "Production issue" → You: "Got it. Which services are affected?"
- Build on what user tells you - don't repeat answered questions

PROBLEM: {problem_statement}

CURRENT UNDERSTANDING:
Symptoms: {symptoms}
Urgency: {urgency_level}
Existing Blast Radius Info: {existing_blast_radius}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

ASSESSMENT FRAMEWORK:
1. SCOPE Questions:
   - Which services/systems are affected?
   - Is it all users or specific subset?
   - Which environments (prod, staging, dev)?
   - Geographic regions affected?

2. SEVERITY Indicators:
   - Production down vs degraded performance
   - Revenue impact
   - Data integrity risk
   - Security implications

3. PATTERNS to Look For:
   - Specific error codes/messages
   - Time-based patterns (peak hours?)
   - User-specific vs system-wide
   - Correlated failures

4. DEPENDENCIES:
   - Which downstream services affected?
   - Third-party integrations involved?
   - Database/cache/queue impacts

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
  "answer": "Natural explanation of blast radius assessment",
  "blast_radius": {{
    "affected_services": ["service1", "service2"],
    "affected_users": "all" | "subset" | "specific users",
    "environments": ["production", "staging"],
    "severity": "critical" | "high" | "medium" | "low",
    "error_patterns": ["pattern1", "pattern2"],
    "dependencies_impacted": ["dependency1", "dependency2"]
  }},
  "clarifying_questions": [
    "Are all users experiencing this or just certain ones?",
    "Which specific services are showing errors?"
  ],
  "suggested_actions": [
    {{"label": "🔴 All users affected", "type": "question_template", "payload": "Yes, all users are experiencing this issue"}},
    {{"label": "🟡 Only some users", "type": "question_template", "payload": "Only some users are affected - specifically: "}}
  ],
  "suggested_commands": [
    {{"command": "kubectl get pods -n production", "description": "Check pod health", "why": "To see which pods are failing", "safety": "safe"}}
  ],
  "phase_complete": true if scope is well-defined,
  "confidence": 0.0-1.0
}}

CRITICAL GUIDANCE:
- If scope is unclear, ask specific questions
- Look for patterns in user descriptions
- Map technical components to business impact
- Don't advance to Timeline until blast radius is defined
- DON'T announce "Phase 1" to user - focus on helping them
"""


class BlastRadiusAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 1: Impact Assessment.

    Focuses on understanding the scope and severity of the problem.
    Helps identify patterns and affected components before diving into timeline.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=1,
            phase_name="Blast Radius",
            prompt_template=BLAST_RADIUS_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Blast radius needs problem statement, symptoms, urgency."""
        return {
            "problem_statement": full_state.problem_statement or "Unknown problem",
            "symptoms": ", ".join(full_state.symptoms) if full_state.symptoms else "None captured yet",
            "urgency_level": full_state.urgency_level.value,
            "existing_blast_radius": self._format_blast_radius(full_state.blast_radius) if full_state.blast_radius else "Not yet assessed"
        }

    def _format_blast_radius(self, blast_radius: Dict[str, Any]) -> str:
        """Format existing blast radius info for prompt."""
        if not blast_radius:
            return "Not yet assessed"

        parts = []
        for key, value in blast_radius.items():
            if isinstance(value, list):
                parts.append(f"{key}={', '.join(map(str, value))}")
            else:
                parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not yet assessed"

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Assess blast radius and impact."""

        # Build compact prompt
        prompt = self.build_prompt(context)

        # Call LLM
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.7,  # Balanced for pattern recognition
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
            blast_radius_data = parsed.get("blast_radius", {})

            state_updates = {
                "blast_radius": blast_radius_data
            }

            # Check if blast radius is well-defined
            is_complete = self._is_blast_radius_complete(blast_radius_data)

            if is_complete:
                state_updates["current_phase"] = 2  # Advance to Timeline

            # Parse suggested_actions from LLM response to typed Pydantic models
            raw_actions = parsed.get("suggested_actions", [])
            suggested_actions = _parse_suggested_actions(raw_actions)

            # Defensive fallback: Generate contextual actions if LLM didn't provide any
            if not suggested_actions and not is_complete:
                suggested_actions = generate_fallback_actions(
                    phase=self.phase_number,
                    phase_state=context.phase_state,
                    user_query=context.user_query,
                    phase_complete=is_complete
                )

            # Parse suggested_commands from LLM response to typed Pydantic models
            raw_commands = parsed.get("suggested_commands", [])
            suggested_commands = _parse_suggested_commands(raw_commands)

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=suggested_actions,
                suggested_commands=suggested_commands,
                phase_complete=is_complete,
                confidence=parsed.get("confidence", 0.75),
                recommended_next_phase=2 if is_complete else 1
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Extract blast radius info from text
            blast_radius = self._extract_blast_radius_from_text(llm_response.content)

            is_complete = self._is_blast_radius_complete(blast_radius)

            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "blast_radius": blast_radius,
                    "current_phase": 2 if is_complete else 1
                },
                suggested_actions=[],
                suggested_commands=[],
                phase_complete=is_complete,
                confidence=0.65,  # Lower for heuristic
                recommended_next_phase=2 if is_complete else 1
            )

    def _is_blast_radius_complete(self, blast_radius: Dict[str, Any]) -> bool:
        """Check if blast radius is sufficiently defined to advance.

        Criteria:
        - Has affected_services OR affected_users defined
        - Has severity assessment
        """
        if not blast_radius:
            return False

        # Need at least scope (services or users) and severity
        has_scope = (
            blast_radius.get("affected_services") or
            blast_radius.get("affected_users")
        )
        has_severity = blast_radius.get("severity")

        return bool(has_scope and has_severity)

    def _extract_blast_radius_from_text(self, text: str) -> Dict[str, Any]:
        """Heuristic extraction of blast radius from freeform text."""
        blast_radius = {}

        # Simple keyword detection
        text_lower = text.lower()

        # Detect severity
        if any(word in text_lower for word in ["production down", "outage", "complete failure"]):
            blast_radius["severity"] = "critical"
        elif any(word in text_lower for word in ["degraded", "slow", "intermittent"]):
            blast_radius["severity"] = "medium"
        else:
            blast_radius["severity"] = "low"

        # Detect scope
        if "all users" in text_lower:
            blast_radius["affected_users"] = "all"
        elif "some users" in text_lower or "subset" in text_lower:
            blast_radius["affected_users"] = "subset"

        # Extract service mentions (simple heuristic)
        import re
        service_pattern = r'(api|database|cache|redis|postgres|nginx|kubernetes|pod|service|deployment)'
        services = re.findall(service_pattern, text_lower)
        if services:
            blast_radius["affected_services"] = list(set(services))

        return blast_radius

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Advance to Timeline (Phase 2) if:
        - Blast radius is well-defined (scope + severity)
        - Impact is understood
        """
        blast_radius = response.state_updates.get("blast_radius", {})
        return self._is_blast_radius_complete(blast_radius)

    def build_prompt(self, context: PhaseContext) -> str:
        """Build blast radius assessment prompt."""

        recent_conversation = "\\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state = context.phase_state

        return self.prompt_template.format(
            user_query=context.user_query,
            problem_statement=phase_state.get("problem_statement", "Unknown"),
            symptoms=phase_state.get("symptoms", "None"),
            urgency_level=phase_state.get("urgency_level", "normal"),
            existing_blast_radius=phase_state.get("existing_blast_radius", "Not yet assessed"),
            recent_conversation=recent_conversation
        )
