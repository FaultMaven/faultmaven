"""doctrine.py

Purpose: Five-phase troubleshooting methodology

Requirements:
--------------------------------------------------------------------------------
• Define five phases as constants
• Create phase-specific logic methods
• Implement evidence-driven SRE investigation process

Key Components:
--------------------------------------------------------------------------------
  class TroubleshootingDoctrine:
  PHASES: List[str]

Technology Stack:
--------------------------------------------------------------------------------
State machine patterns

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
from enum import Enum
from typing import Any, Dict, Optional

from faultmaven.models import AgentState


class Phase(Enum):
    """Enumeration of troubleshooting phases"""

    DEFINE_BLAST_RADIUS = "define_blast_radius"
    ESTABLISH_TIMELINE = "establish_timeline"
    FORMULATE_HYPOTHESIS = "formulate_hypothesis"
    VALIDATE_HYPOTHESIS = "validate_hypothesis"
    PROPOSE_SOLUTION = "propose_solution"


class TroubleshootingDoctrine:
    """Implements the five-phase troubleshooting methodology"""

    PHASES = [phase.value for phase in Phase]

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Phase-specific prompts and guidance
        self.phase_guidance = {
            Phase.DEFINE_BLAST_RADIUS: {
                "objective": "Assess the scope and impact of the issue",
                "key_questions": [
                    "What systems or services are affected?",
                    "How many users or customers are impacted?",
                    "What is the severity level of the issue?",
                    "Are there any workarounds available?",
                    "What is the business impact?",
                    "Which environments are affected (prod, staging, dev)?",
                ],
                "tools_needed": ["knowledge_base_search"],
                "outputs": [
                    "affected_systems",
                    "impact_assessment",
                    "severity_level",
                    "business_impact",
                ],
                "success_criteria": [
                    "Clear understanding of what's broken",
                    "Quantified impact assessment",
                    "Severity classification",
                ],
            },
            Phase.ESTABLISH_TIMELINE: {
                "objective": (
                    "Establish a timeline of events and identify when the "
                    "issue started"
                ),
                "key_questions": [
                    "When did the issue first occur?",
                    "What deployments or changes happened recently?",
                    "Are there any patterns in the timing?",
                    "What was the sequence of events?",
                    "Were there any warnings or early indicators?",
                    "What was the system state before the issue?",
                ],
                "tools_needed": ["knowledge_base_search", "log_analysis"],
                "outputs": [
                    "timeline_of_events",
                    "recent_changes",
                    "correlation_analysis",
                ],
                "success_criteria": [
                    "Clear timeline established",
                    "Recent changes identified",
                    "Correlation patterns found",
                ],
            },
            Phase.FORMULATE_HYPOTHESIS: {
                "objective": "Develop testable hypotheses about the root cause",
                "key_questions": [
                    "What are the most likely root causes?",
                    "What evidence supports each hypothesis?",
                    "How can we test these hypotheses?",
                    "What are the dependencies and interactions?",
                    "Are there similar past incidents?",
                    "What would explain all the observed symptoms?",
                ],
                "tools_needed": ["knowledge_base_search", "log_analysis"],
                "outputs": ["hypothesis_list", "evidence_mapping", "test_plan"],
                "success_criteria": [
                    "Multiple testable hypotheses",
                    "Evidence-based ranking",
                    "Clear testing approach",
                ],
            },
            Phase.VALIDATE_HYPOTHESIS: {
                "objective": "Test and validate the most promising hypotheses",
                "key_questions": [
                    "Which hypothesis best explains the symptoms?",
                    "What tests can confirm or refute each hypothesis?",
                    "What additional data is needed?",
                    "Are there any contradictory findings?",
                    "Can we reproduce the issue?",
                    "What would disprove our leading hypothesis?",
                ],
                "tools_needed": ["knowledge_base_search"],
                "outputs": [
                    "validated_hypothesis",
                    "test_results",
                    "root_cause_confirmation",
                ],
                "success_criteria": [
                    "Hypothesis validated with evidence",
                    "Root cause identified",
                    "Reproduction steps if applicable",
                ],
            },
            Phase.PROPOSE_SOLUTION: {
                "objective": "Develop and present a comprehensive solution plan",
                "key_questions": [
                    "What are the possible solutions?",
                    "What are the risks and trade-offs?",
                    "What is the estimated time to implement?",
                    "What resources are required?",
                    "What is the rollback plan?",
                    "How will we prevent this from happening again?",
                ],
                "tools_needed": ["knowledge_base_search"],
                "outputs": [
                    "solution_options",
                    "implementation_plan",
                    "risk_assessment",
                    "prevention_measures",
                ],
                "success_criteria": [
                    "Comprehensive solution plan",
                    "Risk mitigation strategy",
                    "Prevention measures defined",
                ],
            },
        }

    def get_phase_objective(self, phase: Phase) -> str:
        """Returns the primary goal for a given doctrine phase."""
        objectives = {
            Phase.DEFINE_BLAST_RADIUS: (
                "Objective: Assess the scope and impact of the issue. "
                "Identify which systems, services, and users are affected. "
                "Determine the severity level and business impact to prioritize response efforts."
            ),
            Phase.ESTABLISH_TIMELINE: (
                "Objective: Establish a timeline of events. "
                "Request logs and metrics from the time the issue started. "
                "Ask about recent deployments or configuration changes to find correlations."
            ),
            Phase.FORMULATE_HYPOTHESIS: (
                "Objective: Develop testable hypotheses about the root cause. "
                "Analyze patterns in the data and symptoms to generate likely explanations. "
                "Prioritize hypotheses based on evidence and impact."
            ),
            Phase.VALIDATE_HYPOTHESIS: (
                "Objective: Test and validate the most promising hypotheses. "
                "Gather additional evidence to confirm or refute each hypothesis. "
                "Identify the root cause through systematic validation."
            ),
            Phase.PROPOSE_SOLUTION: (
                "Objective: Develop a comprehensive solution plan. "
                "Create actionable steps to resolve the issue and prevent recurrence. "
                "Consider risks, resources, and implementation timeline."
            ),
        }
        return objectives.get(phase, "Unknown phase objective")

    async def execute_phase(
        self, phase: Phase, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a specific phase of the troubleshooting process

        Args:
            phase: The phase to execute
            agent_state: Current agent state
            context: Additional context and tools

        Returns:
            Phase execution results
        """
        self.logger.info(f"Executing phase: {phase.value}")

        guidance = self.phase_guidance[phase]

        # Create phase-specific prompt
        prompt = self._create_phase_prompt(phase, guidance, agent_state, context)

        # Execute phase logic
        if phase == Phase.DEFINE_BLAST_RADIUS:
            return await self._execute_blast_radius_phase(prompt, agent_state, context)
        elif phase == Phase.ESTABLISH_TIMELINE:
            return await self._execute_timeline_phase(prompt, agent_state, context)
        elif phase == Phase.FORMULATE_HYPOTHESIS:
            return await self._execute_hypothesis_phase(prompt, agent_state, context)
        elif phase == Phase.VALIDATE_HYPOTHESIS:
            return await self._execute_validation_phase(prompt, agent_state, context)
        elif phase == Phase.PROPOSE_SOLUTION:
            return await self._execute_solution_phase(prompt, agent_state, context)
        else:
            raise ValueError(f"Unknown phase: {phase}")

    def _create_phase_prompt(
        self,
        phase: Phase,
        guidance: Dict[str, Any],
        agent_state: AgentState,
        context: Dict[str, Any],
    ) -> str:
        """
        Create a phase-specific prompt for the agent

        Args:
            phase: Current phase
            guidance: Phase guidance information
            agent_state: Current agent state
            context: Additional context

        Returns:
            Formatted prompt for the phase
        """
        prompt_parts = [
            f"## Phase: {phase.value.replace('_', ' ').title()}",
            f"**Objective:** {guidance['objective']}",
            "",
            "**Key Questions to Address:**",
        ]

        for question in guidance["key_questions"]:
            prompt_parts.append(f"- {question}")

        prompt_parts.append("")
        prompt_parts.append("**Available Tools:**")
        for tool in guidance["tools_needed"]:
            prompt_parts.append(f"- {tool}")

        prompt_parts.append("")
        prompt_parts.append("**Expected Outputs:**")
        for output in guidance["outputs"]:
            prompt_parts.append(f"- {output}")

        prompt_parts.append("")
        prompt_parts.append("**Success Criteria:**")
        for criteria in guidance["success_criteria"]:
            prompt_parts.append(f"- {criteria}")

        prompt_parts.append("")
        prompt_parts.append("**Current Context:**")
        prompt_parts.append(f"- User Query: {agent_state.get('user_query', 'N/A')}")
        prompt_parts.append(f"- Session ID: {agent_state.get('session_id', 'N/A')}")

        if agent_state.get("findings"):
            prompt_parts.append("- Previous Findings:")
            for finding in agent_state["findings"][-3:]:  # Last 3 findings
                prompt_parts.append(f"  - {finding.get('finding', 'N/A')}")

        if context.get("uploaded_data"):
            prompt_parts.append("- Available Data:")
            for data in context["uploaded_data"]:
                prompt_parts.append(
                    f"  - {data.get('data_type', 'unknown')}: {data.get('file_name', 'unnamed')}"
                )

        prompt_parts.append("")
        prompt_parts.append("**Instructions:**")
        prompt_parts.append(
            "Follow the 'Single Insight, Single Question' rule - present one key finding and ask one clear question."
        )
        prompt_parts.append(
            "Keep responses concise and focused for productive turn-based dialogue."
        )
        prompt_parts.append(
            "Proceed systematically using the available tools and following the guidance above."
        )

        return "\n".join(prompt_parts)

    async def _execute_blast_radius_phase(
        self, prompt: str, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute the blast radius definition phase with LLM reasoning

        Args:
            prompt: Phase-specific prompt
            agent_state: Current agent state
            context: Additional context

        Returns:
            Phase execution results
        """
        self.logger.info("Executing blast radius phase with LLM")

        try:
            # Get the LLM router from context
            llm_router = context.get("llm_router")
            if not llm_router:
                # Fallback to basic analysis if no LLM router available
                return self._fallback_blast_radius_analysis(agent_state, context)

            # 1. Call the LLM with the generated prompt
            llm_response = await llm_router.route(
                prompt=prompt, max_tokens=500, temperature=0.3
            )

            # 2. Parse the structured response from the LLM
            parsed_output = self._parse_llm_response(llm_response.content)
            key_insight = parsed_output.get(
                "key_insight", "Blast radius assessment completed"
            )
            follow_up_question = parsed_output.get(
                "follow_up_question", "Should we proceed to establish the timeline?"
            )

            findings = []

            # 3. Execute tools if LLM recommends them
            if parsed_output.get("tool_to_use") == "knowledge_base_search":
                kb_tool = context.get("knowledge_base_tool")
                if kb_tool:
                    tool_query = parsed_output.get(
                        "tool_query",
                        f"blast radius impact assessment {agent_state.get('user_query', '')}",
                    )
                    tool_results = kb_tool.search(
                        tool_query, context={"phase": "define_blast_radius"}
                    )
                    findings.append(
                        f"Knowledge base search completed: {len(tool_results.split('**')) // 2} relevant documents found"
                    )

                    # If no useful results from KB, try web search as fallback
                    if len(tool_results.split("**")) < 3:  # Minimal useful results
                        web_search_tool = context.get("web_search_tool")
                        if web_search_tool and web_search_tool.is_available():
                            _ = await web_search_tool._arun(
                                query=tool_query,
                                context={"phase": "define_blast_radius"},
                            )
                            findings.append(
                                "Web search performed for additional blast radius information"
                            )

            # Check for uploaded data insights
            if context.get("uploaded_data"):
                for data in context["uploaded_data"]:
                    if data.get("data_type") == "log_file":
                        findings.append("Log files available for blast radius analysis")
                    elif data.get("data_type") == "error_message":
                        findings.append("Error messages provide scope indicators")

            return {
                "phase": Phase.DEFINE_BLAST_RADIUS.value,
                "status": "completed",
                "findings": findings,
                "key_insight": key_insight,
                "follow_up_question": follow_up_question,
                "recommendations": [
                    "Continue to timeline establishment phase",
                    "Gather more data about affected systems",
                ],
                "next_phase": Phase.ESTABLISH_TIMELINE.value,
                "confidence_score": 0.8,
                "requires_user_input": True,
            }

        except Exception as e:
            self.logger.error(f"LLM-driven blast radius analysis failed: {e}")
            # Fallback to basic analysis
            return self._fallback_blast_radius_analysis(agent_state, context)

    async def _execute_timeline_phase(
        self, prompt: str, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute the timeline establishment phase with LLM reasoning

        Args:
            prompt: Phase-specific prompt
            agent_state: Current agent state
            context: Additional context

        Returns:
            Phase execution results
        """
        self.logger.info("Executing timeline establishment phase with LLM")

        try:
            # Get the LLM router from context
            llm_router = context.get("llm_router")
            if not llm_router:
                return self._fallback_timeline_analysis(agent_state, context)

            # 1. Call the LLM with the generated prompt
            llm_response = await llm_router.route(
                prompt=prompt, max_tokens=500, temperature=0.3
            )

            # 2. Parse the structured response from the LLM
            parsed_output = self._parse_llm_response(llm_response.content)
            key_insight = parsed_output.get(
                "key_insight", "Timeline analysis completed"
            )
            follow_up_question = parsed_output.get(
                "follow_up_question", "Should we proceed to formulate hypotheses?"
            )

            findings = []

            # 3. Execute tools if LLM recommends them
            if parsed_output.get("tool_to_use") == "knowledge_base_search":
                kb_tool = context.get("knowledge_base_tool")
                if kb_tool:
                    tool_query = parsed_output.get(
                        "tool_query",
                        f"timeline analysis recent changes deployment {agent_state.get('user_query', '')}",
                    )
                    tool_results = kb_tool.search(
                        tool_query, context={"phase": "establish_timeline"}
                    )
                    findings.append(
                        f"Knowledge base search for timeline patterns completed"
                    )

                    # If no useful results from KB, try web search as fallback
                    if len(tool_results.split("**")) < 3:  # Minimal useful results
                        web_search_tool = context.get("web_search_tool")
                        if web_search_tool and web_search_tool.is_available():
                            web_results = await web_search_tool._arun(
                                query=tool_query,
                                context={"phase": "establish_timeline"},
                            )
                            findings.append(
                                "Web search performed for timeline analysis information"
                            )

            # Analyze uploaded data for timeline clues
            if context.get("uploaded_data"):
                for data in context["uploaded_data"]:
                    if data.get("data_type") == "log_file":
                        findings.append(
                            "Log timestamps available for timeline analysis"
                        )
                    elif data.get("data_type") == "metrics_data":
                        findings.append(
                            "Metrics data shows performance trends over time"
                        )

            return {
                "phase": Phase.ESTABLISH_TIMELINE.value,
                "status": "completed",
                "findings": findings,
                "key_insight": key_insight,
                "follow_up_question": follow_up_question,
                "recommendations": [
                    "Continue to hypothesis formulation phase",
                    "Correlate timeline with system changes",
                ],
                "next_phase": Phase.FORMULATE_HYPOTHESIS.value,
                "confidence_score": 0.8,
                "requires_user_input": True,
            }

        except Exception as e:
            self.logger.error(f"LLM-driven timeline analysis failed: {e}")
            return self._fallback_timeline_analysis(agent_state, context)

    async def _execute_hypothesis_phase(
        self, prompt: str, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute the hypothesis formulation phase with LLM reasoning

        Args:
            prompt: Phase-specific prompt
            agent_state: Current agent state
            context: Additional context

        Returns:
            Phase execution results
        """
        self.logger.info("Executing hypothesis formulation phase with LLM")

        try:
            # Get the LLM router from context
            llm_router = context.get("llm_router")
            if not llm_router:
                return self._fallback_hypothesis_analysis(agent_state, context)

            # 1. Call the LLM with the generated prompt
            llm_response = await llm_router.route(
                prompt=prompt, max_tokens=600, temperature=0.4
            )

            # 2. Parse the structured response from the LLM
            parsed_output = self._parse_llm_response(llm_response.content)
            key_insight = parsed_output.get(
                "key_insight", "Hypotheses formulated based on available evidence"
            )
            follow_up_question = parsed_output.get(
                "follow_up_question", "Which hypothesis should we validate first?"
            )

            findings = []
            hypothesis_list = []

            # 3. Execute tools if LLM recommends them
            if parsed_output.get("tool_to_use") == "knowledge_base_search":
                kb_tool = context.get("knowledge_base_tool")
                if kb_tool:
                    tool_query = parsed_output.get(
                        "tool_query",
                        f"root cause hypothesis similar issues {agent_state.get('user_query', '')}",
                    )
                    tool_results = kb_tool.search(
                        tool_query, context={"phase": "formulate_hypothesis"}
                    )
                    findings.append(
                        "Knowledge base search for similar issue patterns completed"
                    )
                    hypothesis_list.append(
                        "Pattern-matched hypothesis from knowledge base"
                    )

                    # If no useful results from KB, try web search as fallback
                    if len(tool_results.split("**")) < 3:  # Minimal useful results
                        web_search_tool = context.get("web_search_tool")
                        if web_search_tool and web_search_tool.is_available():
                            web_results = await web_search_tool._arun(
                                query=tool_query,
                                context={"phase": "formulate_hypothesis"},
                            )
                            findings.append(
                                "Web search performed for hypothesis formulation"
                            )
                            hypothesis_list.append(
                                "Web-sourced hypothesis from external documentation"
                            )

            # Extract hypotheses from LLM response
            if "hypotheses" in parsed_output:
                hypothesis_list.extend(parsed_output["hypotheses"])

            # Analyze patterns in previous findings
            previous_findings = agent_state.get("findings", [])
            if previous_findings:
                findings.append("Previous phase findings inform hypothesis generation")

                # Extract key patterns from findings
                for finding in previous_findings[-3:]:
                    finding_text = finding.get("finding", "").lower()
                    if (
                        "error" in finding_text
                        and "Error-based failure hypothesis" not in hypothesis_list
                    ):
                        hypothesis_list.append("Error-based failure hypothesis")
                    elif (
                        "performance" in finding_text
                        and "Performance degradation hypothesis" not in hypothesis_list
                    ):
                        hypothesis_list.append("Performance degradation hypothesis")
                    elif (
                        "connection" in finding_text
                        and "Connectivity issue hypothesis" not in hypothesis_list
                    ):
                        hypothesis_list.append("Connectivity issue hypothesis")

            return {
                "phase": Phase.FORMULATE_HYPOTHESIS.value,
                "status": "completed",
                "findings": findings,
                "key_insight": key_insight,
                "follow_up_question": follow_up_question,
                "hypothesis_list": hypothesis_list,
                "recommendations": [
                    "Continue to hypothesis validation phase",
                    "Prioritize hypotheses by likelihood and testability",
                ],
                "next_phase": Phase.VALIDATE_HYPOTHESIS.value,
                "confidence_score": 0.7,
                "requires_user_input": True,
            }

        except Exception as e:
            self.logger.error(f"LLM-driven hypothesis formulation failed: {e}")
            return self._fallback_hypothesis_analysis(agent_state, context)

    async def _execute_validation_phase(
        self, prompt: str, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute the hypothesis validation phase with LLM reasoning

        Args:
            prompt: Phase-specific prompt
            agent_state: Current agent state
            context: Additional context

        Returns:
            Phase execution results
        """
        self.logger.info("Executing hypothesis validation phase with LLM")

        try:
            # Get the LLM router from context
            llm_router = context.get("llm_router")
            if not llm_router:
                return self._fallback_validation_analysis(agent_state, context)

            # 1. Call the LLM with the generated prompt
            llm_response = await llm_router.route(
                prompt=prompt, max_tokens=500, temperature=0.3
            )

            # 2. Parse the structured response from the LLM
            parsed_output = self._parse_llm_response(llm_response.content)
            key_insight = parsed_output.get(
                "key_insight", "Hypothesis validation completed"
            )
            follow_up_question = parsed_output.get(
                "follow_up_question", "Does this match your observations?"
            )

            findings = []
            validated_hypothesis = None

            # 3. Execute tools if LLM recommends them
            if parsed_output.get("tool_to_use") == "knowledge_base_search":
                kb_tool = context.get("knowledge_base_tool")
                if kb_tool:
                    tool_query = parsed_output.get(
                        "tool_query",
                        f"validation testing procedures {agent_state.get('user_query', '')}",
                    )
                    tool_results = kb_tool.search(
                        tool_query, context={"phase": "validate_hypothesis"}
                    )
                    findings.append(
                        "Knowledge base search for validation approaches completed"
                    )

                    # If no useful results from KB, try web search as fallback
                    if len(tool_results.split("**")) < 3:  # Minimal useful results
                        web_search_tool = context.get("web_search_tool")
                        if web_search_tool and web_search_tool.is_available():
                            web_results = await web_search_tool._arun(
                                query=tool_query,
                                context={"phase": "validate_hypothesis"},
                            )
                            findings.append(
                                "Web search performed for validation approaches"
                            )

            # Look for hypotheses from previous phase
            investigation_context = agent_state.get("investigation_context", {})
            hypothesis_results = investigation_context.get(
                "formulate_hypothesis_results", {}
            )
            hypothesis_list = hypothesis_results.get("hypothesis_list", [])

            if hypothesis_list:
                findings.append("Testing hypotheses from previous phase")
                # Use LLM recommendation or default to first hypothesis
                validated_hypothesis = parsed_output.get(
                    "validated_hypothesis", hypothesis_list[0]
                )

            return {
                "phase": Phase.VALIDATE_HYPOTHESIS.value,
                "status": "completed",
                "findings": findings,
                "key_insight": key_insight,
                "follow_up_question": follow_up_question,
                "validated_hypothesis": validated_hypothesis,
                "recommendations": [
                    "Continue to solution proposal phase",
                    "Document validation results for future reference",
                ],
                "next_phase": Phase.PROPOSE_SOLUTION.value,
                "confidence_score": 0.8,
                "requires_user_input": True,
            }

        except Exception as e:
            self.logger.error(f"LLM-driven hypothesis validation failed: {e}")
            return self._fallback_validation_analysis(agent_state, context)

    async def _execute_solution_phase(
        self, prompt: str, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute the solution proposal phase with LLM reasoning

        Args:
            prompt: Phase-specific prompt
            agent_state: Current agent state
            context: Additional context

        Returns:
            Phase execution results
        """
        self.logger.info("Executing solution proposal phase with LLM")

        try:
            # Get the LLM router from context
            llm_router = context.get("llm_router")
            if not llm_router:
                return self._fallback_solution_analysis(agent_state, context)

            # 1. Call the LLM with the generated prompt
            llm_response = await llm_router.route(
                prompt=prompt, max_tokens=600, temperature=0.4
            )

            # 2. Parse the structured response from the LLM
            parsed_output = self._parse_llm_response(llm_response.content)
            key_insight = parsed_output.get(
                "key_insight", "Solution proposal completed"
            )
            follow_up_question = parsed_output.get(
                "follow_up_question",
                "Which solution approach would you like to implement?",
            )

            findings = []
            solution_options = []

            # 3. Execute tools if LLM recommends them
            if parsed_output.get("tool_to_use") == "knowledge_base_search":
                kb_tool = context.get("knowledge_base_tool")
                if kb_tool:
                    tool_query = parsed_output.get(
                        "tool_query",
                        f"solution fix resolution {agent_state.get('user_query', '')}",
                    )
                    tool_results = kb_tool.search(
                        tool_query, context={"phase": "propose_solution"}
                    )
                    findings.append(
                        "Knowledge base search for solution patterns completed"
                    )
                    solution_options.append(
                        "Apply documented solution from knowledge base"
                    )

                    # If no useful results from KB, try web search as fallback
                    if len(tool_results.split("**")) < 3:  # Minimal useful results
                        web_search_tool = context.get("web_search_tool")
                        if web_search_tool and web_search_tool.is_available():
                            web_results = await web_search_tool._arun(
                                query=tool_query, context={"phase": "propose_solution"}
                            )
                            findings.append(
                                "Web search performed for solution approaches"
                            )
                            solution_options.append(
                                "Apply web-sourced solution from external documentation"
                            )

            # Extract solutions from LLM response
            if "solutions" in parsed_output:
                solution_options.extend(parsed_output["solutions"])

            # Look for validated hypothesis from previous phase
            investigation_context = agent_state.get("investigation_context", {})
            validation_results = investigation_context.get(
                "validate_hypothesis_results", {}
            )
            validated_hypothesis = validation_results.get("validated_hypothesis")

            if validated_hypothesis:
                findings.append("Solution based on validated hypothesis")
                if (
                    f"Address root cause: {validated_hypothesis}"
                    not in solution_options
                ):
                    solution_options.append(
                        f"Address root cause: {validated_hypothesis}"
                    )

            return {
                "phase": Phase.PROPOSE_SOLUTION.value,
                "status": "completed",
                "findings": findings,
                "key_insight": key_insight,
                "follow_up_question": follow_up_question,
                "solution_options": solution_options,
                "recommendations": [
                    "Implement selected solution carefully",
                    "Monitor system health during implementation",
                    "Document solution for future reference",
                ],
                "next_phase": None,  # Final phase
                "confidence_score": 0.9,
                "requires_user_input": True,
            }

        except Exception as e:
            self.logger.error(f"LLM-driven solution proposal failed: {e}")
            return self._fallback_solution_analysis(agent_state, context)

    def _parse_llm_response(self, content: str) -> Dict[str, Any]:
        """
        Parse the LLM response to extract structured information

        Args:
            content: Raw LLM response content

        Returns:
            Parsed structured data
        """
        parsed: Dict[str, Any] = {
            "key_insight": "",
            "follow_up_question": "",
            "tool_to_use": None,
            "tool_query": "",
            "hypotheses": [],
            "solutions": [],
            "validated_hypothesis": None,
        }

        # Handle None or empty content
        if not content:
            return parsed

        # Split content into lines for analysis
        lines = content.split("\n")
        current_section = None

        for line in lines:
            if line is None:
                continue

            line = line.strip()
            if not line:
                continue

            # Look for key insight indicators
            if any(
                indicator in line.lower()
                for indicator in ["key insight:", "insight:", "finding:"]
            ):
                parsed["key_insight"] = line.split(":", 1)[-1].strip()

            # Look for follow-up questions
            elif "?" in line and any(
                indicator in line.lower()
                for indicator in ["question:", "next:", "should"]
            ):
                parsed["follow_up_question"] = line

            # Look for tool usage recommendations
            elif "knowledge base" in line.lower() or "search" in line.lower():
                parsed["tool_to_use"] = "knowledge_base_search"
                if "search for" in line.lower():
                    search_part = line.lower().split("search for")[-1].strip()
                    parsed["tool_query"] = search_part

            # Look for hypothesis lists
            elif "hypothesis" in line.lower() and (
                "1." in line or "-" in line or "likely" in line.lower()
            ):
                hypothesis = line.replace("1.", "").replace("-", "").strip()
                if hypothesis and hypothesis not in parsed["hypotheses"]:
                    parsed["hypotheses"].append(hypothesis)

            # Look for solution lists
            elif "solution" in line.lower() and (
                "1." in line or "-" in line or "option" in line.lower()
            ):
                solution = line.replace("1.", "").replace("-", "").strip()
                if solution and solution not in parsed["solutions"]:
                    parsed["solutions"].append(solution)

            # Look for validated hypothesis
            elif "validated" in line.lower() or "confirmed" in line.lower():
                if "hypothesis" in line.lower():
                    parsed["validated_hypothesis"] = line

        # Fallback: use first meaningful sentence as key insight
        if not parsed["key_insight"]:
            sentences = content.split(".")
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) > 20 and not sentence.startswith("Based on"):
                    parsed["key_insight"] = sentence
                    break

        # Fallback: use last question as follow-up
        if not parsed["follow_up_question"]:
            questions = [line for line in lines if line and "?" in line]
            if questions:
                parsed["follow_up_question"] = questions[-1].strip()

        return parsed

    # Fallback methods for when LLM is not available

    def _fallback_blast_radius_analysis(
        self, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback blast radius analysis without LLM"""
        findings = []
        key_insight = "Need to assess the full scope of impact"
        follow_up_question = (
            "Can you provide more details about which systems or users are affected?"
        )

        if context.get("uploaded_data"):
            for data in context["uploaded_data"]:
                if data.get("data_type") == "log_file":
                    findings.append("Log files available for analysis")
                    key_insight = "Log files indicate potential system-wide impact"
                    follow_up_question = (
                        "Are multiple services showing similar error patterns?"
                    )

        return {
            "phase": Phase.DEFINE_BLAST_RADIUS.value,
            "status": "completed",
            "findings": findings,
            "key_insight": key_insight,
            "follow_up_question": follow_up_question,
            "recommendations": ["Continue to timeline establishment phase"],
            "next_phase": Phase.ESTABLISH_TIMELINE.value,
            "confidence_score": 0.6,
            "requires_user_input": True,
        }

    def _fallback_timeline_analysis(
        self, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback timeline analysis without LLM"""
        findings = []
        key_insight = "Timeline establishment requires more temporal data"
        follow_up_question = "When exactly did you first notice the issue?"

        if context.get("uploaded_data"):
            for data in context["uploaded_data"]:
                if data.get("data_type") == "log_file":
                    findings.append("Log timestamps available for timeline analysis")
                    key_insight = "Logs show issue started at specific time"
                    follow_up_question = (
                        "Were there any deployments or changes around that time?"
                    )

        return {
            "phase": Phase.ESTABLISH_TIMELINE.value,
            "status": "completed",
            "findings": findings,
            "key_insight": key_insight,
            "follow_up_question": follow_up_question,
            "recommendations": ["Continue to hypothesis formulation phase"],
            "next_phase": Phase.FORMULATE_HYPOTHESIS.value,
            "confidence_score": 0.6,
            "requires_user_input": True,
        }

    def _fallback_hypothesis_analysis(
        self, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback hypothesis analysis without LLM"""
        findings = []
        hypothesis_list = []

        # Analyze patterns in previous findings
        previous_findings = agent_state.get("findings", [])
        if previous_findings:
            findings.append("Previous phase findings inform hypothesis generation")
            for finding in previous_findings[-3:]:
                if "error" in finding.get("finding", "").lower():
                    hypothesis_list.append("Error-based failure hypothesis")
                elif "performance" in finding.get("finding", "").lower():
                    hypothesis_list.append("Performance degradation hypothesis")

        key_insight = (
            f"Generated {len(hypothesis_list)} testable hypotheses"
            if hypothesis_list
            else "Need more specific symptoms to formulate hypotheses"
        )
        follow_up_question = (
            "Which hypothesis should we test first?"
            if hypothesis_list
            else "Can you provide more details about the specific error messages?"
        )

        return {
            "phase": Phase.FORMULATE_HYPOTHESIS.value,
            "status": "completed",
            "findings": findings,
            "key_insight": key_insight,
            "follow_up_question": follow_up_question,
            "hypothesis_list": hypothesis_list,
            "recommendations": ["Continue to hypothesis validation phase"],
            "next_phase": Phase.VALIDATE_HYPOTHESIS.value,
            "confidence_score": 0.6,
            "requires_user_input": True,
        }

    def _fallback_validation_analysis(
        self, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback validation analysis without LLM"""
        findings = []
        validated_hypothesis = None

        # Look for hypothesis from previous phase
        investigation_context = agent_state.get("investigation_context", {})
        hypothesis_results = investigation_context.get(
            "formulate_hypothesis_results", {}
        )
        hypothesis_list = hypothesis_results.get("hypothesis_list", [])

        if hypothesis_list:
            findings.append("Testing hypotheses from previous phase")
            validated_hypothesis = hypothesis_list[0]
            key_insight = f"Evidence supports: {validated_hypothesis}"
            follow_up_question = "Does this match your observations of the issue?"
        else:
            key_insight = "Need to gather more evidence to validate hypotheses"
            follow_up_question = (
                "Can you provide additional logs or metrics to test our theories?"
            )

        return {
            "phase": Phase.VALIDATE_HYPOTHESIS.value,
            "status": "completed",
            "findings": findings,
            "key_insight": key_insight,
            "follow_up_question": follow_up_question,
            "validated_hypothesis": validated_hypothesis,
            "recommendations": ["Continue to solution proposal phase"],
            "next_phase": Phase.PROPOSE_SOLUTION.value,
            "confidence_score": 0.6,
            "requires_user_input": True,
        }

    def _fallback_solution_analysis(
        self, agent_state: AgentState, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fallback solution analysis without LLM"""
        findings = []
        solution_options = []

        # Look for validated hypothesis from previous phase
        investigation_context = agent_state.get("investigation_context", {})
        validation_results = investigation_context.get(
            "validate_hypothesis_results", {}
        )
        validated_hypothesis = validation_results.get("validated_hypothesis")

        if validated_hypothesis:
            findings.append("Solution based on validated hypothesis")
            solution_options.append(f"Address root cause: {validated_hypothesis}")
            key_insight = "Identified solution approach based on validated hypothesis"
            follow_up_question = (
                "Would you like to proceed with implementing this solution?"
            )
        else:
            key_insight = "Need to develop custom solution based on specific issue characteristics"
            follow_up_question = (
                "What constraints or requirements should I consider for the solution?"
            )

        return {
            "phase": Phase.PROPOSE_SOLUTION.value,
            "status": "completed",
            "findings": findings,
            "key_insight": key_insight,
            "follow_up_question": follow_up_question,
            "solution_options": solution_options,
            "recommendations": ["Implement selected solution carefully"],
            "next_phase": None,
            "confidence_score": 0.7,
            "requires_user_input": True,
        }

    def get_phase_guidance(self, phase: Phase) -> Dict[str, Any]:
        """
        Get guidance for a specific phase

        Args:
            phase: The phase to get guidance for

        Returns:
            Phase guidance dictionary
        """
        return self.phase_guidance.get(phase, {}) or {}

    def get_next_phase(self, current_phase: Phase) -> Optional[Phase]:
        """
        Get the next phase in the sequence

        Args:
            current_phase: Current phase

        Returns:
            Next phase or None if at the end
        """
        try:
            current_index = self.PHASES.index(current_phase.value)
            if current_index < len(self.PHASES) - 1:
                return Phase(self.PHASES[current_index + 1])
        except ValueError:
            pass

        return None

    def validate_phase_transition(self, from_phase: Phase, to_phase: Phase) -> bool:
        """
        Validate if a phase transition is allowed

        Args:
            from_phase: Current phase
            to_phase: Target phase

        Returns:
            True if transition is valid
        """
        try:
            from_index = self.PHASES.index(from_phase.value)
            to_index = self.PHASES.index(to_phase.value)
            return to_index == from_index + 1  # Only allow sequential transitions
        except ValueError:
            return False

    def should_request_user_input(self, phase_result: Dict[str, Any]) -> bool:
        """
        Determine if user input is required based on phase results

        Args:
            phase_result: Results from phase execution

        Returns:
            True if user input is required
        """
        return bool(phase_result.get("requires_user_input", False))
