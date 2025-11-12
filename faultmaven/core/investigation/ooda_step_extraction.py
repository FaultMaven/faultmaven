"""OODA Step Extraction with Explicit Declaration

Parses LLM responses to extract explicitly declared OODA steps.
Uses <ooda_step>observe</ooda_step> tags for unambiguous step identification.

Includes graceful fallback (Refinement A) when LLM misses declaration.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationPhase, OODAStep

from faultmaven.models.investigation import OODAStep, PHASE_OODA_WEIGHTS

logger = logging.getLogger(__name__)


def extract_ooda_step_from_response(
    response: str,
    phase: "InvestigationPhase",
    metrics_tracker=None
) -> tuple["OODAStep", bool]:
    """Extract OODA step from LLM response with graceful fallback (Refinement A)

    Expected format in LLM response:
        <ooda_step>observe</ooda_step>

    If missing, falls back to primary step for phase with warning.

    Args:
        response: LLM response text
        phase: Current investigation phase
        metrics_tracker: Optional metrics tracker for monitoring

    Returns:
        (step, was_explicit)
        - step: Extracted or fallback OODA step
        - was_explicit: True if found in response, False if fallback used

    Example:
        # Successful extraction
        response = "<ooda_step>observe</ooda_step>Let me check the error logs..."
        step, explicit = extract_ooda_step_from_response(response, Phase.BLAST_RADIUS)
        # Returns: (OODAStep.OBSERVE, True)

        # Graceful fallback
        response = "Let me check the error logs..."  # Missing tag
        step, explicit = extract_ooda_step_from_response(response, Phase.BLAST_RADIUS)
        # Returns: (OODAStep.OBSERVE, False)  # Observe is primary for Phase 1
        # Warning logged
    """
    # Try to extract explicit declaration
    match = re.search(
        r'<ooda_step>\s*(observe|orient|decide|act)\s*</ooda_step>',
        response,
        re.IGNORECASE
    )

    if match:
        step_value = match.group(1).lower()
        return (OODAStep(step_value), True)

    # Graceful fallback: use primary step for phase
    logger.warning(
        f"LLM missed OODA declaration in {phase.name} phase. "
        f"Falling back to primary step. This indicates prompt issue.",
        extra={
            "phase": phase.name,
            "response_preview": response[:200],
        }
    )

    # Get primary step from phase weights
    profile = PHASE_OODA_WEIGHTS[phase]
    primary_steps = profile.get_primary_steps()

    if not primary_steps:
        # Phase has no primary steps (shouldn't happen)
        logger.error(
            f"Phase {phase.name} has no primary steps. Defaulting to OBSERVE.",
            extra={"phase": phase.name}
        )
        fallback_step = OODAStep.OBSERVE
    else:
        fallback_step = OODAStep(primary_steps[0])

    # Track for monitoring (detect prompt failures)
    if metrics_tracker:
        metrics_tracker.increment(
            "ooda_declaration_missing",
            tags={"phase": phase.name, "fallback_step": fallback_step.value}
        )

    return (fallback_step, False)  # was_explicit=False


def validate_and_handle_step(
    phase: "InvestigationPhase",
    declared_step: "OODAStep",
    was_explicit: bool
) -> tuple["OODAStep", str | None]:
    """Validate declared OODA step and handle validation failures (Refinement B)

    Post-extraction validation with graceful recovery.

    Args:
        phase: Current investigation phase
        declared_step: OODA step from LLM
        was_explicit: Whether step was explicitly declared

    Returns:
        (final_step, warning)
        - final_step: Step to use (may be forced to primary if invalid)
        - warning: Warning message if applicable

    Validation failure handling (weight = 0.0):
        - Logs error
        - Forces to primary step for phase
        - Returns warning message
        - Tracks metrics

    Example:
        # Valid step
        step, warning = validate_and_handle_step(
            Phase.BLAST_RADIUS,
            OODAStep.OBSERVE,
            was_explicit=True
        )
        # Returns: (OODAStep.OBSERVE, None)

        # Invalid step (forced to primary)
        step, warning = validate_and_handle_step(
            Phase.DOCUMENT,
            OODAStep.ACT,  # Not allowed in Document phase
            was_explicit=True
        )
        # Returns: (OODAStep.ORIENT, "Invalid step ACT...")
    """
    from faultmaven.core.investigation.iteration_strategy import PhaseIterationStrategy

    # Validate step
    is_valid, validation_warning = PhaseIterationStrategy.validate_step_allowed(
        phase, declared_step
    )

    if is_valid:
        # Valid step (may have tactical/micro warning)
        return (declared_step, validation_warning)

    # Invalid step (weight = 0.0) → Force to primary step
    logger.error(
        f"LLM chose invalid step {declared_step.value} for {phase.name} phase. "
        f"Forcing to primary step.",
        extra={
            "phase": phase.name,
            "invalid_step": declared_step.value,
            "was_explicit": was_explicit,
        }
    )

    # Get primary step
    profile = PHASE_OODA_WEIGHTS[phase]
    primary_steps = profile.get_primary_steps()
    forced_step = OODAStep(primary_steps[0]) if primary_steps else OODAStep.OBSERVE

    warning_message = (
        f"Invalid step {declared_step.value} for {phase.name} phase. "
        f"Forced to {forced_step.value}. This indicates prompt guidance issue."
    )

    return (forced_step, warning_message)


def extract_and_validate_ooda_step(
    response: str,
    phase: "InvestigationPhase",
    metrics_tracker=None
) -> tuple["OODAStep", bool, str | None]:
    """Extract and validate OODA step from LLM response (combined)

    Convenience function combining extraction + validation.

    Args:
        response: LLM response text
        phase: Current investigation phase
        metrics_tracker: Optional metrics tracker

    Returns:
        (step, was_explicit, warning)
        - step: Final OODA step to use
        - was_explicit: True if step was explicitly declared
        - warning: Warning message if validation failed or step is tactical/micro

    Example:
        step, explicit, warning = extract_and_validate_ooda_step(
            response="<ooda_step>observe</ooda_step>Let me check logs...",
            phase=InvestigationPhase.BLAST_RADIUS
        )
        # Returns: (OODAStep.OBSERVE, True, None)
    """
    # Extract step (with fallback)
    declared_step, was_explicit = extract_ooda_step_from_response(
        response, phase, metrics_tracker
    )

    # Validate and handle failures
    final_step, warning = validate_and_handle_step(
        phase, declared_step, was_explicit
    )

    return (final_step, was_explicit, warning)
