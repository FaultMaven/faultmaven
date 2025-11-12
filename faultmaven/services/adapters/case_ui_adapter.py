"""Phase-adaptive case response adapter for UI optimization.

This module transforms internal Case domain models into UI-optimized responses
based on case status (CONSULTING, INVESTIGATING, RESOLVED).

Purpose:
- Eliminate multiple API calls to assemble UI state
- Return only relevant fields for each investigation phase
- Provide computed metrics (progress percentages, duration, etc.)
- Extract phase-specific data from case state

Architecture:
- Pure transformer (no external dependencies beyond models)
- Stateless functions
- Zero business logic (just data transformation)
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from faultmaven.models.case import Case, CaseStatus, HypothesisStatus, InvestigationPath
from faultmaven.models.case_ui import (
    CaseUIResponse,
    CaseUIResponse_Consulting,
    CaseUIResponse_Investigating,
    CaseUIResponse_Resolved,
    ClarifyingQuestion,
    ConsultingResponseData,
    EvidenceSummary,
    HypothesisSummary,
    ImpactData,
    InvestigationProgressSummary,
    InvestigationStrategyData,
    ProblemVerificationData,
    ReportAvailability,
    ResolutionSummary,
    RootCauseSummary,
    SolutionSummary,
    TemporalStateData,
    UserRequestSummary,
    VerificationStatus,
    WorkingConclusionSummary,
)


def transform_case_for_ui(case: Case) -> CaseUIResponse:
    """Transform Case model into phase-adaptive UI response.

    Args:
        case: Internal Case domain model

    Returns:
        Phase-specific UI response (Consulting, Investigating, or Resolved)

    Raises:
        ValueError: If case status is unsupported (CLOSED without RESOLVED)
    """
    if case.status == CaseStatus.CONSULTING:
        return _transform_consulting(case)
    elif case.status == CaseStatus.INVESTIGATING:
        return _transform_investigating(case)
    elif case.status == CaseStatus.RESOLVED:
        return _transform_resolved(case)
    elif case.status == CaseStatus.CLOSED:
        # CLOSED cases: return RESOLVED format with closure details
        return _transform_resolved(case)
    else:
        raise ValueError(f"Unsupported case status for UI transformation: {case.status}")


# ============================================================
# Helper Functions for Data Extraction
# ============================================================

def _get_investigation_strategy_data(case: Case) -> Optional[InvestigationStrategyData]:
    """Extract investigation strategy from case state."""

    # Map investigation path to descriptive approach
    approach_map = {
        InvestigationPath.MITIGATION_FIRST:
            "Mitigation-first - quick fix now, comprehensive RCA after service restored",
        InvestigationPath.ROOT_CAUSE:
            "Root cause analysis - thorough investigation before permanent solution",
        InvestigationPath.USER_CHOICE:
            "User choice - awaiting path selection based on requirements"
    }

    # Get path from path_selection if available, otherwise use default
    path = case.path_selection.path if case.path_selection else InvestigationPath.ROOT_CAUSE
    approach = approach_map.get(path, "Standard investigation")

    # Extract next steps from pending milestones
    next_steps = []
    pending_milestones = case.progress.pending_milestones[:3]  # Top 3

    if pending_milestones:
        milestone_steps = {
            "symptom_verified": "Verify symptom with concrete evidence",
            "scope_assessed": "Assess scope and impact (users/services/regions)",
            "timeline_established": "Establish when problem started and timeline",
            "changes_identified": "Identify recent changes (deployments, configs)",
            "root_cause_identified": "Identify and validate root cause",
            "solution_proposed": "Propose solution or mitigation",
            "solution_applied": "Apply solution or mitigation",
            "solution_verified": "Verify solution effectiveness",
            "mitigation_applied": "Apply quick mitigation (MITIGATION_FIRST path)"
        }
        next_steps = [
            milestone_steps.get(m, f"Complete {m.replace('_', ' ')}")
            for m in pending_milestones
        ]

    return InvestigationStrategyData(
        approach=approach,
        next_steps=next_steps if next_steps else None
    )


def _extract_problem_verification(case: Case) -> Optional[ProblemVerificationData]:
    """Extract problem verification data from case state."""

    # Get urgency and severity
    urgency_level = "unknown"
    severity = None

    if case.consulting and case.consulting.problem_confirmation:
        severity = case.consulting.problem_confirmation.severity_guess or "medium"

    # Extract temporal state from evidence timeline
    temporal_state = None
    if case.evidence:
        timestamps = [e.collected_at for e in case.evidence if hasattr(e, 'collected_at') and e.collected_at]
        if timestamps:
            sorted_times = sorted(timestamps)
            temporal_state = TemporalStateData(
                started_at=sorted_times[0],
                last_occurrence_at=sorted_times[-1] if len(sorted_times) > 1 else None,
                state="ongoing"  # Could be determined from evidence recency
            )

    # Extract impact from case description (simple keyword extraction)
    impact = None
    affected_services = []
    affected_users = None
    affected_regions = []

    if case.description:
        # Simple keyword extraction for services
        common_services = ['api', 'service', 'database', 'db', 'cache', 'auth', 'payment', 'checkout']
        text_lower = case.description.lower()

        for service in common_services:
            if service in text_lower:
                affected_services.append(service)

        # Check for user impact indicators
        if any(word in text_lower for word in ['users', 'customers', 'all']):
            affected_users = "Multiple users affected"

    if affected_services or affected_users:
        impact = ImpactData(
            affected_services=affected_services if affected_services else None,
            affected_users=affected_users,
            affected_regions=affected_regions if affected_regions else None
        )

    # User impact summary
    user_impact = None
    if impact and affected_services:
        user_impact = f"{len(affected_services)} service(s) affected"
        if affected_users:
            user_impact += f" - {affected_users}"

    return ProblemVerificationData(
        urgency_level=urgency_level,
        severity=severity,
        temporal_state=temporal_state,
        impact=impact,
        user_impact=user_impact
    )


# ============================================================
# Phase-Specific Transformation Functions
# ============================================================

def _transform_consulting(case: Case) -> CaseUIResponse_Consulting:
    """Transform case into CONSULTING phase UI response."""

    # Defensive: Ensure consulting object exists (should never be None from repository)
    # If somehow None, initialize with default to satisfy API contract requirement
    if case.consulting is None:
        from faultmaven.models.case import ConsultingData
        case.consulting = ConsultingData()

    # Build nested consulting data
    consulting_data = ConsultingResponseData(
        proposed_problem_statement=case.consulting.proposed_problem_statement,
        problem_statement_confirmed=case.consulting.problem_statement_confirmed,
        decided_to_investigate=case.consulting.decided_to_investigate,
        consultation_turns=case.consulting.consultation_turns,
        problem_confirmation={
            "problem_type": case.consulting.problem_confirmation.problem_type if case.consulting.problem_confirmation else None,
            "severity_guess": case.consulting.problem_confirmation.severity_guess if case.consulting.problem_confirmation else "unknown"
        } if case.consulting.problem_confirmation else None
    )

    return CaseUIResponse_Consulting(
        case_id=case.case_id,
        status=CaseStatus.CONSULTING,
        title=case.title,
        current_turn=case.current_turn,
        created_at=case.created_at,
        updated_at=case.updated_at,
        uploaded_files_count=len(case.uploaded_files),
        consulting=consulting_data
    )


def _transform_investigating(case: Case) -> CaseUIResponse_Investigating:
    """Transform case into INVESTIGATING phase UI response."""

    # Build progress summary
    progress = InvestigationProgressSummary(
        milestones_completed=len(case.progress.completed_milestones),
        total_milestones=9,  # 9 milestones: 4 verification + 1 root_cause + 3 solution + 1 mitigation
        completed_milestone_ids=case.progress.completed_milestones,
        current_stage=case.progress.current_stage
    )

    # Build working conclusion from highest-confidence active hypothesis
    working_conclusion = None
    if case.hypotheses:
        active_hypotheses = [h for h in case.hypotheses.values() if h.status == HypothesisStatus.ACTIVE]
        if active_hypotheses:
            # Get hypothesis with highest likelihood
            best_hypothesis = max(active_hypotheses, key=lambda h: h.likelihood)
            working_conclusion = WorkingConclusionSummary(
                summary=best_hypothesis.description,
                confidence=best_hypothesis.likelihood,
                last_updated=case.updated_at  # Could track hypothesis update time separately
            )

    # Build hypothesis summaries (top 5 by likelihood)
    hypothesis_summaries = []
    if case.hypotheses:
        sorted_hypotheses = sorted(
            case.hypotheses.values(),
            key=lambda h: h.likelihood,
            reverse=True
        )[:5]

        for hyp in sorted_hypotheses:
            hypothesis_summaries.append(HypothesisSummary(
                hypothesis_id=hyp.hypothesis_id,
                text=hyp.description,
                likelihood=hyp.likelihood,
                status=hyp.status,
                evidence_count=len(hyp.evidence_links)
            ))

    # Build evidence summaries (last 5 evidence items)
    evidence_summaries = []
    if case.evidence:
        sorted_evidence = sorted(
            case.evidence,
            key=lambda e: e.collected_at,
            reverse=True
        )[:5]

        for ev in sorted_evidence:
            evidence_summaries.append(EvidenceSummary(
                evidence_id=ev.evidence_id,
                type=ev.evidence_type.value,
                summary=ev.summary,
                timestamp=ev.collected_at,
                relevance_score=0.8  # Could compute from hypothesis links
            ))

    # Agent status message
    agent_status = f"Working on {case.progress.current_stage.value.replace('_', ' ')}"
    if case.degraded_mode and case.degraded_mode.is_active:
        agent_status = "Investigation appears stuck - reviewing alternative approaches"

    # Next actions (from pending milestones)
    next_actions = []
    pending = case.progress.pending_milestones[:3]  # First 3 pending
    for milestone_id in pending:
        # Convert milestone ID to action description
        action = milestone_id.replace('_', ' ').title()
        next_actions.append(action)

    # Extract investigation strategy and problem verification
    investigation_strategy_data = _get_investigation_strategy_data(case)
    problem_verification_data = _extract_problem_verification(case)

    return CaseUIResponse_Investigating(
        case_id=case.case_id,
        status=CaseStatus.INVESTIGATING,
        title=case.title,
        created_at=case.created_at,
        updated_at=case.updated_at,
        working_conclusion=working_conclusion,
        progress=progress,
        active_hypotheses=hypothesis_summaries,
        latest_evidence=evidence_summaries,
        next_actions=next_actions,
        agent_status=agent_status,
        is_stuck=case.is_stuck,
        degraded_mode=case.degraded_mode is not None and case.degraded_mode.is_active,
        investigation_strategy=investigation_strategy_data,
        problem_verification=problem_verification_data
    )


def _transform_resolved(case: Case) -> CaseUIResponse_Resolved:
    """Transform case into RESOLVED phase UI response."""

    # Extract root cause from conclusion
    root_cause_desc = "Root cause identified"
    root_cause_id = "unknown"
    root_cause_category = "other"
    root_cause_severity = "medium"

    if case.root_cause_conclusion:
        root_cause_desc = case.root_cause_conclusion.root_cause
        # Extract category from validated hypothesis if available
        if case.hypotheses:
            # Find the hypothesis that was marked as VALIDATED
            for hyp in case.hypotheses.values():
                if hyp.status == HypothesisStatus.VALIDATED:
                    root_cause_id = hyp.hypothesis_id
                    root_cause_category = hyp.category.value
                    break

    root_cause = RootCauseSummary(
        description=root_cause_desc,
        root_cause_id=root_cause_id,
        category=root_cause_category,
        severity=root_cause_severity
    )

    # Extract solution from case (if solutions tracked)
    solution_desc = "Solution applied and verified"
    solution_applied_at = case.resolved_at if case.resolved_at else case.updated_at
    solution_applied_by = case.user_id

    # Get solution description from solutions list if available
    if case.solutions:
        applied_solutions = [s for s in case.solutions if s.applied_at is not None]
        if applied_solutions:
            # Use the most recent applied solution
            latest_solution = max(applied_solutions, key=lambda s: s.applied_at)
            if latest_solution.immediate_action:
                solution_desc = latest_solution.immediate_action
            elif latest_solution.longterm_fix:
                solution_desc = latest_solution.longterm_fix
            solution_applied_at = latest_solution.applied_at
            solution_applied_by = latest_solution.applied_by or case.user_id

    solution_applied = SolutionSummary(
        description=solution_desc,
        applied_at=solution_applied_at,
        applied_by=solution_applied_by
    )

    # Verification status
    verification_status = VerificationStatus(
        verified=True,  # Assumed if case is RESOLVED
        verification_method="Post-resolution monitoring",
        details="Solution applied and verified effective"
    )

    # Calculate duration
    duration_minutes = 0
    if case.resolved_at:
        duration = case.resolved_at - case.created_at
        duration_minutes = int(duration.total_seconds() / 60)

    # Resolution summary
    key_insights = []
    if case.root_cause_conclusion:
        # Extract contributing factors as key insights if available
        key_insights = case.root_cause_conclusion.contributing_factors[:5]  # Top 5

    resolution_summary = ResolutionSummary(
        total_duration_minutes=duration_minutes,
        milestones_completed=len(case.progress.completed_milestones),
        hypotheses_tested=len([h for h in case.hypotheses.values() if h.status != HypothesisStatus.CAPTURED]),
        evidence_collected=len(case.evidence),
        key_insights=key_insights
    )

    # Report availability
    reports_available = [
        ReportAvailability(
            report_type="incident_report",
            status="recommended",
            reason="Standard incident documentation"
        ),
        ReportAvailability(
            report_type="post_mortem",
            status="recommended",
            reason="Detailed analysis for future reference"
        ),
        ReportAvailability(
            report_type="timeline",
            status="available",
            reason="Investigation timeline reconstructed"
        )
    ]

    return CaseUIResponse_Resolved(
        case_id=case.case_id,
        status=CaseStatus.RESOLVED,
        title=case.title,
        created_at=case.created_at,
        updated_at=case.updated_at,
        resolved_at=case.resolved_at if case.resolved_at else case.updated_at,
        root_cause=root_cause,
        solution_applied=solution_applied,
        verification_status=verification_status,
        resolution_summary=resolution_summary,
        reports_available=reports_available
    )
