"""OODA Guidance for Weighted Step System

Generates phase-specific OODA guidance using weight profiles.
Provides step emphasis (PRIMARY/tactical/micro) to guide LLM behavior naturally.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

from faultmaven.models.investigation import InvestigationPhase, PHASE_OODA_WEIGHTS
from faultmaven.core.investigation.iteration_strategy import PhaseIterationStrategy


def get_phase_ooda_guidance(phase: InvestigationPhase) -> str:
    """Generate phase-specific OODA guidance using weight profiles

    Args:
        phase: Current investigation phase

    Returns:
        Formatted OODA guidance for this phase

    Example output for Phase 1 (Blast Radius):
        **Current Phase**: Blast Radius (Phase 1/6)
        **OODA Step Weights**:
        - Observe: 60% (PRIMARY FOCUS)
        - Orient: 30% (PRIMARY FOCUS)
        - Decide: 8% (tactical use)
        - Act: 2% (micro-actions only)

        **Primary OODA Focus**: observe (60%), orient (30%)
        **Tactical Use Allowed**: decide (8%)
        **Micro-Actions Permitted**: act (2%)

        Focus on observe and orient, but don't hesitate to make tactical
        decisions (decide) or quick checks (act) when they advance the investigation.
    """
    profile = PHASE_OODA_WEIGHTS[phase]
    norm = profile.normalize()

    # Get step classifications
    primary = profile.get_primary_steps()
    tactical = profile.get_tactical_steps()
    micro = profile.get_micro_steps()

    # Build weight display with emphasis markers
    def format_weight(step: str, weight: float) -> str:
        if weight >= 0.30:
            return f"- {step.capitalize()}: {weight:.0%} (PRIMARY FOCUS)"
        elif weight >= 0.10:
            return f"- {step.capitalize()}: {weight:.0%} (tactical use)"
        elif weight > 0:
            return f"- {step.capitalize()}: {weight:.0%} (micro-actions only)"
        else:
            return f"- {step.capitalize()}: Not used in this phase"

    weights_display = "\n".join([
        format_weight("observe", norm["observe"]),
        format_weight("orient", norm["orient"]),
        format_weight("decide", norm["decide"]),
        format_weight("act", norm["act"]),
    ])

    # Build guidance text
    guidance_parts = [
        f"**Current Phase**: {phase.name.replace('_', ' ').title()} (Phase {phase.value}/6)",
        "",
        "**OODA Step Weights**:",
        weights_display,
        "",
    ]

    # Add focus guidance
    if primary:
        primary_list = ", ".join([f"{s} ({norm[s]:.0%})" for s in primary])
        guidance_parts.append(f"**Primary OODA Focus**: {primary_list}")

    if tactical:
        tactical_list = ", ".join([f"{s} ({norm[s]:.0%})" for s in tactical])
        guidance_parts.append(f"**Tactical Use Allowed**: {tactical_list}")

    if micro:
        micro_list = ", ".join([f"{s} ({norm[s]:.0%})" for s in micro])
        guidance_parts.append(f"**Micro-Actions Permitted**: {micro_list}")

    # Add usage guidance
    if primary:
        primary_verbs = " and ".join(primary)
        guidance_parts.extend([
            "",
            f"**Guidance**: Focus on **{primary_verbs}** to drive this phase forward.",
        ])

        if tactical or micro:
            guidance_parts.append(
                "However, don't hesitate to make tactical decisions or quick checks "
                "when they naturally advance the investigation."
            )

    return "\n".join(guidance_parts)


def get_complete_ooda_prompt(phase: InvestigationPhase) -> str:
    """Get complete OODA prompt with weighted step guidance

    Args:
        phase: Current investigation phase

    Returns:
        OODA guidance section with weight profiles and step emphasis

    Example usage in system prompt:
        system_prompt = f'''
        {LEAD_INVESTIGATOR_SYSTEM_PROMPT}

        {get_complete_ooda_prompt(current_phase)}

        ...rest of prompt...
        '''
    """
    return f"""
# OODA Framework Guidance

{get_phase_ooda_guidance(phase)}
"""


def get_iteration_requirements_text(phase: InvestigationPhase) -> str:
    """Get text description of iteration requirements for phase

    Args:
        phase: Investigation phase

    Returns:
        Human-readable iteration requirements

    Example output for Phase 1:
        "Each iteration in Blast Radius phase should include:
         - Observe (required)
         - Orient (required)
         - Decide (optional - use when tactically useful)
         - Act (optional - use for micro-actions)"
    """
    reqs = PhaseIterationStrategy.get_iteration_requirements(phase)

    lines = [f"Each iteration in {phase.name.replace('_', ' ').title()} phase:"]

    for step, requirement in reqs.items():
        if requirement == "required":
            lines.append(f"- {step.capitalize()} (required - use in every iteration)")
        elif requirement == "optional":
            lines.append(f"- {step.capitalize()} (optional - use when tactically useful)")
        elif requirement == "skip":
            lines.append(f"- {step.capitalize()} (not used in this phase)")

    return "\n".join(lines)


# Step-specific usage examples for prompt
OODA_STEP_EXAMPLES = {
    "observe": [
        "Request specific evidence: logs, metrics, configuration",
        "Ask user to check system state",
        "Gather timeline information",
    ],
    "orient": [
        "Analyze collected evidence for patterns",
        "Contextualize findings within system architecture",
        "Update understanding based on new data",
    ],
    "decide": [
        "Choose which hypothesis to test next",
        "Decide what metric to check",
        "Select investigation approach",
    ],
    "act": [
        "Request user to run diagnostic command",
        "Execute hypothesis test",
        "Implement proposed solution",
    ],
}


def get_step_examples(step: str) -> str:
    """Get usage examples for specific OODA step

    Args:
        step: OODA step name (observe, orient, decide, act)

    Returns:
        Formatted examples for this step
    """
    examples = OODA_STEP_EXAMPLES.get(step, [])
    if not examples:
        return ""

    lines = [f"**{step.capitalize()} Examples**:"]
    lines.extend([f"- {example}" for example in examples])
    return "\n".join(lines)
