"""Validation Agent - Phase 4: Hypothesis Testing

Responsibilities:
- Test hypotheses with diagnostic commands
- Analyze evidence for/against theories
- Narrow down root cause
- Guide user through validation process

Context Size: ~700 tokens (needs hypotheses + test results)
Key Optimizations:
- Includes: problem, blast radius, timeline, hypotheses, test results
- Excludes: Initial intake conversation, detailed history
- Focus on: Which hypothesis is correct? What evidence confirms/refutes?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Validation-focused prompt (~700 tokens)
VALIDATION_PROMPT = """You are FaultMaven's validation specialist. Test hypotheses systematically.

GOAL: Validate hypotheses through evidence gathering and testing.

PROBLEM: {problem_statement}

WORKING CONTEXT:
Symptoms: {symptoms}
Blast Radius: {blast_radius}
Timeline: {timeline}

HYPOTHESES TO VALIDATE:
{hypotheses}

TESTS ALREADY PERFORMED:
{tests_performed}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

VALIDATION FRAMEWORK:
1. PRIORITIZE hypotheses by:
   - Likelihood (test high-likelihood first)
   - Ease of testing (quick tests first)
   - Impact if confirmed (critical issues first)

2. DESIGN TESTS:
   - Specific diagnostic commands
   - Metrics to check
   - Logs to examine
   - Expected results if hypothesis is correct

3. ANALYZE RESULTS:
   - Does evidence confirm hypothesis?
   - Does evidence refute hypothesis?
   - Is evidence inconclusive?
   - Update hypothesis likelihood

4. ITERATE:
   - Suggest next test based on results
   - Narrow down to most likely root cause
   - Collect supporting evidence

VALIDATION STRATEGIES:
- Logs: Check error logs, application logs, system logs
- Metrics: CPU, memory, latency, error rates
- State: Database queries, cache status, queue depth
- Correlation: Compare affected vs unaffected systems
- Reproduction: Can we reproduce the issue?
- Rollback: Does reverting change fix it?

RESPONSE FORMAT (JSON):
{{
  "answer": "Natural explanation of validation progress",
  "validation_results": [
    {{
      "hypothesis_index": 0,
      "status": "confirmed" | "refuted" | "inconclusive",
      "evidence": ["log shows X", "metric Y increased"],
      "updated_likelihood": "high" | "medium" | "low"
    }}
  ],
  "root_cause_confidence": 0.0-1.0,
  "recommended_test": {{
    "hypothesis_index": 1,
    "test_description": "Check database connection pool",
    "command": "psql -c 'SELECT count(*) FROM pg_stat_activity'",
    "expected_result": "Should show < 100 connections",
    "why": "To validate connection exhaustion hypothesis"
  }},
  "suggested_commands": [
    {{"command": "kubectl logs pod/api-123", "description": "Check API logs", "why": "To see actual errors", "safety": "safe"}}
  ],
  "phase_complete": true if root cause identified with high confidence,
  "confidence": 0.0-1.0
}}

CRITICAL GUIDANCE:
- Be systematic - test one hypothesis at a time
- Look for definitive evidence (not just correlation)
- Update hypothesis likelihood based on results
- Don't advance to Solution until root cause is confident (>0.8)
- Guide user through testing process step-by-step
- DON'T announce "Phase 4" to user
"""


class ValidationAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 4: Hypothesis Validation.

    Focuses on systematic testing of hypotheses to identify root cause.
    Analyzes evidence and guides user through diagnostic process.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=4,
            phase_name="Validation",
            prompt_template=VALIDATION_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Validation needs full diagnostic context except detailed history."""
        return {
            "problem_statement": full_state.problem_statement or "Unknown problem",
            "symptoms": ", ".join(full_state.symptoms) if full_state.symptoms else "None captured",
            "blast_radius": self._format_blast_radius(full_state.blast_radius),
            "timeline": self._format_timeline(full_state.timeline_info),
            "hypotheses": self._format_hypotheses(full_state.hypotheses),
            "tests_performed": ", ".join(full_state.tests_performed) if full_state.tests_performed else "None yet",
            "num_hypotheses": len(full_state.hypotheses) if full_state.hypotheses else 0
        }

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

    def _format_timeline(self, timeline_info: Dict[str, Any]) -> str:
        """Format timeline for prompt."""
        if not timeline_info:
            return "Not established"

        parts = []
        for key, value in timeline_info.items():
            if key == "recent_changes" and isinstance(value, list):
                changes_summary = f"{len(value)} changes"
                parts.append(f"recent_changes={changes_summary}")
            else:
                parts.append(f"{key}={value}")

        return "; ".join(parts) if parts else "Not established"

    def _format_hypotheses(self, hypotheses: List[Dict[str, Any]]) -> str:
        """Format hypotheses for prompt."""
        if not hypotheses:
            return "No hypotheses generated yet"

        formatted = []
        for i, h in enumerate(hypotheses):
            likelihood = h.get("likelihood", "unknown")
            hypothesis = h.get("hypothesis", "Unknown hypothesis")
            evidence = h.get("evidence", [])

            line = f"{i}. [{likelihood.upper()}] {hypothesis}"
            if evidence:
                line += f" (Evidence: {', '.join(evidence[:2])})"

            formatted.append(line)

        return "\\n".join(formatted)

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Validate hypotheses through systematic testing."""

        # Build prompt with validation context
        prompt = self.build_prompt(context)

        # Call LLM
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.6,  # Lower for analytical reasoning
            max_tokens=1500
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
            validation_results = parsed.get("validation_results", [])
            root_cause_confidence = parsed.get("root_cause_confidence", 0.0)

            state_updates = {}

            # Update hypotheses with validation results
            if validation_results:
                state_updates["validation_results"] = validation_results

            # Check if root cause is identified
            is_complete = root_cause_confidence >= 0.8

            if is_complete:
                # Extract confirmed hypothesis as root cause
                confirmed = [r for r in validation_results if r.get("status") == "confirmed"]
                if confirmed:
                    # Get the hypothesis text from phase_state
                    phase_hypotheses = context.phase_state.get("hypotheses", "")
                    state_updates["root_cause"] = "Root cause validation in progress"
                    state_updates["current_phase"] = 5  # Advance to Solution

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=[],
                suggested_commands=parsed.get("suggested_commands", []),
                phase_complete=is_complete,
                confidence=root_cause_confidence,
                recommended_next_phase=5 if is_complete else 4
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Continue in validation phase
            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={},
                suggested_actions=[],
                suggested_commands=[],
                phase_complete=False,
                confidence=0.60,
                recommended_next_phase=4
            )

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Advance to Solution (Phase 5) if:
        - Root cause identified with high confidence (>0.8)
        - Evidence confirms hypothesis
        """
        return response.confidence >= 0.8 and response.phase_complete

    def build_prompt(self, context: PhaseContext) -> str:
        """Build validation prompt."""

        recent_conversation = "\\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state = context.phase_state

        return self.prompt_template.format(
            user_query=context.user_query,
            problem_statement=phase_state.get("problem_statement", "Unknown"),
            symptoms=phase_state.get("symptoms", "None"),
            blast_radius=phase_state.get("blast_radius", "Not defined"),
            timeline=phase_state.get("timeline", "Not established"),
            hypotheses=phase_state.get("hypotheses", "No hypotheses"),
            tests_performed=phase_state.get("tests_performed", "None yet"),
            recent_conversation=recent_conversation
        )
