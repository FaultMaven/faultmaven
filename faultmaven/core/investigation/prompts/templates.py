"""Investigation Prompt Templates

This module defines the core templates for FaultMaven's THREE-TEMPLATE system:
1. INQUIRY: Explore problem, get commitment.
2. INVESTIGATING: Active investigation (Adaptive).
3. TERMINAL: Documentation and summary.
"""

from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.prompts.context_builder import (
    build_investigation_context,
)
from faultmaven.modules.case.contracts import Case, CaseStatus, InvestigationStage

# =============================================================================
# INQUIRY TEMPLATE
# =============================================================================

INQUIRY_TEMPLATE = """You are FaultMaven, an expert SRE troubleshooting copilot.

STATUS: INQUIRY (Pre-Investigation)

{identity}
{core_context}

{kb_results}

CONVERSATION HISTORY:
{conversation_history}

{system_feedback}
CURRENT USER MESSAGE:
{user_message}

YOUR TASK:
1. Answer the user's question clearly and helpfully.
2. If you detect a problem signal (error, slowness, outage):
   - Formalize it into a 'proposed_problem_statement'.
   - Ask for user confirmation: "Is this accurate?"
3. If Knowledge Base results match (~70%+), suggest them immediately.
4. Assess urgency semantically based on business impact.

Remember: Be reactive. Don't force investigation if the user just wants information.
Use the natural, conversational response for the agent_response field and update state in state_updates.
"""

# =============================================================================
# INVESTIGATING TEMPLATE (Adaptive)
# =============================================================================

INVESTIGATION_BASE = """You are FaultMaven, the Lead Investigator for this case.

STATUS: INVESTIGATING
{identity}
{core_context}

{milestones}

{evidence}

{hypotheses}

CONVERSATION HISTORY:
{conversation_history}

{system_feedback}
CURRENT USER MESSAGE:
{user_message}

YOUR TASK:
{adaptive_instructions}

KEY PRINCIPLES:
- Data-Driven Progress: Complete multiple milestones in one turn if data allows.
- Evidence requests should be specific and actionable.
- Maintain a working conclusion at all times.
- Sound like a helpful colleague, not a robot.

CRITICAL: REASONING-FIRST REQUIREMENT
When completing any milestone, you MUST provide internal_reasoning BEFORE state_updates:

internal_reasoning:
  evidence_analyzed: [list of evidence IDs you considered]
  conclusions: [step-by-step reasoning from evidence to conclusions]
  milestone_justifications:
    milestone_name: "Why this milestone is complete based on evidence X, Y, Z"
  uncertainties: [what remains unclear]

Example - Completing symptom_verified:
  internal_reasoning:
    evidence_analyzed: ["evidence_001", "evidence_002"]
    conclusions:
      - observation: "Error logs show 500 errors starting at 14:35 UTC"
        inference: "Problem is confirmed and ongoing"
        confidence: 0.95
    milestone_justifications:
      symptom_verified: "Confirmed via evidence_001 (error logs) and evidence_002 (metrics) showing consistent 500 errors"
    uncertainties: ["Root cause still unknown"]

Without justification, milestone completion will be REJECTED.

PROACTIVE BLOCKER DETECTION
Detect data quality issues IMMEDIATELY (Turn 1) instead of waiting 3 turns:

If evidence is corrupted, incomplete, missing critical fields, or unusable:
  state_updates:
    missing_critical_data:
      blocker_type: "data_corrupted" | "data_missing" | "data_incomplete" | "data_access_denied"
      description: "Specific issue description"
      what_was_expected: "Complete error logs with timestamps"
      what_was_found: "Logs missing timestamps and stack traces"
      impact: "Cannot establish timeline or trace error origin"
      suggested_alternatives: ["Request logs from different source", "Use metrics as alternative"]
      triggers_degraded_mode: true

This triggers IMMEDIATE degraded mode entry, allowing you to:
- Transparently communicate limitations
- Offer alternative approaches
- Continue best-effort investigation with caveats

For minor issues that don't block progress, use evidence_quality_issues instead.
"""

# Adaptive instructions by stage
STAGE_INSTRUCTIONS = {
    InvestigationStage.SYMPTOM_VERIFICATION: """
Focus: VERIFICATION
- Confirm the problem is real and ongoing.
- Determine blast radius (services/users/regions affected).
- Establish a timeline of when it started.
- Check for recent changes (deployments, config changes).
- If the issue is CRITICAL, look for quick mitigations immediately.
""",
    InvestigationStage.HYPOTHESIS_FORMULATION: """
Focus: DIAGNOSIS (Formulation)
- Verification is complete. Now generate likely theories (hypotheses).
- Use evidence to support or refute theories.
- Don't settle on one theory too early unless evidence is conclusive.
""",
    InvestigationStage.HYPOTHESIS_VALIDATION: """
Focus: DIAGNOSIS (Validation)
- You have lead hypotheses. Now test them rigorously.
- Request specific logs, metrics, or traces to validate the target cause.
- Identify the root cause with high confidence (~70%+).
""",
    InvestigationStage.SOLUTION: """
Focus: RESOLUTION
- Root cause identified. Now propose and verify a permanent fix.
- Ensure the fix addresses the mechanism of failure, not just the symptom.
- Verify effectiveness after the user applies the solution.
""",
}

# =============================================================================
# TERMINAL TEMPLATE
# =============================================================================

TERMINAL_TEMPLATE = """You are FaultMaven. This investigation is complete.

STATUS: {status_upper}
{identity}
{core_context}

The case has been {status_lower}. 

CONVERSATION HISTORY:
{conversation_history}

CURRENT USER MESSAGE:
{user_message}

YOUR TASK:
- Answer questions about the investigation findings.
- Summarize the root cause and solution if requested.
- DO NOT perform new investigation or suggest state changes.
- Focus on documentation and knowledge sharing.
"""

# =============================================================================
# FALLBACK TEMPLATES (Simplified for token limits or errors)
# =============================================================================

FALLBACK_INQUIRY_TEMPLATE = """You are FaultMaven, a troubleshooting assistant.

STATUS: INQUIRY

PROBLEM: {problem_summary}

USER: {user_message}

Respond helpfully. If detecting a problem, propose a problem statement for confirmation.
"""

FALLBACK_INVESTIGATION_TEMPLATE = """You are FaultMaven investigating an issue.

STATUS: INVESTIGATING
STAGE: {stage}
PROBLEM: {problem_summary}

MILESTONES COMPLETED: {milestones_summary}
HYPOTHESES: {hypotheses_summary}

USER: {user_message}

Continue investigation. Focus on the most critical next step.
"""

FALLBACK_TERMINAL_TEMPLATE = """You are FaultMaven. Case is {status}.

PROBLEM: {problem_summary}
RESOLUTION: {resolution_summary}

USER: {user_message}

Answer questions about the findings. Do not reopen investigation.
"""


def get_fallback_prompt_for_case(
    case: Case,
    user_message: str,
) -> str:
    """Build simplified fallback prompt for token limit or error recovery."""

    problem_summary = (
        case.description or case.inquiry.proposed_problem_statement or "Not defined"
    )

    if case.status == CaseStatus.INQUIRY:
        return FALLBACK_INQUIRY_TEMPLATE.format(
            problem_summary=problem_summary[:200], user_message=user_message[:500]
        )

    elif case.status == CaseStatus.INVESTIGATING:
        stage = (
            case.progress.stage_display_name
            if hasattr(case.progress, "stage_display_name")
            else "Unknown"
        )
        milestones = []
        if case.progress.symptom_verified:
            milestones.append("symptom_verified")
        if case.progress.root_cause_identified:
            milestones.append("root_cause_identified")
        if case.progress.solution_proposed:
            milestones.append("solution_proposed")

        hypotheses = []
        for h in list(case.hypotheses.values())[:3]:
            hypotheses.append(f"{h.statement[:50]} ({h.status.value})")

        return FALLBACK_INVESTIGATION_TEMPLATE.format(
            stage=stage,
            problem_summary=problem_summary[:200],
            milestones_summary=", ".join(milestones) if milestones else "None yet",
            hypotheses_summary="; ".join(hypotheses) if hypotheses else "None yet",
            user_message=user_message[:500],
        )

    else:  # TERMINAL
        resolution = (
            "Solution verified"
            if case.progress.solution_verified
            else case.closure_reason or "Closed"
        )
        return FALLBACK_TERMINAL_TEMPLATE.format(
            status=case.status.value,
            problem_summary=problem_summary[:200],
            resolution_summary=resolution,
            user_message=user_message[:500],
        )


# =============================================================================
# DEGRADED MODE INSTRUCTIONS
# =============================================================================


def get_degraded_mode_instructions(case: Case) -> str:
    """
    Generate degraded mode instructions when investigation is blocked or struggling.

    Reference: Prompt Engineering Guide Section 4.6 (lines 1248-1327)
    """
    if not case.degraded_mode or not case.degraded_mode.is_active:
        return ""

    mode = case.degraded_mode
    mode_type_display = mode.mode_type.value.replace("_", " ").title()

    # Map mode types to specific guidance
    if mode.mode_type.value == "data_blocker":
        limitation = "Critical data is corrupted, incomplete, or inaccessible"
        suggestion = (
            "Request alternative data sources or work with available information"
        )
    elif mode.mode_type.value == "limited_data":
        limitation = "Insufficient data to complete full investigation"
        suggestion = "Identify what data would be most valuable and request it"
    elif mode.mode_type.value == "hypothesis_deadlock":
        limitation = "All hypotheses are inconclusive with current evidence"
        suggestion = (
            "Try a different diagnostic approach or escalate to deeper investigation"
        )
    elif mode.mode_type.value == "no_progress":
        limitation = "Investigation has not advanced in several turns"
        suggestion = "Clarify what information would unblock progress"
    elif mode.mode_type.value == "external_dependency":
        limitation = "Waiting on external team or resource"
        suggestion = "Provide interim analysis or alternative approaches"
    else:
        limitation = "Investigation facing unexpected challenges"
        suggestion = "Identify specific blockers and suggest alternatives"

    instructions = f"""
═══════════════════════════════════════════════════════════
⚠️ DEGRADED INVESTIGATION MODE
═══════════════════════════════════════════════════════════

**Type**: {mode_type_display}
**Reason**: {mode.reason}

**BEHAVIOR CHANGES:**

1. **Transparent Communication**
   - ALWAYS prefix responses: "⚠️ Investigation limitations: {limitation}"
   - Explicitly state caveats in EVERY response
   - Be honest about confidence levels

2. **Lower Confidence Assessment**
   - Assess confidence based ONLY on available evidence
   - Use explicit confidence terms:
     * "I'm speculating" (<50% confidence)
     * "I think this is probably..." (50-70% confidence)
     * "I'm fairly confident" (70-90% confidence)
     * Never claim >90% confidence in degraded mode

3. **Offer Fallback Options**
   - Every 2 turns, explicitly offer:
     * Escalation: "Would you like to escalate to [team/person]?"
     * Alternative approach: "We could try [alternative method]"
     * Documentation: "I can document findings so far for handoff"

4. **Continue Best-Effort Investigation**
   - DON'T give up or stop investigating
   - Work within limitations
   - Provide best-effort analysis with caveats
   - Focus on what CAN be determined vs what cannot

5. **Suggested Next Steps**
   - {suggestion}
   - Be specific about what would help exit degraded mode

Turn {case.current_turn}: You are in degraded mode. Follow the above behavior changes strictly.
"""

    return instructions


# =============================================================================
# BUILDER FUNCTIONS
# =============================================================================


def get_prompt_for_case(
    case: Case, user_message: str, kb_results: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Build the final prompt based on case status and stage."""

    ctx = build_investigation_context(case, user_message, kb_results)

    if case.status == CaseStatus.INQUIRY:
        return INQUIRY_TEMPLATE.format(**ctx)

    elif case.status == CaseStatus.INVESTIGATING:
        stage = case.current_stage or InvestigationStage.SYMPTOM_VERIFICATION
        adaptive_instr = STAGE_INSTRUCTIONS.get(
            stage, STAGE_INSTRUCTIONS[InvestigationStage.SYMPTOM_VERIFICATION]
        )

        # Add a note if it's MITIGATION_FIRST
        if case.path_selection and case.path_selection.path == "mitigation_first":
            adaptive_instr = (
                "PATH: MITIGATION_FIRST (Prioritize stopping the impact over finding RCA)\n"
                + adaptive_instr
            )

        # Inject degraded mode instructions if active
        degraded_mode_instr = get_degraded_mode_instructions(case)
        if degraded_mode_instr:
            adaptive_instr = degraded_mode_instr + "\n\n" + adaptive_instr

        return INVESTIGATION_BASE.format(adaptive_instructions=adaptive_instr, **ctx)

    else:  # TERMINAL (RESOLVED/CLOSED)
        return TERMINAL_TEMPLATE.format(
            status_upper=case.status.value.upper(),
            status_lower=case.status.value,
            **ctx,
        )
