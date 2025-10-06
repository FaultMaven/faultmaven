"""Hypothesis Agent - Phase 3: Root Cause Theory Formation

Responsibilities:
- Generate 2-3 ranked hypotheses about root cause
- Provide evidence for each theory
- Suggest validation steps
- Enable parallel hypothesis testing

Context Size: ~600 tokens (needs more context than Intake)
Key Optimizations:
- Includes: symptoms, timeline, blast radius
- Excludes: Initial intake conversation, detailed history
- Focus on: What could cause this? How to test?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Compact hypothesis-focused prompt (~400 tokens)
HYPOTHESIS_PROMPT = """You are FaultMaven's root cause analyst. Generate ranked hypotheses.

GOAL: Formulate 2-3 root cause theories with validation steps.

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

RESPONSE FORMAT (JSON):
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

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=[],  # Could add "Test hypothesis X" actions
                suggested_commands=parsed.get("suggested_commands", []),
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
