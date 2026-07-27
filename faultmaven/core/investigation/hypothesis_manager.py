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
from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    CAUSAL_STANCE_CONFIDENCE_MIN,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    hypothesis_likelihood_capped_no_evidence_total,
)
from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceStance,
    Hypothesis,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

# A freshly-created hypothesis is a PRIOR, never a conclusion. Its initial
# likelihood is capped strictly below the ``CAUSE_IDENTIFIED_LIKELIHOOD`` gate so
# no single emission — an over-confident LLM ``likelihood: 1.0`` in particular —
# can arrive near-conclusion on creation; the climb past the gate is earned only
# by evidence (``update_likelihood_from_evidence``) or chain validation. The
# value is pinned below the gate by the import-time
# check below, so it cannot silently drift above the gate if the gate constant
# is later changed.
NEW_HYPOTHESIS_MAX_PRIOR = 0.5
if NEW_HYPOTHESIS_MAX_PRIOR >= CAUSE_IDENTIFIED_LIKELIHOOD:  # pragma: no cover
    raise AssertionError(
        f"NEW_HYPOTHESIS_MAX_PRIOR ({NEW_HYPOTHESIS_MAX_PRIOR}) must be < "
        f"CAUSE_IDENTIFIED_LIKELIHOOD ({CAUSE_IDENTIFIED_LIKELIHOOD})"
    )

# Anchoring detection condition 1: this many ACTIVE hypotheses in one category
# reads as fixation on a single line of reasoning. Named so downstream producers
# of hypotheses (e.g. the KB cause seeder) can bound how many candidates they
# introduce below this threshold instead of hardcoding a copy of the number.
ANCHORING_SAME_CATEGORY_THRESHOLD = 4

# Age-based stagnation sweep: an ACTIVE hypothesis that has gone this many turns
# since its last progress WITHOUT any turn touching it (see
# ``advance_stagnation_if_ignored``) is treated as stagnant and its stagnation
# counter advances. Coupled to the 3-iteration stagnation horizon the rest of the
# lifecycle already uses (``detect_anchoring`` conditions 2/3 and the INCONCLUSIVE
# transition both act at ``iterations_without_progress >= 3``): an IGNORED
# hypothesis reaches the same stall state as a repeatedly-tested one instead of
# lingering at its prior forever. Kept equal to that horizon on purpose — do not
# diverge the two without cause.
IGNORED_STAGNATION_TURN_THRESHOLD = 3


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
        """Promote CAPTURED (queued-pending-symptom-anchor) hypotheses to ACTIVE
        once the symptom is verified.

        Cause hypotheses formed before the symptom is verified are *queued* as
        CAPTURED rather than dropped (data of any order is retained) and rather
        than activated on an unverified premise. CAPTURED is produced ONLY by that
        pre-anchor queue (nothing else emits it), so every CAPTURED hypothesis here
        is a queued one. They activate as **un-validated ACTIVE candidates** (no
        evidence links, capped prior) — subject to the normal decay / anti-anchoring
        culling, so a stale queued theory cannot pollute a conclusion (it simply
        fails to gather support and decays). Forward-only: a promoted hypothesis is
        not demoted back to CAPTURED.

        Returns the promoted hypothesis ids (for turn-progress accounting).
        """
        promoted: list[str] = []
        for h in case.hypotheses.values():
            if h.state == HypothesisState.CAPTURED:
                h.state = HypothesisState.ACTIVE
                # Activation starts the stagnation clock: a queued theory does
                # not bank the turns it spent CAPTURED (the age-based sweep skips
                # non-ACTIVE hypotheses, so an un-refreshed last_progress_at_turn
                # from its creation turn would charge those turns the instant it
                # goes ACTIVE and pre-age a fresh candidate). Match the
                # create_hypothesis grace — decay counts from activation.
                h.last_progress_at_turn = case.current_turn
                h.last_updated_turn = case.current_turn
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

        ``initial_likelihood`` is capped at ``NEW_HYPOTHESIS_MAX_PRIOR`` (below
        the IDENTIFIED gate) — a new hypothesis is a prior, not a conclusion;
        evidence/validation earns the climb.
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

    def link_evidence(
        self,
        hypothesis: Hypothesis,
        evidence_id: str,
        stance: EvidenceStance,
        turn: int,
        reasoning: str = "Linked by agent",
        stance_confidence: float = 1.0,
    ) -> None:
        """Link evidence to hypothesis.

        Args:
            hypothesis: Hypothesis to link evidence to
            evidence_id: ID of evidence to link
            stance: SUPPORTS, NEUTRAL, or REFUTES — carried through verbatim.
                NEUTRAL links are stored for the audit trail without any
                likelihood effect.
            turn: Current turn number
            reasoning: Explanation of why evidence is linked
            stance_confidence: Confidence in the stance (0.0-1.0)
        """
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
        case: "Case | None" = None,
    ) -> Hypothesis:
        """Update hypothesis likelihood manually (for test results)

        A hypothesis with NO qualifying supporting evidence links is still a
        PRIOR, so a RAISE is capped at ``max(current likelihood,
        NEW_HYPOTHESIS_MAX_PRIOR)`` — the same discipline
        ``create_hypothesis`` applies, as a ceiling only (an update never
        drags belief BELOW its current earned value; downward moves are
        honest and always pass). Without this, a direct LLM update was a
        fiat lever past the ``CAUSE_IDENTIFIED_LIKELIHOOD`` gate the
        creation cap exists to protect (#573 B1): belief above the prior
        cap is earned only by linked case evidence. Qualifying = a
        SUPPORTS link at ``stance_confidence >= CAUSAL_STANCE_CONFIDENCE_MIN``
        (a link the model itself hedges is not grounds for MORE belief —
        the same bar the §7.1 chain tally reads); a refuting-only link set
        does not lift the cap (disconfirmation is not grounds for MORE
        belief either).

        Args:
            hypothesis: Hypothesis to update
            new_likelihood: New likelihood level (0.0 to 1.0)
            current_turn: Current conversation turn
            reason: Reason for likelihood change

        Returns:
            Updated hypothesis
        """
        old_likelihood = hypothesis.likelihood
        capped = max(0.0, min(1.0, new_likelihood))  # Clamp to [0, 1]
        has_supporting_evidence = any(
            link.stance == EvidenceStance.SUPPORTS
            and (link.stance_confidence if link.stance_confidence is not None else 1.0)
            >= CAUSAL_STANCE_CONFIDENCE_MIN
            for link in hypothesis.evidence_links
        ) or self._chain_root_confidently_supported(hypothesis, case)
        # The cap is a CEILING ON RAISES, never a demotion: a hypothesis
        # legitimately above the prior bar (belief earned via the evidence
        # formula, which counts links this qualifying test may reject) keeps
        # its earned value as the effective ceiling — forcing 0.95 down to
        # 0.5 would both punish an honest small correction and reset the
        # stagnation counters (|delta| >= 0.05 reads as progress).
        ceiling = max(old_likelihood, NEW_HYPOTHESIS_MAX_PRIOR)
        if not has_supporting_evidence and capped > ceiling:
            logger.info(
                "Capped evidence-free likelihood update %.3f -> %.3f on %s "
                "(ceiling=max(current, NEW_HYPOTHESIS_MAX_PRIOR); %s)",
                capped,
                ceiling,
                hypothesis.hypothesis_id,
                reason,
            )
            hypothesis_likelihood_capped_no_evidence_total.inc()
            capped = ceiling
        hypothesis.likelihood = capped
        hypothesis.last_updated_turn = current_turn
        # Progress is judged on the APPLIED value (consistent with
        # update_likelihood_from_evidence): a capped re-request of the same
        # over-cap number is a no-op, not progress — it must not reset the
        # stagnation/decay counters.
        if abs(capped - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = current_turn
            hypothesis.iterations_without_progress = 0
            logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} likelihood updated: "
                f"{old_likelihood:.2f} → {capped:.2f} ({reason})"
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

    @staticmethod
    def _chain_root_confidently_supported(
        hypothesis: Hypothesis, case: "Case | None"
    ) -> bool:
        """Chain-axis half of the B1 qualifying test: a confident
        (>= CAUSAL_STANCE_CONFIDENCE_MIN) SUPPORTS link on the hypothesis's
        chain ROOT, backed by a CAUSAL_EVIDENCE row, is real same-turn
        grounding — chain node_evidence_links never mirror into the flat
        hypothesis.evidence_links, so judging the cap on the flat list alone
        permanently capped (and gaslit) chain-only-grounded hypotheses.
        Conservative without a case (no graph access -> False)."""
        if case is None or not getattr(hypothesis, "root_node_id", None):
            return False
        node = (case.causal_nodes or {}).get(hypothesis.root_node_id)
        if node is None:
            return False
        categories = {
            e.evidence_id: getattr(e, "category", None) for e in (case.evidence or [])
        }
        return any(
            link.stance == EvidenceStance.SUPPORTS
            and (link.stance_confidence if link.stance_confidence is not None else 1.0)
            >= CAUSAL_STANCE_CONFIDENCE_MIN
            and categories.get(link.evidence_id) == EvidenceCategory.CAUSAL_EVIDENCE
            for link in node.evidence_links
        )

    def _check_state_transition(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Check the SOFT (likelihood-driven) hypothesis lifecycle transitions.

        VALIDATED and REFUTED are NO LONGER set here (#695 Defect A): validation
        is derived from the chain root node's state
        (``project_hypothesis_states_from_roots``, the sole producer of a
        VALIDATED hypothesis), and refutation is owned by the explicit lifecycle
        (``refute_hypothesis`` / M6 ``_net_refuted``). The flat likelihood
        thresholds set neither state independent of the causal graph anymore.

        Remaining soft transitions (prior/anchoring management, kept):
        - INCONCLUSIVE: likelihood 0.3-0.5 stagnant for 3+ turns
        - RETIRED: likelihood < 0.3 (low confidence after testing)

        Args:
            hypothesis: Hypothesis to check
            turn: Current turn number
        """
        if hypothesis.state != HypothesisState.ACTIVE:
            # Only active hypotheses can be auto-transitioned
            return

        # Check for inconclusive: stagnant in gray zone (0.3-0.5) for 3+ turns
        if (
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

    def advance_stagnation_if_ignored(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
    ) -> Hypothesis:
        """Age an ACTIVE hypothesis toward stagnation when no turn is touching it.

        ``iterations_without_progress`` otherwise advances ONLY when a hypothesis
        is engaged (evidence linked / a likelihood update that fails to move
        belief >= 5%). A hypothesis that is simply IGNORED — never touched by
        evidence — keeps ``iterations_without_progress == 0`` forever, so
        ``apply_likelihood_decay`` no-ops and stagnation-based anchoring never
        fires on it: it lingers at its prior as a permanent unrefuted sibling
        (#713). This sweep closes that gap with an age signal: once a hypothesis
        has gone ``IGNORED_STAGNATION_TURN_THRESHOLD`` turns since its last
        progress, its stagnation counter advances by one per housekeeping turn,
        feeding the SAME decay/anchoring machinery a repeatedly-tested hypothesis
        already drives.

        Conservative and origin-BLIND by construction: it only ADVANCES the
        stagnation counter (which can lower belief over time or stall the theory)
        — it never raises likelihood, validates, refutes, concludes, or reads a
        hypothesis's provenance. A hypothesis touched THIS turn (its
        ``last_updated_turn`` already reached ``current_turn`` via the evidence
        path or its own creation) is skipped, so the counter is never
        double-advanced in one turn.

        Args:
            hypothesis: hypothesis to age
            current_turn: current conversation turn
        """
        if hypothesis.state != HypothesisState.ACTIVE:
            return hypothesis
        # Already touched/created/advanced this turn — don't double-count.
        if hypothesis.last_updated_turn >= current_turn:
            return hypothesis
        turns_since_progress = current_turn - hypothesis.last_progress_at_turn
        if turns_since_progress < IGNORED_STAGNATION_TURN_THRESHOLD:
            return hypothesis

        hypothesis.iterations_without_progress += 1
        hypothesis.last_updated_turn = current_turn
        logger.info(
            "Aged ignored hypothesis %s toward stagnation: "
            "%d turns since progress, iterations_without_progress=%d",
            hypothesis.hypothesis_id,
            turns_since_progress,
            hypothesis.iterations_without_progress,
        )
        return hypothesis

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
        active_hypotheses = [h for h in hypotheses if not h.state.is_terminal]

        if not active_hypotheses:
            return False, None, []

        # Condition 1: Too many in same category
        category_counts: dict[str, list[str]] = {}
        for h in active_hypotheses:
            if h.category not in category_counts:
                category_counts[h.category] = []
            category_counts[h.category].append(h.hypothesis_id)

        for category, hypothesis_ids in category_counts.items():
            if len(hypothesis_ids) >= ANCHORING_SAME_CATEGORY_THRESHOLD:
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
        target_hypothesis_ids: list[str],
        hypotheses: list[Hypothesis],
        current_turn: int,
    ) -> list[str]:
        """Break a detected fixation by retiring the STALLED hypotheses the
        anchoring detector flagged.

        ``target_hypothesis_ids`` are the ids ``detect_anchoring`` returned (the
        over-represented category, or the stalled set) — retiring those keeps the
        action aligned with the reason the intervention fired, rather than acting
        on an unrelated "dominant by count" category. Only ids that are ACTIVE and
        have stalled (``iterations_without_progress >= 2``) are retired, so a
        freshly-formed theory is never swept out.

        Args:
            target_hypothesis_ids: ids flagged by ``detect_anchoring``
            hypotheses: hypotheses to resolve the ids against
            current_turn: current conversation turn

        Returns:
            The list of retired hypothesis ids (possibly empty — e.g. all flagged
            hypotheses are still fresh).
        """
        by_id = {h.hypothesis_id: h for h in hypotheses}
        retired: list[str] = []
        for hid in target_hypothesis_ids:
            h = by_id.get(hid)
            if (
                h is not None
                and h.state == HypothesisState.ACTIVE
                and h.iterations_without_progress >= 2
            ):
                h.state = HypothesisState.RETIRED
                h.retirement_reason = (
                    "Anti-anchoring: retired a stalled hypothesis to diversify "
                    "the differential"
                )
                h.last_updated_turn = current_turn
                retired.append(hid)

        if retired:
            logger.warning(
                "Anti-anchoring retired %d stalled hypotheses: %s",
                len(retired),
                retired,
            )
        return retired

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
