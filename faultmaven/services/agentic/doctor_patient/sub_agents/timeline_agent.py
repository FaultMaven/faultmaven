"""Timeline Agent - Phase 2: Change Analysis

Responsibilities:
- Establish when problem started
- Identify what changed recently
- Find last known good state
- Correlate timeline with symptoms

Context Size: ~650 tokens
Key Optimizations:
- Includes: problem statement, symptoms, blast radius
- Excludes: Detailed hypothesis theories, full conversation
- Focus on: When? What changed? What was working before?
"""

from typing import Dict, Any, List
import json
from datetime import datetime

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


# Compact timeline-focused prompt (~650 tokens)
TIMELINE_PROMPT = """You are FaultMaven's timeline analyst. Establish the incident timeline.

GOAL: Understand WHEN problem started and WHAT changed.

DOCTOR-PATIENT PHILOSOPHY:
You are a technical diagnostician. The user is your patient.

CORE RULES (ALWAYS FOLLOW):
1. USER CAN ASK ANYTHING: If user asks off-topic question → Answer it briefly, then return to diagnosis
2. ANSWER FIRST, GUIDE SECOND: Always respond to what user said before asking new questions
3. ACKNOWLEDGE BEFORE PROBING: "I see [relevant observation]. Let me ask about [Y]..."
4. MAINTAIN DIAGNOSTIC AGENDA: Guide toward understanding timeline and changes, but never force it
5. NO METHODOLOGY JARGON: Don't say "Phase 2" or technical methodology terms - speak naturally
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

YOUR DIAGNOSTIC APPROACH (Timeline):
- IF user asks explicit question (what/when/why/how) → Answer it DIRECTLY first
- RESPOND to user's question/statement first
- ASSESS what's still missing: when it started? what changed? last known good state?
- If missing critical info → Ask ONE specific question
- Examples:
  * "When exactly did you first notice this problem?"
  * "What changed recently? Any deployments, config updates, or infrastructure changes?"
  * "When was the last time it was working normally?"
  * "Was it morning or afternoon when this started? Before or after lunch?"
  * "Let's verify: any deployments in the last 24 hours? Config changes? Traffic spikes?"
- Build on what user tells you - don't repeat answered questions

PROBLEM: {problem_statement}

KNOWN CONTEXT:
Symptoms: {symptoms}
Blast Radius: {blast_radius}
Urgency: {urgency_level}
Existing Timeline: {existing_timeline}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

TIMELINE FRAMEWORK:
1. WHEN did problem start?
   - Exact time if known
   - Approximate time ("this morning", "2 hours ago")
   - Duration ("started happening 30 minutes ago")

2. LAST KNOWN GOOD:
   - When was it working correctly?
   - What was the last successful operation?
   - When was last deployment/change?

3. RECENT CHANGES (within problem window):
   - Code deployments
   - Configuration changes
   - Infrastructure updates
   - Third-party changes
   - Traffic pattern changes
   - Data migrations

4. CORRELATION:
   - Does timing match any changes?
   - Gradual degradation vs sudden failure?
   - Recurring pattern or one-time event?

KEY QUESTIONS TO ASK:
- "When did you first notice this issue?"
- "What changed recently in your system?"
- "When was the last time it was working normally?"
- "Were there any deployments or updates?"
- "Has traffic or usage patterns changed?"

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
  "answer": "Natural explanation of timeline analysis",
  "timeline_info": {{
    "problem_started_at": "2024-01-15 14:30 UTC" | "approximately 2 hours ago" | "unknown",
    "problem_duration": "30 minutes" | "2 hours" | "unknown",
    "last_known_good": "2024-01-15 14:00 UTC" | "this morning" | "unknown",
    "recent_changes": [
      {{
        "type": "deployment" | "config" | "infrastructure" | "other",
        "description": "Deployed API v2.3.1",
        "timestamp": "2024-01-15 14:25 UTC" | "5 minutes before issue",
        "correlation": "high" | "medium" | "low"
      }}
    ],
    "pattern": "sudden" | "gradual" | "intermittent" | "recurring"
  }},
  "clarifying_questions": [
    "When exactly did you first notice the errors?",
    "Were there any deployments in the last few hours?"
  ],
  "suggested_actions": [
    {{"label": "📅 I know when it started", "type": "question_template", "payload": "The problem started at [time] on [date]"}},
    {{"label": "🔄 Recent deployment", "type": "question_template", "payload": "We deployed version [X] at [time]"}}
  ],
  "suggested_commands": [
    {{"command": "kubectl rollout history deployment/api", "description": "Check deployment history", "why": "To see recent deployments", "safety": "safe"}}
  ],
  "phase_complete": true if timeline is established,
  "confidence": 0.0-1.0
}}

CRITICAL GUIDANCE:
- Focus on temporal correlation
- Changes around problem start time are HIGH priority
- If timeline is unclear, ask specific time-based questions
- Don't advance to Hypothesis until timeline is reasonably clear
- DON'T announce "Phase 2" to user
"""


class TimelineAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 2: Timeline and Change Analysis.

    Focuses on understanding the temporal context of the problem.
    Critical for identifying potential root causes through change correlation.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=2,
            phase_name="Timeline",
            prompt_template=TIMELINE_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Timeline needs problem, symptoms, blast radius."""
        return {
            "problem_statement": full_state.problem_statement or "Unknown problem",
            "symptoms": ", ".join(full_state.symptoms) if full_state.symptoms else "None captured",
            "blast_radius": self._format_blast_radius(full_state.blast_radius),
            "urgency_level": full_state.urgency_level.value,
            "existing_timeline": self._format_timeline(full_state.timeline_info) if full_state.timeline_info else "Not yet established"
        }

    def _format_blast_radius(self, blast_radius: Dict[str, Any]) -> str:
        """Format blast radius for prompt."""
        if not blast_radius:
            return "Not yet defined"

        parts = []
        for key, value in blast_radius.items():
            if isinstance(value, list):
                parts.append(f"{key}={', '.join(map(str, value))}")
            else:
                parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not yet defined"

    def _format_timeline(self, timeline_info: Dict[str, Any]) -> str:
        """Format existing timeline info for prompt."""
        if not timeline_info:
            return "Not yet established"

        parts = []
        for key, value in timeline_info.items():
            if key == "recent_changes" and isinstance(value, list):
                changes_summary = f"{len(value)} changes identified"
                parts.append(f"recent_changes={changes_summary}")
            else:
                parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not yet established"

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Establish timeline and identify changes."""

        # Build compact prompt
        prompt = self.build_prompt(context)

        # Call LLM
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.7,  # Balanced for temporal reasoning
            max_tokens=1200
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
            timeline_data = parsed.get("timeline_info", {})

            state_updates = {
                "timeline_info": timeline_data
            }

            # Check if timeline is established
            is_complete = self._is_timeline_complete(timeline_data)

            if is_complete:
                state_updates["current_phase"] = 3  # Advance to Hypothesis

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
                recommended_next_phase=3 if is_complete else 2
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Extract timeline info from text
            timeline_info = self._extract_timeline_from_text(llm_response.content)

            is_complete = self._is_timeline_complete(timeline_info)

            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "timeline_info": timeline_info,
                    "current_phase": 3 if is_complete else 2
                },
                suggested_actions=[],
                suggested_commands=[],
                phase_complete=is_complete,
                confidence=0.65,  # Lower for heuristic
                recommended_next_phase=3 if is_complete else 2
            )

    def _is_timeline_complete(self, timeline_info: Dict[str, Any]) -> bool:
        """Check if timeline is sufficiently established to advance.

        Criteria:
        - Has problem_started_at (even if approximate)
        - OR has recent_changes identified
        - Ideally has both
        """
        if not timeline_info:
            return False

        has_start_time = bool(
            timeline_info.get("problem_started_at") and
            timeline_info.get("problem_started_at") != "unknown"
        )

        has_changes = bool(
            timeline_info.get("recent_changes") and
            len(timeline_info.get("recent_changes", [])) > 0
        )

        # Need at least one temporal anchor
        return has_start_time or has_changes

    def _extract_timeline_from_text(self, text: str) -> Dict[str, Any]:
        """Heuristic extraction of timeline from freeform text."""
        timeline_info = {}

        text_lower = text.lower()

        # Detect time references
        import re

        # Look for "started" patterns
        started_pattern = r'started\s+(at|around|about)\s+([^.]+)'
        started_matches = re.findall(started_pattern, text_lower)
        if started_matches:
            timeline_info["problem_started_at"] = started_matches[0][1].strip()

        # Look for "ago" patterns
        ago_pattern = r'(\d+)\s+(minutes?|hours?|days?)\s+ago'
        ago_matches = re.findall(ago_pattern, text_lower)
        if ago_matches:
            duration = f"{ago_matches[0][0]} {ago_matches[0][1]} ago"
            timeline_info["problem_started_at"] = duration

        # Detect change mentions
        change_keywords = ["deployed", "deployment", "updated", "changed", "configured", "migration"]
        if any(keyword in text_lower for keyword in change_keywords):
            timeline_info["recent_changes"] = [
                {
                    "type": "deployment" if "deploy" in text_lower else "config",
                    "description": "Change mentioned in conversation",
                    "timestamp": "unknown",
                    "correlation": "medium"
                }
            ]

        # Detect pattern
        if "sudden" in text_lower or "immediately" in text_lower:
            timeline_info["pattern"] = "sudden"
        elif "gradual" in text_lower or "slowly" in text_lower:
            timeline_info["pattern"] = "gradual"
        elif "intermittent" in text_lower or "sometimes" in text_lower:
            timeline_info["pattern"] = "intermittent"

        return timeline_info

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Advance to Hypothesis (Phase 3) if:
        - Timeline is established (when problem started)
        - OR recent changes identified
        """
        timeline_info = response.state_updates.get("timeline_info", {})
        return self._is_timeline_complete(timeline_info)

    def build_prompt(self, context: PhaseContext) -> str:
        """Build timeline analysis prompt."""

        recent_conversation = "\\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state = context.phase_state

        return self.prompt_template.format(
            user_query=context.user_query,
            problem_statement=phase_state.get("problem_statement", "Unknown"),
            symptoms=phase_state.get("symptoms", "None"),
            blast_radius=phase_state.get("blast_radius", "Not defined"),
            urgency_level=phase_state.get("urgency_level", "normal"),
            existing_timeline=phase_state.get("existing_timeline", "Not yet established"),
            recent_conversation=recent_conversation
        )
