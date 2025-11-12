"""Loop-Back Prompt Templates (v3.0)

Guides agent when looping back to previous phases after validation failure.
Three pattern-specific prompts for Phase 4 → Phase 3/2/1 transitions.

Design Reference:
- docs/architecture/prompt-engineering-architecture.md (v3.0 Section 3.4)
- docs/architecture/investigation-phases-and-ooda-integration.md (v3.0 Loop-Back Mechanism)
"""

from typing import Dict, List
from faultmaven.models.investigation import InvestigationState, InvestigationPhase


def get_hypothesis_refutation_loopback_prompt(
    investigation_state: InvestigationState,
    loop_count: int,
    working_conclusion: Dict,
    refutation_reason: str,
) -> str:
    """Pattern 1: Hypothesis Refutation (Phase 4 → Phase 3)

    Trigger: All hypotheses refuted or insufficient hypotheses remaining

    Args:
        investigation_state: Current investigation state
        loop_count: Number of loop-backs so far (1-3)
        working_conclusion: Previous best understanding before loop-back
        refutation_reason: Why hypotheses were refuted

    Returns:
        Prompt template for re-entering Phase 3
    """
    confidence_trajectory = _format_confidence_history(investigation_state)
    evidence_completeness = _format_evidence_completeness(investigation_state)
    refuted_hypotheses = _format_refuted_hypotheses_with_confidence(investigation_state)
    discriminating_evidence = _format_discriminating_evidence(investigation_state)
    patterns_tried = _list_hypothesis_categories_tried(investigation_state)
    highest_confidence = max(investigation_state.ooda_engine.confidence_trajectory, default=0.0)
    evidence_count = len(investigation_state.evidence.evidence_provided)

    return f"""# 🔄 LOOP-BACK: Returning to Hypothesis Generation

## Context
You've completed validation (Phase 4) but all current hypotheses have been refuted or
are insufficient. This is loop-back #{loop_count} of 3 maximum.

## Working Conclusion Before Loop-Back (v3.0)

**Previous Best Understanding**:
Statement: "{working_conclusion.get('statement', 'Unknown')}"
Confidence: {working_conclusion.get('confidence', 0.0)*100:.0f}% ({working_conclusion.get('confidence_level', 'unknown')})
Turn when generated: {working_conclusion.get('generated_at_turn', 0)}

**Why It Failed**:
{refutation_reason}

**Confidence Trajectory** (shows investigation momentum):
{confidence_trajectory}

**Evidence Completeness by Hypothesis**:
{evidence_completeness}

## What We Learned

**Refuted Hypotheses** (tested and ruled out):
{refuted_hypotheses}

**Key Evidence That Discriminates**:
{discriminating_evidence}

**Patterns We've Tried** (avoid repeating):
{patterns_tried}

## Your Task

Generate NEW hypotheses that:
1. **Account for ALL evidence** collected so far (including what ruled out previous theories)
2. **Explore DIFFERENT categories** than before (see "Patterns We've Tried" - avoid these)
3. **Consider alternative root causes** we haven't tested yet
4. **Learn from confidence trajectory**: If previous hypotheses peaked at {highest_confidence*100:.0f}% then declined,
   next hypotheses should target higher initial confidence

**IMPORTANT - Avoid These Mistakes**:
- ❌ Don't regenerate variations of refuted hypotheses
- ❌ Don't ignore discriminating evidence
- ❌ Don't repeat hypothesis categories already tried (see list above)

**EXIT CONDITION**: If you cannot formulate viable new hypotheses because:
- Problem requires domain expertise beyond your knowledge
- Critical evidence is permanently unavailable
- Issue appears to be systemic/environmental rather than single-component failure
- You've exhausted all reasonable investigation paths

Then ADMIT this limitation explicitly and suggest:
"I've explored all reasonable hypotheses given available evidence and expertise.

**What was tried**:
{patterns_tried}

**Peak confidence achieved**: {highest_confidence*100:.0f}%

**Why stuck**: [Explain blocking reason based on evidence state]

This issue may require:
- Escalation to [specific domain expert based on evidence]
- Additional access to [specific systems/logs that would help]
- Different investigation approach [suggest alternative]

I recommend escalating this case and closing with FaultMaven. I can document what
we've learned so far to help the next investigator."

## What to Retain
- Blast radius and timeline (Phases 1-2 findings remain valid)
- All evidence collected ({evidence_count} items)
- Refutation learnings (what it's NOT - see above)
- Working conclusion history (confidence trajectory)

## What to Re-evaluate
- Hypothesis categories (try different types)
- Likelihood rankings (previous rankings were wrong)
- Testing strategies (need different evidence to break through confidence ceiling)

**Success Criteria**:
Generate 2-4 NEW hypotheses that:
- Target unexplored categories (not in "Patterns We've Tried")
- Have clear differentiation from refuted ones
- Aim for >50% initial confidence (if previous peak was <50%, adjust approach)
- Include 2-5 evidence requirements each (following Phase 3 template)
"""


def get_scope_change_loopback_prompt(
    investigation_state: InvestigationState,
    loop_count: int,
    scope_revealing_evidence: str,
) -> str:
    """Pattern 2: Scope Change (Phase 4 → Phase 1)

    Trigger: Evidence reveals actual scope is larger/different than initially defined

    Args:
        investigation_state: Current investigation state
        loop_count: Number of loop-backs so far (1-3)
        scope_revealing_evidence: Evidence that revealed broader scope

    Returns:
        Prompt template for re-entering Phase 1
    """
    initial_scope = investigation_state.ooda_engine.anomaly_frame.affected_scope if investigation_state.ooda_engine.anomaly_frame else "Unknown"

    return f"""# 🔄 LOOP-BACK: Returning to Blast Radius Assessment

## Context
Validation evidence (Phase 4) revealed the actual scope differs from initial assessment.
This is loop-back #{loop_count} of 3 maximum.

## What We Learned
Initial scope: {initial_scope}

New evidence showing broader/different scope:
{scope_revealing_evidence}

Implications:
- Scope is broader than initially assessed
- Multiple systems/components may be affected
- Investigation needs to reassess blast radius

## Your Task
Re-assess blast radius with expanded understanding:
1. What is the ACTUAL scope now that we have more evidence?
2. Are there additional affected systems/users we missed?
3. Does this change severity or urgency assessment?

**EXIT CONDITION**: If scope keeps expanding and:
- Root cause appears to be widespread infrastructure issue
- Multiple independent failures occurring simultaneously
- Scope is beyond single-system troubleshooting capability

Then ADMIT this and suggest:
"The scope has expanded beyond a single-component failure. This appears to be a
[systemic issue/infrastructure problem/cascading failure].

I recommend:
- Escalating to site reliability team for coordination
- Opening separate cases for each affected component
- Focusing on immediate mitigation rather than single root cause

Shall we close this investigation and escalate to appropriate teams?"

## What to Retain
- Original timeline (when issue started)
- All evidence collected
- Refuted hypotheses (still valuable)

## What to Re-evaluate
- Affected scope (expand or refocus)
- Severity assessment (may increase)
- Hypothesis generation (Phase 3 will need broader theories)

Expected: Updated blast radius with evidence-based justification
"""


def get_timeline_wrong_loopback_prompt(
    investigation_state: InvestigationState,
    loop_count: int,
    contradicting_evidence: str,
) -> str:
    """Pattern 3: Timeline Wrong (Phase 4 → Phase 2)

    Trigger: Evidence contradicts initial timeline, requiring timeline reassessment

    Args:
        investigation_state: Current investigation state
        loop_count: Number of loop-backs so far (1-3)
        contradicting_evidence: Evidence that contradicted timeline

    Returns:
        Prompt template for re-entering Phase 2
    """
    initial_timeline = "Unknown"
    if investigation_state.ooda_engine.anomaly_frame:
        initial_timeline = str(investigation_state.ooda_engine.anomaly_frame.started_at)

    return f"""# 🔄 LOOP-BACK: Returning to Timeline Analysis

## Context
Validation evidence (Phase 4) contradicts our initial timeline. We need to re-establish
accurate timeline before continuing. This is loop-back #{loop_count} of 3 maximum.

## What We Learned
Initial timeline: {initial_timeline}

Evidence contradicting timeline:
{contradicting_evidence}

Why this matters:
- Hypotheses were based on wrong timing
- Need to correlate with correct change events
- Root cause analysis depends on accurate "when"

## Your Task
Re-establish accurate timeline:
1. When did symptoms ACTUALLY first appear?
2. What changes occurred around the corrected time?
3. Is this gradual degradation or sudden failure?

**EXIT CONDITION**: If timeline cannot be established because:
- Logs/metrics have been rotated or are unavailable
- Issue is intermittent with no clear start time
- Multiple related issues make single timeline impossible

Then ADMIT this and suggest:
"I cannot establish a reliable timeline due to [specific limitation]. Without accurate
timing, root cause analysis is highly uncertain.

Options:
1. Proceed with 'best guess' timeline (lower confidence solution)
2. Focus on current state mitigation (skip root cause analysis)
3. Escalate to team with access to historical data

What would you prefer?"

## What to Retain
- Blast radius (scope assessment remains valid)
- Evidence that revealed timeline issue
- High-confidence evidence (even if timing unclear)

## What to Re-evaluate
- Timeline (establish correct "when")
- Correlation with changes (re-align with correct timeline)
- Hypothesis generation (Phase 3 will need new theories based on corrected timing)

Expected: Corrected timeline with confidence level and evidence basis
"""


# =============================================================================
# Helper Functions
# =============================================================================


def _format_confidence_history(state: InvestigationState) -> str:
    """Format confidence trajectory for display"""
    if not state.ooda_engine.confidence_trajectory:
        return "No confidence history available"

    trajectory = state.ooda_engine.confidence_trajectory[-5:]  # Last 5 turns
    formatted = []
    for i, conf in enumerate(trajectory):
        turn = state.metadata.current_turn - len(trajectory) + i + 1
        formatted.append(f"Turn {turn}: {conf*100:.0f}%")

    return " → ".join(formatted)


def _format_evidence_completeness(state: InvestigationState) -> str:
    """Format evidence completeness by hypothesis"""
    if not state.ooda_engine.hypotheses:
        return "No hypotheses to evaluate"

    lines = []
    for hyp in state.ooda_engine.hypotheses:
        completeness = hyp.calculate_evidence_completeness() if hasattr(hyp, 'calculate_evidence_completeness') else 0.0
        status_str = f"{hyp.status.value.upper()}" if hasattr(hyp, 'status') else "UNKNOWN"
        lines.append(
            f"- {hyp.statement}: {len(hyp.supporting_evidence)}/{len(hyp.required_evidence)} evidence "
            f"({completeness*100:.0f}%) - {status_str}"
        )

    return "\n".join(lines) if lines else "No evidence completeness data"


def _format_refuted_hypotheses_with_confidence(state: InvestigationState) -> str:
    """Format refuted hypotheses with peak confidence"""
    refuted = [h for h in state.ooda_engine.hypotheses if getattr(h, 'status', None) and h.status.value == 'refuted']

    if not refuted:
        return "No hypotheses formally refuted yet"

    lines = []
    for i, hyp in enumerate(refuted, 1):
        peak_conf = getattr(hyp, 'peak_confidence', hyp.likelihood)
        refuting = [e for e in hyp.refuting_evidence] if hasattr(hyp, 'refuting_evidence') else []
        lines.append(
            f"{i}. \"{hyp.statement}\" (Peak confidence: {peak_conf*100:.0f}%)\n"
            f"   - Refuted by: {len(refuting)} evidence items"
        )

    return "\n".join(lines)


def _format_discriminating_evidence(state: InvestigationState) -> str:
    """Format evidence that ruled out hypotheses"""
    # This would require evidence tracking - simplified for now
    return "Evidence analysis shows: [Key discriminating evidence would be listed here]"


def _list_hypothesis_categories_tried(state: InvestigationState) -> str:
    """List hypothesis categories already attempted"""
    if not state.ooda_engine.hypotheses:
        return "No hypothesis categories tried yet"

    # Extract categories from hypothesis statements (simplified)
    categories = set()
    for hyp in state.ooda_engine.hypotheses:
        # Simple category extraction based on common patterns
        statement_lower = hyp.statement.lower()
        if "pool" in statement_lower or "connection" in statement_lower:
            categories.add("Resource exhaustion (pool, connection)")
        elif "network" in statement_lower or "latency" in statement_lower:
            categories.add("Network issues")
        elif "memory" in statement_lower or "leak" in statement_lower:
            categories.add("Memory management")
        elif "query" in statement_lower or "database" in statement_lower:
            categories.add("Database performance")
        # Add more patterns as needed

    if not categories:
        return "No clear patterns identified"

    return "- " + "\n- ".join(sorted(categories))
