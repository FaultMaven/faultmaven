"""Unified Hypothesis Manager - Complete Hypothesis Lifecycle Management

Consolidates all hypothesis management logic into ONE coherent system.

Merges:
- faultmaven/core/investigation/hypothesis_manager.py (OLD - HypothesisManager class)
- faultmaven/services/agentic/hypothesis/hypothesis_manager.py (NEW - evidence linking)

Design Reference:
- docs/architecture/investigation-engine/evidence-driven-investigation-framework.md
- docs/architecture/investigation-engine/investigation-data-models.md (§3 Hypothesis Lifecycle)

Hypothesis Lifecycle (matches HypothesisState enum):
- CAPTURED: Opportunistic hypothesis (not yet promoted to active testing)
- ACTIVE: Currently being tested (promoted from CAPTURED or systematic generation)
- VALIDATED: Confirmed by evidence (likelihood ≥0.70 + ≥2 supporting evidence)
- REFUTED: Disproved by evidence (likelihood ≤0.20 + ≥2 refuting evidence)
- INCONCLUSIVE: System-automated — likelihood 0.3–0.5 stagnant for 3+ turns
- RETIRED: System-automated (likelihood <0.30) or manual abandonment without disproof

Confidence Management:
- Evidence-ratio based: initial + (0.15 × supporting) - (0.20 × refuting)
- Confidence decay for stagnation: base × 0.85^iterations_without_progress
- Auto-transition to VALIDATED/REFUTED based on thresholds

Anchoring Prevention:
- Detect: 4+ hypotheses in same category
- Detect: 3+ iterations without progress
- Detect: Top hypothesis stagnant for 3+ iterations
- Action: Retire low-progress hypotheses, force alternative generation
"""

import logging
from typing import TYPE_CHECKING, Any

from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
)
from faultmaven.modules.case.contracts import (
    EvidenceStance,
    Hypothesis,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

# GAP 4 (process realignment): a freshly-created hypothesis is a PRIOR, never a
# conclusion. Its initial likelihood is capped strictly below the
# ``CAUSE_IDENTIFIED_LIKELIHOOD`` gate so no single emission — an over-confident
# LLM ``likelihood: 1.0`` in particular — can arrive near-conclusion on creation.
# The climb past the gate is earned only by evidence
# (``update_likelihood_from_evidence``) or chain validation. This mirrors
# ``runbook_cause_matcher._MATCHER_MAX_PRIOR`` (the same invariant for the
# external-source prior) and is pinned to the single SSOT via the boot assert
# below, so the cap can never silently drift above the gate if the gate moves
# (Agent-2 SSOT requirement). NOTE: ``create_validated_hypothesis`` is the
# separate, evidence-gated path for opening a hypothesis above this prior.
NEW_HYPOTHESIS_MAX_PRIOR = 0.5
if NEW_HYPOTHESIS_MAX_PRIOR >= CAUSE_IDENTIFIED_LIKELIHOOD:  # pragma: no cover
    raise AssertionError(
        f"NEW_HYPOTHESIS_MAX_PRIOR ({NEW_HYPOTHESIS_MAX_PRIOR}) must be < "
        f"CAUSE_IDENTIFIED_LIKELIHOOD ({CAUSE_IDENTIFIED_LIKELIHOOD})"
    )


class HypothesisManager:
    """Unified hypothesis lifecycle and confidence management

    Responsibilities:
    - Create new hypotheses (CAPTURED or ACTIVE)
    - Update confidence based on evidence
    - Apply confidence decay for stagnation
    - Detect and prevent anchoring bias
    - Track hypothesis testing
    - Auto-transition state (VALIDATED/REFUTED)
    - Evidence linking and ratio calculation
    """

    def __init__(self):
        """Initialize hypothesis manager"""
        pass

    @staticmethod
    def active_hypotheses(case: "Case") -> list[Hypothesis]:
        """Return the case's ACTIVE hypotheses.

        ACTIVE = the LLM is still testing the theory (not CAPTURED/VALIDATED/
        REFUTED/RETIRED). This is the single source of truth for the
        ``cause_state=CANDIDATES`` derivation (redesign R1 / Q4) — do not
        re-implement the count elsewhere.
        """
        return [
            h for h in case.hypotheses.values() if h.state == HypothesisState.ACTIVE
        ]

    @staticmethod
    def count_active_hypotheses(case: "Case") -> int:
        """Count ACTIVE hypotheses. ``>= 2`` derives ``cause_state=CANDIDATES``.

        Coupling caveat (redesign §4.1): the LLM must reliably emit hypotheses
        when the cause is uncertain, or this count under-fires and a genuinely
        ambiguous case reads as UNKNOWN. The prompt mandate that forces
        hypothesis emission under uncertainty ships with this derivation.
        """
        return len(HypothesisManager.active_hypotheses(case))

    @staticmethod
    def activate_queued_hypotheses(case: "Case") -> list[str]:
        """GAP 3: promote CAPTURED (queued-pending-symptom-anchor) hypotheses to
        ACTIVE once the symptom is verified.

        Cause hypotheses formed before the symptom is verified are *queued* as
        CAPTURED rather than dropped (opportunistic intake — data of any order is
        never lost) and rather than activated on an unverified premise. CAPTURED
        is produced ONLY by that pre-anchor queue (nothing else emits it), so every
        CAPTURED hypothesis here is a queued one. They activate as **un-validated
        ACTIVE candidates** (no evidence links, GAP 4-capped prior) — subject to
        the normal decay / anti-anchoring culling, so a stale queued theory cannot
        pollute a conclusion (it simply fails to gather support and decays). This
        is forward-only: ``symptom_verified`` does not un-set in practice, and a
        promoted hypothesis is not demoted back to CAPTURED.

        Returns the promoted hypothesis ids (for turn-progress accounting).
        """
        promoted: list[str] = []
        for h in case.hypotheses.values():
            if h.state == HypothesisState.CAPTURED:
                h.state = HypothesisState.ACTIVE
                promoted.append(h.hypothesis_id)
        return promoted

    @staticmethod
    def calculate_evidence_ratio(hypothesis: Hypothesis) -> float:
        """Compute supporting/(supporting+refuting) evidence ratio.

        Returns 0.0 when there is no linked evidence.
        """
        supporting = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )
        total = supporting + refuting
        return (supporting / total) if total > 0 else 0.0

    def create_hypothesis(
        self,
        statement: str,
        category: str,
        initial_likelihood: float,
        current_turn: int,
        generation_mode: HypothesisGenerationMode = HypothesisGenerationMode.SYSTEMATIC,
        state: HypothesisState = HypothesisState.ACTIVE,
        rationale: str | None = None,
    ) -> Hypothesis:
        """Create new hypothesis.

        GAP 4: ``initial_likelihood`` is capped at ``NEW_HYPOTHESIS_MAX_PRIOR``
        (below the IDENTIFIED gate) — a new hypothesis is a prior, not a
        conclusion; evidence/validation earns the climb. The runbook matcher
        pre-caps to ``_MATCHER_MAX_PRIOR`` (same bound), so its values pass
        through unchanged.
        """
        capped_likelihood = min(max(0.0, initial_likelihood), NEW_HYPOTHESIS_MAX_PRIOR)
        if capped_likelihood < initial_likelihood:
            logger.debug(
                "Capped new-hypothesis prior %.3f -> %.3f (NEW_HYPOTHESIS_MAX_PRIOR)",
                initial_likelihood,
                capped_likelihood,
            )
        hypothesis = Hypothesis(
            statement=statement,
            category=category,
            likelihood=capped_likelihood,
            initial_likelihood=capped_likelihood,
            state=state,
            generation_mode=generation_mode,
            generated_at_turn=current_turn,
            last_updated_turn=current_turn,
            last_progress_at_turn=current_turn,
            evidence_links=[],
            rationale=rationale or "Initial hypothesis",
        )

        logger.info(
            f"Created hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (category={category}, likelihood={capped_likelihood}, turn={current_turn})"
        )

        return hypothesis

    def create_validated_hypothesis(
        self,
        statement: str,
        category: str,
        likelihood: float,
        supporting_evidence_ids: list[str],
        current_turn: int,
    ) -> Hypothesis:
        """Create hypothesis already in VALIDATED state (Single-Shot Validation)."""
        if likelihood < 0.7:
            raise ValueError("Validated hypothesis requires likelihood >= 0.7")
        if len(supporting_evidence_ids) < 2:
            raise ValueError(
                "Validated hypothesis requires at least 2 supporting evidence items"
            )

        hypothesis = self.create_hypothesis(
            statement=statement,
            category=category,
            initial_likelihood=likelihood,
            current_turn=current_turn,
            state=HypothesisState.VALIDATED,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        )

        # Manually link evidence
        for ev_id in supporting_evidence_ids:
            self.link_evidence(hypothesis, ev_id, True, current_turn)

        logger.info(
            f"Created VALIDATED hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (likelihood={likelihood:.2f})"
        )

        return hypothesis

    def link_evidence(
        self,
        hypothesis: Hypothesis,
        evidence_id: str,
        supports: bool,
        turn: int,
        reasoning: str = "Linked by agent",
        stance_confidence: float = 1.0,
        stance_override: EvidenceStance | None = None,
    ) -> None:
        """Link evidence to hypothesis.

        Args:
            hypothesis: Hypothesis to link evidence to
            evidence_id: ID of evidence to link
            supports: True for SUPPORTS, False for REFUTES (ignored if stance_override set)
            turn: Current turn number
            reasoning: Explanation of why evidence is linked
            stance_confidence: Confidence in the stance (0.0-1.0)
            stance_override: Optional explicit stance (SUPPORTS, REFUTES, or NEUTRAL).
                           When set, the `supports` parameter is ignored.
        """
        if stance_override is not None:
            stance = stance_override
        else:
            stance = EvidenceStance.SUPPORTS if supports else EvidenceStance.REFUTES

        link = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id=evidence_id,
            stance=stance,
            reasoning=reasoning,
            stance_confidence=stance_confidence,
        )
        # evidence_links is now List[HypothesisEvidenceLink] (junction-table
        # backed). Upsert by evidence_id: replace an existing link if one
        # already exists for this evidence_id, else append.
        existing_idx = next(
            (
                i
                for i, existing in enumerate(hypothesis.evidence_links)
                if existing.evidence_id == evidence_id
            ),
            None,
        )
        if existing_idx is not None:
            hypothesis.evidence_links[existing_idx] = link
        else:
            hypothesis.evidence_links.append(link)

            stance_label = stance.value
            logger.info(
                f"Linked {stance_label} evidence to hypothesis: {evidence_id}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis": hypothesis.statement[:50],
                },
            )

        # Update likelihood after linking evidence
        # NEUTRAL links are stored for audit trail but do not affect confidence
        self.update_likelihood_from_evidence(hypothesis, turn)

    def update_likelihood_from_evidence(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Update hypothesis likelihood based on evidence accumulation

        Likelihood formula:
        - Start with initial_likelihood
        - Add 0.15 per supporting evidence
        - Subtract 0.20 per refuting evidence
        - Clamp to [0.0, 1.0]

        Args:
            hypothesis: Hypothesis to update
            turn: Current turn number
        """
        old_likelihood = hypothesis.likelihood

        supporting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )

        # Baseline is initial_likelihood. Accumulate deltas.
        new_likelihood = (
            hypothesis.initial_likelihood
            + (0.15 * supporting_count)
            - (0.20 * refuting_count)
        )
        new_likelihood = max(0.0, min(1.0, new_likelihood))

        hypothesis.likelihood = new_likelihood
        hypothesis.last_updated_turn = turn

        # Check if this represents progress
        if abs(new_likelihood - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = turn
            hypothesis.iterations_without_progress = 0
        else:
            hypothesis.iterations_without_progress += 1

        logger.info(
            f"Updated hypothesis likelihood: {old_likelihood:.2f} -> {new_likelihood:.2f}",
            extra={
                "hypothesis_id": hypothesis.hypothesis_id,
                "evidence_ratio": self.calculate_evidence_ratio(hypothesis),
            },
        )

        # Check if hypothesis should transition state
        self._check_state_transition(hypothesis, turn)

    def update_hypothesis_likelihood(
        self,
        hypothesis: Hypothesis,
        new_likelihood: float,
        current_turn: int,
        reason: str,
    ) -> Hypothesis:
        """Update hypothesis likelihood manually (for test results)

        Args:
            hypothesis: Hypothesis to update
            new_likelihood: New likelihood level (0.0 to 1.0)
            current_turn: Current conversation turn
            reason: Reason for likelihood change

        Returns:
            Updated hypothesis
        """
        old_likelihood = hypothesis.likelihood
        hypothesis.likelihood = max(0.0, min(1.0, new_likelihood))  # Clamp to [0, 1]
        hypothesis.last_updated_turn = current_turn
        # Check if this represents progress (consistent with update_likelihood_from_evidence)
        if abs(new_likelihood - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = current_turn
            hypothesis.iterations_without_progress = 0
            logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} likelihood updated: "
                f"{old_likelihood:.2f} → {new_likelihood:.2f} ({reason})"
            )
        else:
            hypothesis.iterations_without_progress += 1
            logger.debug(
                f"Hypothesis {hypothesis.hypothesis_id}: minimal change, "
                f"iterations_without_progress={hypothesis.iterations_without_progress}"
            )

        # Check state transition
        self._check_state_transition(hypothesis, current_turn)

        return hypothesis

    def _check_state_transition(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Check if hypothesis should transition state.

        Transition criteria:
        - VALIDATED: likelihood >= 0.70 and at least 2 supporting evidence
        - REFUTED: likelihood <= 0.20 and at least 2 refuting evidence
        - INCONCLUSIVE: likelihood between 0.3-0.5 for 3+ turns with no evidence progress

        Args:
            hypothesis: Hypothesis to check
            turn: Current turn number
        """
        if hypothesis.state != HypothesisState.ACTIVE:
            # Only active hypotheses can be auto-transitioned
            return

        supporting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )

        # Check for validation
        if hypothesis.likelihood >= 0.70 and supporting_count >= 2:
            hypothesis.state = HypothesisState.VALIDATED
            logger.info(
                f"Hypothesis VALIDATED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                    "supporting_evidence": supporting_count,
                },
            )

        # Check for refutation
        elif hypothesis.likelihood <= 0.20 and refuting_count >= 2:
            hypothesis.refutation_reason = (
                f"likelihood {hypothesis.likelihood:.2f} with "
                f"{refuting_count} refuting evidence items"
            )[:200]
            hypothesis.state = HypothesisState.REFUTED
            logger.info(
                f"Hypothesis REFUTED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                    "refuting_evidence": refuting_count,
                },
            )

        # Check for inconclusive: stagnant in gray zone (0.3-0.5) for 3+ turns
        elif (
            0.3 <= hypothesis.likelihood <= 0.5
            and hypothesis.iterations_without_progress >= 3
        ):
            hypothesis.state = HypothesisState.INCONCLUSIVE
            logger.info(
                f"Hypothesis INCONCLUSIVE: {hypothesis.statement} "
                f"(likelihood={hypothesis.likelihood:.2f}, stagnant for "
                f"{hypothesis.iterations_without_progress} turns)",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                },
            )

        # Check for retirement due to low confidence
        elif (
            hypothesis.likelihood < 0.3 and hypothesis.state != HypothesisState.RETIRED
        ):
            hypothesis.state = HypothesisState.RETIRED
            hypothesis.rationale = "Low confidence after testing"  # Mapped retirement_reason to rationale or logging
            logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} RETIRED (likelihood < 30%)"
            )

    def apply_likelihood_decay(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
    ) -> Hypothesis:
        """Apply likelihood decay to stagnant hypothesis

        Likelihood decay formula: base * 0.85^iterations_without_progress
        """
        if (
            hypothesis.state == HypothesisState.ACTIVE
            and hypothesis.iterations_without_progress > 0
        ):
            old_likelihood = hypothesis.likelihood
            # Apply decay factor
            decay_factor = 0.85**hypothesis.iterations_without_progress
            hypothesis.likelihood = max(0.1, hypothesis.likelihood * decay_factor)

            logger.info(
                f"Applied likelihood decay to hypothesis {hypothesis.hypothesis_id}: "
                f"{old_likelihood:.2f} -> {hypothesis.likelihood:.2f} "
                f"(stagnant for {hypothesis.iterations_without_progress} turns)"
            )

        return hypothesis

    def refute_hypothesis(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
        refuting_evidence_ids: list[str],
        reason: str,
    ) -> Hypothesis:
        """Mark hypothesis as refuted by evidence

        Args:
            hypothesis: Hypothesis to refute
            current_turn: Current conversation turn
            refuting_evidence_ids: IDs of evidence that refutes hypothesis
            reason: Explanation of why hypothesis refuted

        Returns:
            Refuted hypothesis
        """
        # Set refutation_reason BEFORE state so the pair invariant is
        # satisfied if this object is ever re-validated.
        hypothesis.refutation_reason = (reason or "refuted by evidence")[:200]
        hypothesis.state = HypothesisState.REFUTED
        hypothesis.likelihood = 0.0
        # hypothesis.refuting_evidence.extend(refuting_evidence_ids) # Updated logic via link_evidence manually or loop
        existing_evidence_ids = {link.evidence_id for link in hypothesis.evidence_links}
        for ev_id in refuting_evidence_ids:
            if ev_id not in existing_evidence_ids:
                hypothesis.evidence_links.append(
                    HypothesisEvidenceLink(
                        hypothesis_id=hypothesis.hypothesis_id,
                        evidence_id=ev_id,
                        stance=EvidenceStance.REFUTES,
                        reasoning=reason,
                        stance_confidence=1.0,
                    )
                )
        hypothesis.last_updated_turn = current_turn

        logger.info(
            f"Hypothesis {hypothesis.hypothesis_id} REFUTED: {reason} "
            f"(evidence: {refuting_evidence_ids})"
        )

        return hypothesis

    def detect_anchoring(
        self,
        hypotheses: list[Hypothesis],
        current_iteration: int,
    ) -> tuple[bool, str | None, list[str]]:
        """Detect anchoring bias in hypothesis generation/testing

        Anchoring conditions:
        1. 4+ hypotheses in same category
        2. 3+ iterations without progress on any hypothesis
        3. Top hypothesis unchanged for 3+ iterations with <70% confidence

        Args:
            hypotheses: List of all hypotheses
            current_iteration: Current investigation iteration number

        Returns:
            Tuple of (is_anchored, reason, affected_hypothesis_ids)
        """
        active_hypotheses = [
            h
            for h in hypotheses
            if h.state not in [HypothesisState.RETIRED, HypothesisState.REFUTED]
        ]

        if not active_hypotheses:
            return False, None, []

        # Condition 1: Too many in same category
        category_counts: dict[str, list[str]] = {}
        for h in active_hypotheses:
            if h.category not in category_counts:
                category_counts[h.category] = []
            category_counts[h.category].append(h.hypothesis_id)

        for category, hypothesis_ids in category_counts.items():
            if len(hypothesis_ids) >= 4:
                return (
                    True,
                    f"Anchoring: {len(hypothesis_ids)} hypotheses in '{category}' category",
                    hypothesis_ids,
                )

        # Condition 2: No progress for 3+ iterations
        stalled_hypotheses = [
            h.hypothesis_id
            for h in active_hypotheses
            if h.iterations_without_progress >= 3
        ]
        if len(stalled_hypotheses) >= 2:
            return (
                True,
                f"Anchoring: {len(stalled_hypotheses)} hypotheses without progress for 3+ iterations",
                stalled_hypotheses,
            )

        # Condition 3: Top hypothesis stagnant
        sorted_by_likelihood = sorted(
            active_hypotheses, key=lambda h: h.likelihood, reverse=True
        )
        if sorted_by_likelihood:
            top_hypothesis = sorted_by_likelihood[0]
            iterations_stagnant = top_hypothesis.iterations_without_progress

            if iterations_stagnant >= 3 and top_hypothesis.likelihood < 0.7:
                return (
                    True,
                    f"Anchoring: Top hypothesis stagnant for {iterations_stagnant} iterations "
                    f"with only {top_hypothesis.likelihood:.0%} confidence",
                    [top_hypothesis.hypothesis_id],
                )

        return False, None, []

    def force_alternative_generation(
        self,
        existing_hypotheses: list[Hypothesis],
        current_turn: int,
    ) -> dict[str, Any]:
        """Force generation of alternative hypotheses to break anchoring

        Strategy:
        - Identify over-represented categories
        - Generate constraints for alternative generation
        - Retire some low-progress hypotheses

        Args:
            existing_hypotheses: Current hypothesis list
            current_turn: Current conversation turn

        Returns:
            Generation constraints and actions taken
        """
        # Identify over-represented categories
        category_counts: dict[str, int] = {}
        for h in existing_hypotheses:
            if h.state not in [HypothesisState.RETIRED, HypothesisState.REFUTED]:
                category_counts[h.category] = category_counts.get(h.category, 0) + 1

        # Find dominant category
        dominant_category = max(category_counts, key=category_counts.get)
        dominant_count = category_counts[dominant_category]

        # Retire low-progress hypotheses in dominant category
        retired_count = 0
        for h in existing_hypotheses:
            if (
                h.category == dominant_category
                and h.iterations_without_progress >= 2
                and h.state == HypothesisState.ACTIVE
            ):
                h.state = HypothesisState.RETIRED
                h.retirement_reason = f"Anchoring prevention: retired to diversify from {dominant_category}"
                h.last_updated_turn = current_turn
                retired_count += 1

        logger.warning(
            f"Anchoring prevention triggered: retired {retired_count} hypotheses "
            f"from over-represented category '{dominant_category}'"
        )

        return {
            "action": "force_alternative_generation",
            "retired_count": retired_count,
            "dominant_category": dominant_category,
            "constraints": {
                "exclude_categories": [dominant_category],
                "require_diverse_categories": True,
                "min_new_hypotheses": 2,
            },
        }

    def get_testable_hypotheses(
        self,
        hypotheses: list[Hypothesis],
        max_count: int = 3,
    ) -> list[Hypothesis]:
        """Get list of hypotheses ready for testing, sorted by priority

        Priority:
        1. Highest likelihood
        2. Active state (ready for testing)
        3. Has supporting evidence

        Args:
            hypotheses: All hypotheses
            max_count: Maximum number to return

        Returns:
            Sorted list of testable hypotheses
        """
        testable = [
            h
            for h in hypotheses
            if h.state == HypothesisState.ACTIVE
            and h.likelihood > 0.2  # Skip very low confidence
        ]

        # Sort by likelihood (descending)
        sorted_hypotheses = sorted(testable, key=lambda h: h.likelihood, reverse=True)

        return sorted_hypotheses[:max_count]

    def get_validated_hypothesis(
        self,
        hypotheses: list[Hypothesis],
    ) -> Hypothesis | None:
        """Get the validated root cause hypothesis if any

        Args:
            hypotheses: All hypotheses

        Returns:
            Validated hypothesis with highest confidence, or None
        """
        validated = [
            h
            for h in hypotheses
            if h.state == HypothesisState.VALIDATED and h.likelihood >= 0.7
        ]

        if not validated:
            return None

        # Return highest confidence validated hypothesis
        return max(validated, key=lambda h: h.likelihood)

    def get_best_hypothesis(
        self,
        hypotheses: list[Hypothesis],
    ) -> Hypothesis | None:
        """Get the hypothesis with highest confidence

        Args:
            hypotheses: All hypotheses

        Returns:
            Hypothesis with highest likelihood, or None if no active hypotheses
        """
        active_hypotheses = [h for h in hypotheses if h.state == HypothesisState.ACTIVE]

        if not active_hypotheses:
            return None

        return max(active_hypotheses, key=lambda h: h.likelihood)

    def get_hypotheses_by_category(
        self,
        hypotheses: list[Hypothesis],
        category: str,
    ) -> list[Hypothesis]:
        """Get all hypotheses for a specific category

        Args:
            hypotheses: All hypotheses
            category: Category name

        Returns:
            List of hypotheses in category
        """
        return [h for h in hypotheses if h.category == category]

    def has_validated_hypothesis(
        self,
        hypotheses: list[Hypothesis],
    ) -> bool:
        """Check if investigation has at least one validated hypothesis

        Args:
            hypotheses: All hypotheses

        Returns:
            True if at least one hypothesis is validated
        """
        return any(h.state == HypothesisState.VALIDATED for h in hypotheses)


def create_hypothesis_manager() -> HypothesisManager:
    """Factory function for HypothesisManager

    Returns:
        New HypothesisManager instance
    """
    return HypothesisManager()


def rank_hypotheses_by_likelihood(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Sort hypotheses by likelihood (descending)

    Args:
        hypotheses: List of hypotheses to sort

    Returns:
        Sorted list with highest likelihood first
    """
    return sorted(hypotheses, key=lambda h: h.likelihood, reverse=True)
