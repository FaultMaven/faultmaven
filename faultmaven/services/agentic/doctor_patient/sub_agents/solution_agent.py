"""Solution Agent - Phase 5: Resolution and Remediation

Responsibilities:
- Propose specific, actionable fix
- Provide step-by-step implementation
- Assess risks and rollback options
- Suggest preventive measures
- Close the diagnostic loop

Context Size: ~650 tokens (full diagnostic journey summary)
Key Optimizations:
- Includes: problem, root cause, validation evidence, context summary
- Excludes: Detailed conversation history, early exploration
- Focus on: How to fix it? What are the steps? How to prevent recurrence?
"""

from typing import Dict, Any, List
import json

from faultmaven.models import CaseDiagnosticState
from .base import MinimalPhaseAgent, PhaseContext, PhaseAgentResponse


# Solution-focused prompt (~650 tokens)
SOLUTION_PROMPT = """You are FaultMaven's solution architect. Provide clear, actionable resolution.

GOAL: Propose specific fix with implementation steps and risk assessment.

DIAGNOSTIC SUMMARY:
Problem: {problem_statement}
Root Cause: {root_cause}
Symptoms: {symptoms}
Impact: {blast_radius}
Timeline: {timeline}

VALIDATION EVIDENCE:
{validation_evidence}

RECENT CONVERSATION:
{recent_conversation}

USER QUERY: {user_query}

SOLUTION FRAMEWORK:
1. PROPOSED FIX:
   - What specific action will resolve the issue?
   - Why will this fix work (based on root cause)?
   - What is the expected outcome?

2. IMPLEMENTATION STEPS:
   - Clear, numbered steps
   - Specific commands to run
   - Expected results at each step
   - Estimated time

3. RISK ASSESSMENT:
   - What could go wrong?
   - Impact if fix fails
   - Rollback procedure
   - When to abort

4. VERIFICATION:
   - How to confirm fix worked?
   - What metrics to monitor?
   - How long until fully resolved?

5. PREVENTION:
   - How to prevent recurrence?
   - Monitoring/alerting improvements
   - Process changes
   - Documentation needs

RESPONSE FORMAT (JSON):
{{
  "answer": "Natural explanation of the solution",
  "solution": {{
    "fix_description": "Restart the API service to clear the connection pool",
    "why_this_works": "Root cause is connection pool exhaustion; restart will reset connections",
    "implementation_steps": [
      {{
        "step": 1,
        "action": "Put API in maintenance mode",
        "command": "kubectl scale deployment/api --replicas=0",
        "expected_result": "API pods shut down gracefully",
        "estimated_time": "30 seconds"
      }},
      {{
        "step": 2,
        "action": "Restart with fresh pods",
        "command": "kubectl scale deployment/api --replicas=3",
        "expected_result": "New pods start with clean connection pool",
        "estimated_time": "60 seconds"
      }}
    ],
    "risk_level": "low" | "medium" | "high",
    "risks": ["Brief service interruption during restart"],
    "rollback_procedure": "If errors persist, rollback to previous version",
    "verification_steps": [
      "Check error rate drops to < 1%",
      "Monitor connection pool metrics",
      "Verify user-reported issues resolved"
    ],
    "preventive_measures": [
      "Add connection pool monitoring",
      "Set max_connections limit",
      "Implement connection recycling"
    ]
  }},
  "suggested_commands": [
    {{"command": "kubectl scale deployment/api --replicas=0", "description": "Stop API", "why": "To restart cleanly", "safety": "medium"}}
  ],
  "phase_complete": true,
  "confidence": 0.0-1.0
}}

CRITICAL GUIDANCE:
- Be specific and actionable (not vague advice)
- Provide actual commands, not just concepts
- Always include rollback plan
- Assess risks honestly
- Think about long-term prevention
- DON'T announce "Phase 5" to user
- Celebrate success! You've diagnosed and solved the issue.
"""


class SolutionAgent(MinimalPhaseAgent):
    """Specialized agent for Phase 5: Solution and Remediation.

    The final phase that provides concrete resolution steps.
    Closes the diagnostic loop with actionable guidance.
    """

    def __init__(self, llm_client: Any):
        super().__init__(
            llm_client=llm_client,
            phase_number=5,
            phase_name="Solution",
            prompt_template=SOLUTION_PROMPT
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Solution needs the complete diagnostic journey."""
        return {
            "problem_statement": full_state.problem_statement or "Unknown problem",
            "root_cause": full_state.root_cause or "Not yet identified",
            "symptoms": ", ".join(full_state.symptoms) if full_state.symptoms else "None",
            "blast_radius": self._format_blast_radius(full_state.blast_radius),
            "timeline": self._format_timeline(full_state.timeline_info),
            "validation_evidence": self._format_validation_evidence(full_state),
            "solution_proposed": full_state.solution_proposed
        }

    def _format_blast_radius(self, blast_radius: Dict[str, Any]) -> str:
        """Format blast radius for prompt."""
        if not blast_radius:
            return "Not defined"

        severity = blast_radius.get("severity", "unknown")
        affected = blast_radius.get("affected_services", [])

        if isinstance(affected, list):
            affected_str = ", ".join(affected)
        else:
            affected_str = str(affected)

        return f"Severity: {severity}; Affected: {affected_str}"

    def _format_timeline(self, timeline_info: Dict[str, Any]) -> str:
        """Format timeline for prompt."""
        if not timeline_info:
            return "Not established"

        started = timeline_info.get("problem_started_at", "unknown")
        changes = timeline_info.get("recent_changes", [])

        result = f"Started: {started}"
        if changes:
            result += f"; {len(changes)} recent changes"

        return result

    def _format_validation_evidence(self, state: CaseDiagnosticState) -> str:
        """Format validation evidence from hypotheses."""
        if not state.hypotheses:
            return "No validation performed"

        # Find confirmed/high-likelihood hypotheses
        evidence_lines = []
        for h in state.hypotheses:
            likelihood = h.get("likelihood", "unknown")
            if likelihood in ["high", "confirmed"]:
                hypothesis = h.get("hypothesis", "Unknown")
                evidence = h.get("evidence", [])

                line = f"[{likelihood.upper()}] {hypothesis}"
                if evidence:
                    line += f" - Evidence: {', '.join(evidence[:2])}"

                evidence_lines.append(line)

        return "\\n".join(evidence_lines) if evidence_lines else "No strong validation yet"

    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Generate solution with implementation steps."""

        # Build solution prompt
        prompt = self.build_prompt(context)

        # Call LLM
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        llm_response = await self.llm_client.route(
            prompt=prompt,
            temperature=0.6,  # Balanced for creative yet safe solutions
            max_tokens=2000
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
            solution_data = parsed.get("solution", {})

            state_updates = {
                "solution_proposed": True,
                "solution_text": solution_data.get("fix_description", "See detailed solution"),
                "solution_details": solution_data  # Store full solution
            }

            # Phase 5 is the final phase - stay here or mark complete
            is_complete = True  # Solution provided

            return PhaseAgentResponse(
                answer=parsed.get("answer", response_text),
                state_updates=state_updates,
                suggested_actions=[],
                suggested_commands=parsed.get("suggested_commands", []),
                phase_complete=is_complete,
                confidence=parsed.get("confidence", 0.85),
                recommended_next_phase=5  # Stay in solution phase for follow-up
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Fallback: Mark solution as proposed with text content
            return PhaseAgentResponse(
                answer=llm_response.content,
                state_updates={
                    "solution_proposed": True,
                    "solution_text": llm_response.content[:200]  # Summary
                },
                suggested_actions=[],
                suggested_commands=[],
                phase_complete=True,
                confidence=0.70,
                recommended_next_phase=5
            )

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Solution is the final phase - no advancement needed.

        Stay in Phase 5 for:
        - Follow-up questions about solution
        - Implementation assistance
        - Troubleshooting if fix doesn't work (may loop back to earlier phases)
        """
        return False  # Don't advance from solution phase

    def build_prompt(self, context: PhaseContext) -> str:
        """Build solution prompt."""

        recent_conversation = "\\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state = context.phase_state

        return self.prompt_template.format(
            user_query=context.user_query,
            problem_statement=phase_state.get("problem_statement", "Unknown"),
            root_cause=phase_state.get("root_cause", "Not identified"),
            symptoms=phase_state.get("symptoms", "None"),
            blast_radius=phase_state.get("blast_radius", "Not defined"),
            timeline=phase_state.get("timeline", "Not established"),
            validation_evidence=phase_state.get("validation_evidence", "None"),
            recent_conversation=recent_conversation
        )
