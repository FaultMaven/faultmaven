"""Blast Radius Agent - Phase 1: Impact Assessment

Responsibilities:
- Define scope of affected systems/services
- Assess user/business impact
- Map dependencies and correlations
- Identify error patterns

Context Size: ~500 tokens (more than Intake, less than full context)
Key Optimizations:
- Includes: problem statement, symptoms, initial observations
- Excludes: Detailed timeline, full conversation history
- Focus on: What's affected? How widespread? What patterns?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Compact blast radius prompt (~500 tokens)
BLAST_RADIUS_PROMPT = """You are FaultMaven's impact assessment specialist. Define the blast radius.

GOAL: Determine scope and severity of the problem.

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

RESPONSE FORMAT (JSON):
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

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=[],
                suggested_commands=parsed.get("suggested_commands", []),
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
