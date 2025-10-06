"""Timeline Agent - Phase 2: Change Analysis

Responsibilities:
- Establish when problem started
- Identify what changed recently
- Find last known good state
- Correlate timeline with symptoms

Context Size: ~550 tokens
Key Optimizations:
- Includes: problem statement, symptoms, blast radius
- Excludes: Detailed hypothesis theories, full conversation
- Focus on: When? What changed? What was working before?
"""

from typing import Dict, Any, List
import json
from datetime import datetime

from faultmaven.models import CaseDiagnosticState
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Compact timeline-focused prompt (~550 tokens)
TIMELINE_PROMPT = """You are FaultMaven's timeline analyst. Establish the incident timeline.

GOAL: Understand WHEN problem started and WHAT changed.

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

RESPONSE FORMAT (JSON):
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

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=[],
                suggested_commands=parsed.get("suggested_commands", []),
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
