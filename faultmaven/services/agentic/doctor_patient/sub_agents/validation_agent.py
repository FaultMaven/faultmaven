"""Validation Agent - Phase 4: Hypothesis Testing

Responsibilities:
- Test hypotheses with diagnostic commands
- Analyze evidence for/against theories
- Narrow down root cause
- Guide user through validation process

Context Size: ~800 tokens (needs hypotheses + test results)
Key Optimizations:
- Includes: problem, blast radius, timeline, hypotheses, test results
- Excludes: Initial intake conversation, detailed history
- Focus on: Which hypothesis is correct? What evidence confirms/refutes?
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


# Validation-focused prompt (~800 tokens)
VALIDATION_PROMPT = """You are FaultMaven's validation specialist. Test hypotheses systematically.

GOAL: Validate hypotheses through evidence gathering and testing.

DOCTOR-PATIENT PHILOSOPHY:
You are a technical diagnostician. The user is your patient.

CORE RULES (ALWAYS FOLLOW):
1. USER CAN ASK ANYTHING: If user asks off-topic question → Answer it briefly, then return to diagnosis
2. ANSWER FIRST, GUIDE SECOND: Always respond to what user said before asking new questions
3. ACKNOWLEDGE BEFORE PROBING: "I see [relevant observation]. Let me ask about [Y]..."
4. MAINTAIN DIAGNOSTIC AGENDA: Guide toward testing hypotheses, but never force it
5. NO METHODOLOGY JARGON: Don't say "Phase 4" or technical methodology terms - speak naturally
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

YOUR DIAGNOSTIC APPROACH (Validation):
- IF user asks explicit question (what/when/why/how) → Answer it DIRECTLY first
- RESPOND to user's question/statement first
- ASSESS what's still missing: validation data? test results? confirming evidence?
- If missing critical info → Ask ONE specific question
- Examples:
  * "Can you check the error logs for [specific pattern]?"
  * "Let's run this command to confirm: [specific command]"
  * "This confirms [hypothesis] because [interpretation of evidence]."
  * "This rules out [hypothesis]. Let's check [alternative] instead."
  * "To validate this theory, we need to see [specific metric or log]."
- Build on what user tells you - don't repeat answered questions

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
  "suggested_actions": [
    {{"label": "📊 I've gathered evidence", "type": "question_template", "payload": "Here's what I found when I checked: "}},
    {{"label": "❌ Test failed", "type": "question_template", "payload": "The validation test showed: "}}
  ],
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
            "hypotheses_raw": full_state.hypotheses or [],  # Keep raw list for root cause extraction
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
                    # Get the actual confirmed hypothesis text
                    confirmed_idx = confirmed[0].get("hypothesis_index", 0)

                    # Extract hypothesis text from phase_state (has raw hypotheses list)
                    hypotheses_raw = context.phase_state.get("hypotheses_raw", [])
                    if confirmed_idx < len(hypotheses_raw):
                        root_cause_text = hypotheses_raw[confirmed_idx].get("hypothesis", "Confirmed root cause")
                    else:
                        # Fallback: use evidence from validation results
                        evidence = ", ".join(confirmed[0].get("evidence", []))
                        root_cause_text = f"Confirmed via validation: {evidence}"

                    state_updates["root_cause"] = root_cause_text
                    state_updates["current_phase"] = 5  # Advance to Solution

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
