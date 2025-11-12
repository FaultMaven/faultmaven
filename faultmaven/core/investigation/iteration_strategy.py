"""Phase Iteration Strategy for Weighted OODA System

Defines how OODA steps combine within a single iteration for each phase.
Different phases have different iteration structures based on weight profiles.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationPhase, OODAStep

from faultmaven.models.investigation import PHASE_OODA_WEIGHTS


class PhaseIterationStrategy:
    """Defines how OODA steps combine within one iteration

    Different phases have different iteration structures:
    - Phase 0 (Intake): Light O+O for problem triage
    - Phase 1 (Blast Radius): Every iteration MUST Observe+Orient, MAY Decide/Act
    - Phase 4 (Validation): Every iteration uses all four steps (full OODA)
    - Phase 5 (Solution): Every iteration focuses on Decide+Act

    Weights determine step requirements:
    - ≥30% normalized weight → "required" (must occur in every iteration)
    - >0% but <30% → "optional" (may occur when tactically useful)
    - 0% → "skip" (not used in this phase)
    """

    @staticmethod
    def get_iteration_requirements(
        phase: "InvestigationPhase"
    ) -> dict[str, str]:
        """Returns step requirements for iterations in this phase

        Args:
            phase: Investigation phase

        Returns:
            {"observe": "required"|"optional"|"skip", ...}

        Interpretation:
        - "required": Must occur in every iteration
        - "optional": May occur when tactically useful
        - "skip": Not used in this phase

        Example:
            For Phase 1 (Blast Radius):
            {
                "observe": "required",   # 60% weight → primary focus
                "orient": "required",    # 30% weight → secondary focus
                "decide": "optional",    # 8% weight → tactical use
                "act": "optional"        # 2% weight → micro-actions
            }
        """
        profile = PHASE_OODA_WEIGHTS[phase]
        norm = profile.normalize()

        def classify_requirement(weight: float) -> str:
            if weight == 0.0:
                return "skip"
            elif weight >= 0.30:
                return "required"  # Primary/secondary focus
            else:
                return "optional"  # Tactical/micro use

        return {
            "observe": classify_requirement(norm["observe"]),
            "orient": classify_requirement(norm["orient"]),
            "decide": classify_requirement(norm["decide"]),
            "act": classify_requirement(norm["act"]),
        }

    @staticmethod
    def get_expected_steps_per_iteration(
        phase: "InvestigationPhase"
    ) -> list["OODAStep"]:
        """Get steps that should typically occur in each iteration

        Returns only "required" steps (≥30% weight).

        Args:
            phase: Investigation phase

        Returns:
            List of OODAStep instances for required steps

        Example:
            Phase 1 (Blast Radius) → [OODAStep.OBSERVE, OODAStep.ORIENT]
            Phase 4 (Validation) → [OBSERVE, ORIENT, DECIDE, ACT] (full OODA)
            Phase 5 (Solution) → [OODAStep.DECIDE, OODAStep.ACT]
        """
        from faultmaven.models.investigation import OODAStep

        reqs = PhaseIterationStrategy.get_iteration_requirements(phase)
        return [
            OODAStep(step)
            for step, req in reqs.items()
            if req == "required"
        ]

    @staticmethod
    def get_guidance_for_phase(phase: "InvestigationPhase") -> str:
        """Generate human-readable guidance for OODA usage in phase

        Args:
            phase: Investigation phase

        Returns:
            Guidance text describing OODA step usage

        Example output for Phase 1 (Blast Radius):
            "Primary OODA Focus: observe (60%), orient (30%)
             Tactical Use Allowed: decide (8%)
             Micro-Actions Permitted: act (2%)"
        """
        profile = PHASE_OODA_WEIGHTS[phase]
        norm = profile.normalize()

        primary = profile.get_primary_steps()
        tactical = profile.get_tactical_steps()
        micro = profile.get_micro_steps()

        guidance_parts = []

        if primary:
            primary_weights = ", ".join([
                f"{step} ({norm[step]:.0%})"
                for step in primary
            ])
            guidance_parts.append(f"**Primary OODA Focus**: {primary_weights}")

        if tactical:
            tactical_weights = ", ".join([
                f"{step} ({norm[step]:.0%})"
                for step in tactical
            ])
            guidance_parts.append(f"**Tactical Use Allowed**: {tactical_weights}")

        if micro:
            micro_weights = ", ".join([
                f"{step} ({norm[step]:.0%})"
                for step in micro
            ])
            guidance_parts.append(f"**Micro-Actions Permitted**: {micro_weights}")

        return "\n".join(guidance_parts)

    @staticmethod
    def validate_step_allowed(
        phase: "InvestigationPhase",
        declared_step: "OODAStep"
    ) -> tuple[bool, str | None]:
        """Validate declared OODA step is allowed in phase (post-extraction check)

        This is post-generation validation. The LLM should be guided via prompt
        to choose appropriate steps, but this validates the choice.

        Args:
            phase: Current investigation phase
            declared_step: OODA step declared by LLM

        Returns:
            (is_valid, warning_message)

        Example:
            (True, None) → Valid step, no warning
            (True, "Note: decide is micro-action only") → Valid but light use
            (False, "decide not allowed in DOCUMENT") → Invalid step
        """
        profile = PHASE_OODA_WEIGHTS[phase]
        weight = getattr(profile, declared_step.value)
        norm = profile.normalize()
        norm_weight = norm[declared_step.value]

        if weight == 0.0:
            return (
                False,
                f"{declared_step.value} not allowed in {phase.name} phase"
            )
        elif norm_weight < 0.10:
            return (
                True,
                f"Note: {declared_step.value} is micro-action only ({norm_weight:.0%}) in {phase.name}"
            )
        elif norm_weight < 0.30:
            return (
                True,
                f"Note: {declared_step.value} is tactical ({norm_weight:.0%}) in {phase.name}"
            )
        else:
            return (True, None)  # Primary or secondary focus
