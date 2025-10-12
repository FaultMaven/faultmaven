"""Document Handler - Phase 6: Artifact Generation and Knowledge Capture

Phase 6 Characteristics:
- Engagement Mode: Lead Investigator
- OODA Steps: Orient only (synthesis mode)
- Expected Iterations: 1
- Generates: Case report, runbook, knowledge base entries

Objectives:
- Synthesize investigation into case report
- Generate runbook for future incidents
- Capture key insights for knowledge base
- Complete investigation lifecycle

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import List, Dict, Any
from datetime import datetime

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
)
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt


class DocumentHandler(BasePhaseHandler):
    """Phase 6: Document - Generate artifacts and capture knowledge

    Responsibilities:
    - Synthesize investigation into case report
    - Generate runbook with reproduction steps
    - Extract insights for knowledge base
    - Offer artifacts to user (accept/decline)
    - Mark investigation complete
    """

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 6: Document"""
        return InvestigationPhase.DOCUMENT

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
    ) -> PhaseHandlerResult:
        """Handle Phase 6: Document

        Flow:
        1. Check if artifacts already offered
        2. Execute Orient (synthesis) step
        3. Check if user wants artifacts
        4. Generate and deliver if requested
        5. Mark investigation complete

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            conversation_history: Recent conversation

        Returns:
            PhaseHandlerResult with artifacts and completion status
        """
        # Check if we've already offered artifacts
        if investigation_state.lifecycle.artifacts_offered:
            return await self._handle_artifact_response(
                investigation_state, user_query, conversation_history
            )

        # Execute Orient step - synthesize investigation
        return await self._execute_orient(investigation_state, user_query, conversation_history)

    async def _execute_orient(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Orient: Synthesize investigation and offer artifacts

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult offering artifacts
        """
        # Start new OODA iteration if needed
        if not investigation_state.ooda_engine.iterations or \
           investigation_state.ooda_engine.iterations[-1].steps_completed:
            iteration = self.start_new_ooda_iteration(investigation_state)
            investigation_state.ooda_engine.iterations.append(iteration)

        # Gather investigation summary for artifact generation
        investigation_summary = self._synthesize_investigation(investigation_state)

        # Generate offer prompt
        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=investigation_summary,
            conversation_history=conversation_history,
            user_query=user_query,
        )

        from faultmaven.models.responses import LeadInvestigatorResponse

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            max_tokens=400,
            expected_schema=LeadInvestigatorResponse,
        )

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn

        # Mark artifacts offered
        investigation_state.lifecycle.artifacts_offered = True

        self.log_phase_action("Orient step complete", {"artifacts_offered": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            iteration_complete=True,
            made_progress=True,
        )

    async def _handle_artifact_response(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Handle user response to artifact offer

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with artifacts or completion
        """
        # Detect if user wants artifacts
        wants_artifacts = self._detect_artifact_acceptance(user_query)

        if wants_artifacts:
            # Generate artifacts
            artifacts = await self._generate_artifacts(investigation_state)

            # Format artifacts for delivery
            artifact_text = self._format_artifacts_for_delivery(artifacts)

            response = f"{artifact_text}\n\nInvestigation complete! These artifacts have been saved."

            # Mark phase complete
            investigation_state.lifecycle.case_status = "documented"

            self.log_phase_action("Artifacts generated", {"artifact_count": len(artifacts)})

            return PhaseHandlerResult(
                response_text=structured_response.answer,
            structured_response=structured_response,
                updated_state=investigation_state,
                phase_complete=True,
                should_advance=False,  # No next phase - investigation done
                next_phase=None,
                made_progress=True,
            )
        else:
            # User declined artifacts
            response = "No problem! Investigation complete. Feel free to reach out if you need anything else."

            investigation_state.lifecycle.case_status = "completed"

            self.log_phase_action("Artifacts declined", {"user_declined": True})

            return PhaseHandlerResult(
                response_text=structured_response.answer,
            structured_response=structured_response,
                updated_state=investigation_state,
                phase_complete=True,
                should_advance=False,
                next_phase=None,
                made_progress=True,
            )

    async def _generate_artifacts(
        self,
        investigation_state: InvestigationState,
    ) -> List[Dict[str, Any]]:
        """Generate case report and runbook artifacts

        Args:
            investigation_state: Investigation state

        Returns:
            List of artifact dictionaries
        """
        artifacts = []

        # Artifact 1: Case Report
        case_report = await self._generate_case_report(investigation_state)
        artifacts.append({
            "type": "case_report",
            "title": "Investigation Case Report",
            "content": case_report,
            "generated_at": datetime.utcnow().isoformat(),
        })

        # Artifact 2: Runbook
        runbook = await self._generate_runbook(investigation_state)
        artifacts.append({
            "type": "runbook",
            "title": "Incident Response Runbook",
            "content": runbook,
            "generated_at": datetime.utcnow().isoformat(),
        })

        # Artifact 3: Knowledge Base Insights (optional)
        if investigation_state.ooda_engine.hypotheses:
            kb_insights = self._extract_kb_insights(investigation_state)
            if kb_insights:
                artifacts.append({
                    "type": "knowledge_insights",
                    "title": "Key Insights for Knowledge Base",
                    "content": kb_insights,
                    "generated_at": datetime.utcnow().isoformat(),
                })

        return artifacts

    async def _generate_case_report(
        self,
        investigation_state: InvestigationState,
    ) -> str:
        """Generate structured case report

        Args:
            investigation_state: Investigation state

        Returns:
            Formatted case report
        """
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        hypothesis_manager = create_hypothesis_manager()

        # Get validated hypothesis
        validated = hypothesis_manager.get_validated_hypothesis(
            investigation_state.ooda_engine.hypotheses
        )

        anomaly_frame = investigation_state.ooda_engine.anomaly_frame

        report = f"""# Investigation Case Report

## Problem Summary
**Statement:** {anomaly_frame.statement if anomaly_frame else 'N/A'}
**Severity:** {anomaly_frame.severity if anomaly_frame else 'N/A'}
**Affected Components:** {', '.join(anomaly_frame.affected_components) if anomaly_frame else 'N/A'}
**Started At:** {anomaly_frame.started_at.isoformat() if anomaly_frame and anomaly_frame.started_at else 'N/A'}

## Investigation Timeline
**Strategy:** {investigation_state.lifecycle.investigation_strategy.value}
**Total Turns:** {investigation_state.metadata.current_turn}
**OODA Iterations:** {investigation_state.ooda_engine.current_iteration}
**Phases Completed:** {investigation_state.lifecycle.current_phase.value + 1}/7

## Root Cause
**Validated Hypothesis:** {validated.statement if validated else 'No hypothesis validated'}
**Confidence:** {validated.likelihood:.0%} if validated else 'N/A'
**Category:** {validated.category if validated else 'N/A'}

## Solution Applied
**Status:** {investigation_state.lifecycle.case_status}
**Solution Verified:** {'Yes' if investigation_state.lifecycle.case_status == 'resolved' else 'Pending'}

## Key Evidence Collected
**Total Evidence Requests:** {len(investigation_state.evidence.evidence_requests)}
**Coverage Assessment:** Phase-appropriate evidence gathered

## Lessons Learned
"""

        # Add insights from memory
        if investigation_state.memory.persistent_insights:
            report += "\n### Persistent Insights\n"
            for insight in investigation_state.memory.persistent_insights:
                report += f"- {insight}\n"

        return report

    async def _generate_runbook(
        self,
        investigation_state: InvestigationState,
    ) -> str:
        """Generate incident response runbook

        Args:
            investigation_state: Investigation state

        Returns:
            Formatted runbook
        """
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        hypothesis_manager = create_hypothesis_manager()

        validated = hypothesis_manager.get_validated_hypothesis(
            investigation_state.ooda_engine.hypotheses
        )

        anomaly_frame = investigation_state.ooda_engine.anomaly_frame

        runbook = f"""# Incident Response Runbook

## Incident Type
**Problem:** {anomaly_frame.statement if anomaly_frame else 'N/A'}
**Category:** {validated.category if validated else 'Unknown'}

## Detection
**Symptoms to Watch:**
"""

        if anomaly_frame and anomaly_frame.affected_components:
            runbook += f"- Monitor these components: {', '.join(anomaly_frame.affected_components)}\n"

        runbook += f"""
**Severity Indicators:**
- Impact scope: {anomaly_frame.affected_scope if anomaly_frame else 'N/A'}
- Severity level: {anomaly_frame.severity if anomaly_frame else 'N/A'}

## Diagnosis Steps
1. Check affected components for similar symptoms
2. Review recent changes (deployments, config, infrastructure)
3. Examine timeline correlation between changes and problem onset

## Root Cause
**Most Likely Cause:** {validated.statement if validated else 'Refer to investigation'}
**Confidence Level:** {validated.likelihood:.0%} if validated else 'N/A'

## Remediation Steps
1. Verify symptoms match this runbook
2. Apply tested solution from investigation
3. Monitor error rates and affected components
4. Confirm resolution with metrics

## Prevention
"""

        if validated and validated.category == "code":
            runbook += "- Add regression tests for this scenario\n"
            runbook += "- Review deployment checklist\n"
        elif validated and validated.category == "config":
            runbook += "- Implement configuration validation\n"
            runbook += "- Add config change approval workflow\n"
        elif validated and validated.category == "infrastructure":
            runbook += "- Set up capacity alerts\n"
            runbook += "- Review infrastructure scaling policies\n"

        runbook += f"""
## Escalation
- If symptoms persist after remediation: Escalate to senior engineer
- If impact scope increases: Notify incident commander

---
*Generated from investigation on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*
"""

        return runbook

    def _extract_kb_insights(
        self,
        investigation_state: InvestigationState,
    ) -> str:
        """Extract key insights for knowledge base

        Args:
            investigation_state: Investigation state

        Returns:
            Formatted insights
        """
        insights = "# Key Insights for Knowledge Base\n\n"

        # Add validated hypothesis
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        hypothesis_manager = create_hypothesis_manager()
        validated = hypothesis_manager.get_validated_hypothesis(
            investigation_state.ooda_engine.hypotheses
        )

        if validated:
            insights += f"## Validated Root Cause\n"
            insights += f"**Statement:** {validated.statement}\n"
            insights += f"**Category:** {validated.category}\n"
            insights += f"**Confidence:** {validated.likelihood:.0%}\n\n"

        # Add persistent insights from memory
        if investigation_state.memory.persistent_insights:
            insights += "## Persistent Investigation Insights\n"
            for insight in investigation_state.memory.persistent_insights:
                insights += f"- {insight}\n"

        # Add anchoring events if any
        if investigation_state.ooda_engine.anchoring_detected:
            insights += f"\n## Investigation Notes\n"
            insights += f"- Anchoring bias detected and corrected during investigation\n"

        return insights

    def _synthesize_investigation(
        self,
        investigation_state: InvestigationState,
    ) -> Dict[str, Any]:
        """Synthesize investigation for artifact generation context

        Args:
            investigation_state: Investigation state

        Returns:
            Dictionary with investigation summary
        """
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        hypothesis_manager = create_hypothesis_manager()

        validated = hypothesis_manager.get_validated_hypothesis(
            investigation_state.ooda_engine.hypotheses
        )

        return {
            "case_status": investigation_state.lifecycle.case_status,
            "total_turns": investigation_state.metadata.current_turn,
            "ooda_iterations": investigation_state.ooda_engine.current_iteration,
            "phases_completed": investigation_state.lifecycle.current_phase.value + 1,
            "root_cause": validated.statement if validated else None,
            "root_cause_confidence": validated.likelihood if validated else None,
            "anomaly_frame": investigation_state.ooda_engine.anomaly_frame.dict() if investigation_state.ooda_engine.anomaly_frame else None,
        }

    def _detect_artifact_acceptance(self, user_query: str) -> bool:
        """Detect if user wants artifacts

        Args:
            user_query: User query

        Returns:
            True if user wants artifacts
        """
        query_lower = user_query.lower()

        acceptance_signals = [
            "yes", "sure", "please", "ok", "okay", "yeah",
            "go ahead", "sounds good", "that would be great",
            "i'd like", "would love", "generate", "create"
        ]

        rejection_signals = [
            "no", "not now", "skip", "no thanks", "not needed",
            "don't need", "later", "maybe later", "pass"
        ]

        # Check rejection first (higher priority)
        for signal in rejection_signals:
            if signal in query_lower:
                return False

        # Check acceptance
        for signal in acceptance_signals:
            if signal in query_lower:
                return True

        # Default to acceptance if ambiguous (artifacts are helpful)
        return True

    def _format_artifacts_for_delivery(
        self,
        artifacts: List[Dict[str, Any]],
    ) -> str:
        """Format artifacts for user delivery

        Args:
            artifacts: List of artifact dictionaries

        Returns:
            Formatted artifact text
        """
        output = "# Investigation Artifacts\n\n"

        for artifact in artifacts:
            output += f"## {artifact['title']}\n\n"
            output += f"{artifact['content']}\n\n"
            output += "---\n\n"

        return output

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 6 completion criteria met

        Criteria:
        - Artifacts offered
        - User response received (accept or decline)
        - Investigation marked complete

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        # Check if artifacts offered
        if investigation_state.lifecycle.artifacts_offered:
            met_criteria.append("Artifacts offered to user")
        else:
            unmet_criteria.append("Artifacts not yet offered")

        # Check case status
        if investigation_state.lifecycle.case_status in ["documented", "completed"]:
            met_criteria.append("Investigation complete")
        else:
            unmet_criteria.append("Investigation not yet complete")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state: InvestigationState) -> dict:
        """Format state for prompt context"""
        return self._synthesize_investigation(state)
