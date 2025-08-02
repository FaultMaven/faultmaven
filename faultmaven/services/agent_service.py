"""Agent Service Module

Purpose: Orchestrates agent operations and workflows

This service acts as the primary interface for all agent-related operations,
managing the lifecycle of troubleshooting investigations and coordinating
between the core agent and other system components.

Core Responsibilities:
- Agent lifecycle management
- Query processing orchestration
- Investigation state management
- Result aggregation and formatting
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.core.agent.agent import FaultMavenAgent
from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool
from faultmaven.models import AgentState, QueryRequest, TroubleshootingResponse
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer


class AgentService:
    """Service for orchestrating agent operations and troubleshooting workflows"""

    def __init__(
        self,
        core_agent: FaultMavenAgent,
        data_sanitizer: DataSanitizer,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Agent Service

        Args:
            core_agent: The core FaultMaven agent instance
            data_sanitizer: Data sanitization service
            logger: Optional logger instance
        """
        self.core_agent = core_agent
        self.data_sanitizer = data_sanitizer
        self.logger = logger or logging.getLogger(__name__)

    @trace("agent_service_process_query")
    async def process_query(
        self,
        query: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
    ) -> TroubleshootingResponse:
        """
        Process a troubleshooting query through the agent

        Args:
            query: The user's troubleshooting query
            session_id: Session identifier
            context: Optional context for the query
            priority: Query priority level

        Returns:
            TroubleshootingResponse with analysis results

        Raises:
            ValueError: If query validation fails
            RuntimeError: If agent processing fails
        """
        self.logger.info(
            f"Processing query for session {session_id} with priority {priority}"
        )

        # Validate and sanitize input
        if not query or not query.strip():
            raise ValueError("Query cannot be empty")

        sanitized_query = self.data_sanitizer.sanitize(query)

        # Generate investigation ID
        investigation_id = str(uuid.uuid4())
        self.logger.debug(f"Created investigation {investigation_id}")

        try:
            # Process through core agent
            start_time = datetime.utcnow()

            agent_response = await self.core_agent.process_query(
                query=sanitized_query,
                session_id=session_id,
                context=context or {},
                priority=priority,
            )

            end_time = datetime.utcnow()

            # Format and validate response
            response = self._format_agent_response(
                agent_response=agent_response,
                session_id=session_id,
                investigation_id=investigation_id,
                start_time=start_time,
                end_time=end_time,
            )

            self.logger.info(
                f"Successfully processed investigation {investigation_id} "
                f"with confidence {response.confidence_score}"
            )

            return response

        except Exception as e:
            self.logger.error(
                f"Agent processing failed for investigation {investigation_id}: {e}"
            )
            raise RuntimeError(f"Agent processing failed: {str(e)}") from e

    @trace("agent_service_analyze_findings")
    async def analyze_findings(
        self, findings: List[Dict[str, Any]], session_id: str
    ) -> Dict[str, Any]:
        """
        Perform deep analysis on investigation findings

        Args:
            findings: List of findings to analyze
            session_id: Session identifier

        Returns:
            Analysis results with patterns and correlations
        """
        self.logger.debug(f"Analyzing {len(findings)} findings for session {session_id}")

        try:
            # Group findings by type
            findings_by_type = self._group_findings_by_type(findings)

            # Identify patterns
            patterns = await self._identify_patterns(findings_by_type)

            # Calculate severity distribution
            severity_dist = self._calculate_severity_distribution(findings)

            # Generate insights
            insights = {
                "total_findings": len(findings),
                "findings_by_type": {
                    type_name: len(items) for type_name, items in findings_by_type.items()
                },
                "severity_distribution": severity_dist,
                "patterns_identified": patterns,
                "critical_issues": self._extract_critical_issues(findings),
            }

            return insights

        except Exception as e:
            self.logger.error(f"Failed to analyze findings: {e}")
            raise

    @trace("agent_service_get_agent_state")
    async def get_agent_state(self, session_id: str) -> Optional[AgentState]:
        """
        Get the current agent state for a session

        Args:
            session_id: Session identifier

        Returns:
            Current agent state or None if not found
        """
        try:
            # In a real implementation, this would retrieve from persistent storage
            # For now, we'll return a placeholder
            return None
        except Exception as e:
            self.logger.error(f"Failed to get agent state for session {session_id}: {e}")
            raise

    @trace("agent_service_update_agent_state")
    async def update_agent_state(
        self, session_id: str, state_updates: Dict[str, Any]
    ) -> bool:
        """
        Update the agent state for a session

        Args:
            session_id: Session identifier
            state_updates: State updates to apply

        Returns:
            True if update was successful
        """
        try:
            # In a real implementation, this would persist to storage
            self.logger.info(f"Updated agent state for session {session_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to update agent state: {e}")
            return False

    def _format_agent_response(
        self,
        agent_response: Dict[str, Any],
        session_id: str,
        investigation_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> TroubleshootingResponse:
        """
        Format agent response into standardized TroubleshootingResponse

        Args:
            agent_response: Raw response from agent
            session_id: Session identifier
            investigation_id: Investigation identifier
            start_time: Investigation start time
            end_time: Investigation end time

        Returns:
            Formatted TroubleshootingResponse
        """
        # Ensure findings are properly formatted
        findings = agent_response.get("findings", [])
        if not isinstance(findings, list):
            findings = []

        # Format each finding
        formatted_findings = []
        for finding in findings:
            if isinstance(finding, dict):
                formatted_findings.append(finding)
            else:
                # Convert non-dict findings to proper format
                formatted_findings.append(
                    {
                        "type": "general",
                        "message": str(finding),
                        "severity": "info",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )

        return TroubleshootingResponse(
            session_id=session_id,
            investigation_id=investigation_id,
            status="completed",
            findings=formatted_findings,
            root_cause=agent_response.get("root_cause"),
            recommendations=agent_response.get("recommendations", []),
            confidence_score=agent_response.get("confidence_score", 0.5),
            estimated_mttr=agent_response.get("estimated_mttr"),
            next_steps=agent_response.get("next_steps", []),
            created_at=start_time,
            completed_at=end_time,
        )

    def _group_findings_by_type(
        self, findings: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group findings by their type"""
        grouped = {}
        for finding in findings:
            finding_type = finding.get("type", "unknown")
            if finding_type not in grouped:
                grouped[finding_type] = []
            grouped[finding_type].append(finding)
        return grouped

    async def _identify_patterns(
        self, findings_by_type: Dict[str, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """Identify patterns in grouped findings"""
        patterns = []

        # Check for error clustering
        if "error" in findings_by_type and len(findings_by_type["error"]) > 3:
            patterns.append(
                {
                    "pattern": "error_clustering",
                    "description": "Multiple errors detected in close proximity",
                    "count": len(findings_by_type["error"]),
                }
            )

        # Check for performance degradation
        if "performance" in findings_by_type:
            patterns.append(
                {
                    "pattern": "performance_issues",
                    "description": "Performance-related findings detected",
                    "count": len(findings_by_type["performance"]),
                }
            )

        return patterns

    def _calculate_severity_distribution(
        self, findings: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """Calculate distribution of findings by severity"""
        distribution = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}

        for finding in findings:
            severity = finding.get("severity", "info").lower()
            if severity in distribution:
                distribution[severity] += 1

        return distribution

    def _extract_critical_issues(
        self, findings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract critical issues from findings"""
        critical_issues = []

        for finding in findings:
            if finding.get("severity", "").lower() in ["critical", "high"]:
                critical_issues.append(
                    {
                        "type": finding.get("type"),
                        "message": finding.get("message"),
                        "severity": finding.get("severity"),
                        "source": finding.get("source"),
                    }
                )

        return critical_issues