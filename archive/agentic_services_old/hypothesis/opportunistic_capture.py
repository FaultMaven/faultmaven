"""Opportunistic Hypothesis Capture (Proposal #2: Continuous Hypothesis Generation)

Captures early hypotheses during Phases 0-2 as expert intuitions emerge naturally.

Design Principle:
- Experienced engineers form hypotheses continuously, not just in Phase 3
- Capture these early intuitions with low confidence (0.3)
- Don't force testing yet - just preserve the insight
- Promote to active testing in Phase 3 based on supporting evidence

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
import re
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationState, InvestigationPhase, Hypothesis

from faultmaven.models.investigation import (
    Hypothesis,
    HypothesisStatus,
    HypothesisGenerationMode,
    InvestigationPhase,
    PHASE_OODA_WEIGHTS
)

logger = logging.getLogger(__name__)


# Patterns that indicate hypothesis formation
OPPORTUNISTIC_PATTERNS = [
    # Pattern recognition
    (r"this (looks|sounds) like (.+?)(?:\.|$|,)", "pattern_recognition"),
    (r"reminds me of (.+?)(?:\.|$|,)", "pattern_recognition"),
    (r"similar to (.+?)(?:\.|$|,)", "pattern_recognition"),

    # Causal speculation
    (r"(probably|likely|possibly) (?:caused by|due to) (.+?)(?:\.|$|,)", "causal"),
    (r"(might|could) be (?:caused by|due to) (.+?)(?:\.|$|,)", "causal"),
    (r"(?:suggests|indicates) (.+?)(?:\.|$|,)", "causal"),

    # Early diagnosis
    (r"(?:i think|i suspect|i believe) (?:it's|this is) (.+?)(?:\.|$|,)", "diagnosis"),
    (r"(?:looks like|seems like) (.+?)(?:\.|$|,)", "diagnosis"),

    # Hypothesis formation
    (r"hypothesis[:\s]+(.+?)(?:\.|$|,)", "explicit"),
    (r"theory[:\s]+(.+?)(?:\.|$|,)", "explicit"),
]


def detect_opportunistic_hypothesis(
    response: str,
    phase: "InvestigationPhase"
) -> Optional[tuple[str, str]]:
    """Detect if response contains an opportunistic hypothesis

    Only captures in early phases (0-2) when Decide step has weight > 0.

    Args:
        response: LLM response text
        phase: Current investigation phase

    Returns:
        (hypothesis_text, pattern_type) if detected, None otherwise

    Example:
        response = "This sounds like a connection pool exhaustion issue."
        detect_opportunistic_hypothesis(response, Phase.BLAST_RADIUS)
        # Returns: ("connection pool exhaustion issue", "pattern_recognition")
    """
    # Only capture in early phases
    if phase not in [
        InvestigationPhase.INTAKE,
        InvestigationPhase.BLAST_RADIUS,
        InvestigationPhase.TIMELINE
    ]:
        return None

    # Check if phase allows Decide step (even micro-weight)
    profile = PHASE_OODA_WEIGHTS[phase]
    if profile.decide == 0.0:
        # Phase doesn't allow decision-making
        return None

    # Try each pattern
    response_lower = response.lower()
    for pattern, pattern_type in OPPORTUNISTIC_PATTERNS:
        match = re.search(pattern, response_lower, re.IGNORECASE)
        if match:
            # Extract hypothesis text from capture group
            if len(match.groups()) >= 2:
                hypothesis_text = match.group(2).strip()
            else:
                hypothesis_text = match.group(1).strip()

            # Clean up
            hypothesis_text = hypothesis_text.rstrip('.,;:')

            # Minimum length check
            if len(hypothesis_text) < 5:
                continue

            logger.info(
                f"Detected opportunistic hypothesis in {phase.name}: {hypothesis_text}",
                extra={"pattern_type": pattern_type}
            )

            return (hypothesis_text, pattern_type)

    return None


def infer_category(hypothesis_text: str) -> str:
    """Infer hypothesis category from text

    Categories:
    - infrastructure: Networks, servers, clusters, regions
    - code: Bugs, logic errors, memory leaks
    - configuration: Config files, environment variables
    - external_dependency: Third-party services, databases
    - client_side: Browser, mobile app issues
    - data: Data corruption, schema issues
    - network: Network partitions, latency
    - security: Auth, permissions, attacks
    - resource_exhaustion: CPU, memory, disk, connections

    Args:
        hypothesis_text: Hypothesis statement

    Returns:
        Category string
    """
    text_lower = hypothesis_text.lower()

    # Infrastructure keywords
    if any(kw in text_lower for kw in [
        "cluster", "server", "node", "region", "datacenter",
        "load balancer", "dns", "infrastructure"
    ]):
        return "infrastructure"

    # Code keywords
    if any(kw in text_lower for kw in [
        "bug", "code", "logic", "function", "method",
        "memory leak", "deadlock", "race condition"
    ]):
        return "code"

    # Configuration keywords
    if any(kw in text_lower for kw in [
        "config", "configuration", "setting", "environment variable",
        "parameter", "property", "option"
    ]):
        return "configuration"

    # External dependency keywords
    if any(kw in text_lower for kw in [
        "database", "redis", "kafka", "api", "third-party",
        "external", "dependency", "service"
    ]):
        return "external_dependency"

    # Network keywords
    if any(kw in text_lower for kw in [
        "network", "connection", "timeout", "latency",
        "packet", "bandwidth", "firewall"
    ]):
        return "network"

    # Resource exhaustion keywords
    if any(kw in text_lower for kw in [
        "exhaustion", "pool", "limit", "capacity",
        "cpu", "memory", "disk", "thread"
    ]):
        return "resource_exhaustion"

    # Default
    return "unknown"


def capture_hypothesis(
    hypothesis_text: str,
    pattern_type: str,
    phase: "InvestigationPhase",
    turn: int,
    triggering_observation: str
) -> "Hypothesis":
    """Create a captured opportunistic hypothesis

    Args:
        hypothesis_text: Hypothesis statement
        pattern_type: Which pattern triggered capture
        phase: Current investigation phase
        turn: Current turn number
        triggering_observation: Context that sparked this hypothesis

    Returns:
        Hypothesis with CAPTURED status and low confidence (0.3)
    """
    category = infer_category(hypothesis_text)

    hypothesis = Hypothesis(
        hypothesis_id=str(uuid4()),
        statement=hypothesis_text,
        category=category,

        # Generation metadata
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        captured_in_phase=phase,
        captured_at_turn=turn,

        # Lifecycle
        status=HypothesisStatus.CAPTURED,
        promoted_to_active_at_turn=None,

        # Context
        triggering_observation=triggering_observation[:200],  # First 200 chars

        # Confidence
        likelihood=0.3,  # Low initial confidence for opportunistic
        initial_likelihood=0.3,
        confidence_trajectory=[(turn, 0.3)],

        # Legacy fields
        created_at_turn=turn,
        last_updated_turn=turn,

        # Evidence (empty initially)
        supporting_evidence=[],
        refuting_evidence=[],

        # Testing (not yet)
        test_plan=None,
        test_results=[],
    )

    logger.info(
        f"Captured opportunistic hypothesis: {hypothesis_text}",
        extra={
            "hypothesis_id": hypothesis.hypothesis_id,
            "category": category,
            "pattern_type": pattern_type,
            "phase": phase.name,
            "turn": turn
        }
    )

    return hypothesis


def capture_if_present(
    response: str,
    state: "InvestigationState"
) -> Optional["Hypothesis"]:
    """Check response for opportunistic hypothesis and capture if found

    Args:
        response: LLM response text
        state: Current investigation state

    Returns:
        Captured Hypothesis if detected, None otherwise
    """
    detection = detect_opportunistic_hypothesis(response, state.lifecycle.current_phase)

    if not detection:
        return None

    hypothesis_text, pattern_type = detection

    # Create hypothesis
    hypothesis = capture_hypothesis(
        hypothesis_text=hypothesis_text,
        pattern_type=pattern_type,
        phase=state.lifecycle.current_phase,
        turn=state.metadata.current_turn,
        triggering_observation=response
    )

    # Add to state
    state.ooda_engine.hypotheses.append(hypothesis)

    return hypothesis


def get_captured_hypotheses(state: "InvestigationState") -> list["Hypothesis"]:
    """Get all captured (not yet promoted) hypotheses

    Args:
        state: Investigation state

    Returns:
        List of hypotheses with CAPTURED status
    """
    return [
        h for h in state.ooda_engine.hypotheses
        if h.status == HypothesisStatus.CAPTURED
    ]


def link_evidence_to_hypothesis(
    hypothesis: "Hypothesis",
    evidence_id: str,
    supports: bool
):
    """Link evidence to hypothesis (supporting or contradicting)

    Args:
        hypothesis: Hypothesis to update
        evidence_id: Evidence identifier
        supports: True if evidence supports, False if contradicts
    """
    if supports:
        if evidence_id not in hypothesis.supporting_evidence:
            hypothesis.supporting_evidence.append(evidence_id)
    else:
        if evidence_id not in hypothesis.refuting_evidence:
            hypothesis.refuting_evidence.append(evidence_id)


def calculate_evidence_ratio(hypothesis: "Hypothesis") -> float:
    """Calculate supporting evidence ratio

    Args:
        hypothesis: Hypothesis to analyze

    Returns:
        Ratio of supporting to total evidence (0.0-1.0)
    """
    total = len(hypothesis.supporting_evidence) + len(hypothesis.refuting_evidence)
    if total == 0:
        return 0.5  # Neutral (no evidence yet)

    return len(hypothesis.supporting_evidence) / total
