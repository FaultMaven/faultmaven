# Implementation Examples

## Production-Ready Integration Code

This document provides **complete implementation examples** showing how to integrate the prompt templates into your FaultMaven application.

---

## Table of Contents

1. [Module Structure](#1-module-structure)
2. [Agent Core Implementation](#2-agent-core-implementation)
3. [LLM Integration](#3-llm-integration)
4. [Response Processing](#4-response-processing)
5. [State Management](#5-state-management)
6. [Error Handling](#6-error-handling)
7. [Testing Framework](#7-testing-framework)
8. [Complete Usage Examples](#8-complete-usage-examples)
9. [Configuration](#9-configuration)
10. [Deployment Guide](#10-deployment-guide)

---

## 1. Module Structure

```
app/
├── agent/
│   ├── __init__.py
│   ├── core.py                 # Main Agent class
│   ├── prompts/
│   │   ├── __init__.py
│   │   ├── templates.py        # Prompt templates (from Part 2)
│   │   └── builder.py          # Prompt building logic
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py           # LLM API client
│   │   └── schemas.py          # Response schemas
│   ├── processors/
│   │   ├── __init__.py
│   │   ├── consulting.py       # CONSULTING response processor
│   │   ├── investigating.py    # INVESTIGATING response processor
│   │   └── terminal.py         # TERMINAL response processor
│   └── state/
│       ├── __init__.py
│       ├── manager.py          # State management
│       └── transitions.py      # Status transitions
├── models/
│   ├── __init__.py
│   └── case.py                 # Case model (from Case Model Design v2.0)
├── repositories/
│   ├── __init__.py
│   └── case_repository.py      # Database operations
├── services/
│   ├── __init__.py
│   └── agent_service.py        # High-level service interface
├── api/
│   ├── __init__.py
│   └── endpoints/
│       ├── __init__.py
│       └── chat.py             # API endpoints
├── config.py                   # Configuration
└── main.py                     # Application entry point
```

---

## 2. Agent Core Implementation

```python
# app/agent/core.py

"""
FaultMaven Agent - Core Implementation

Main agent class that orchestrates:
- Prompt generation
- LLM invocation
- Response processing
- State updates
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from app.models import Case, CaseStatus
from app.agent.prompts.builder import build_prompt, get_prompt_metadata
from app.agent.llm.client import LLMClient
from app.agent.llm.schemas import (
    ConsultingResponse,
    InvestigationResponse,
    TerminalResponse
)
from app.agent.processors import (
    ConsultingProcessor,
    InvestigatingProcessor,
    TerminalProcessor
)
from app.agent.state.manager import StateManager

logger = logging.getLogger(__name__)


class FaultMavenAgent:
    """
    FaultMaven troubleshooting agent.
    
    Responsibilities:
    - Generate prompts based on case state
    - Invoke LLM with structured output
    - Process LLM responses
    - Update case state
    """
    
    def __init__(
        self,
        llm_client: LLMClient,
        state_manager: StateManager
    ):
        """
        Initialize agent.
        
        Args:
            llm_client: LLM API client
            state_manager: State management system
        """
        self.llm_client = llm_client
        self.state_manager = state_manager
        
        # Response processors by status
        self.processors = {
            CaseStatus.CONSULTING: ConsultingProcessor(state_manager),
            CaseStatus.INVESTIGATING: InvestigatingProcessor(state_manager),
            CaseStatus.RESOLVED: TerminalProcessor(state_manager),
            CaseStatus.CLOSED: TerminalProcessor(state_manager),
        }
    
    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Process a single conversation turn.
        
        Args:
            case: Current case
            user_message: User's message
            attachments: Optional file attachments
            
        Returns:
            {
                "agent_response": str,  # Natural language response
                "case_updated": Case,   # Updated case object
                "metadata": dict        # Turn metadata
            }
            
        Raises:
            AgentError: If processing fails
        """
        
        logger.info(
            f"Processing turn {case.current_turn + 1} for case {case.case_id} "
            f"(status: {case.status})"
        )
        
        try:
            # Step 1: Build prompt
            prompt = build_prompt(case, user_message)
            prompt_metadata = get_prompt_metadata(case)
            
            logger.debug(
                f"Built prompt: {len(prompt)} chars, "
                f"template: {prompt_metadata['template_used']}"
            )
            
            # Step 2: Get response schema
            response_schema = self._get_response_schema(case.status)
            
            # Step 3: Invoke LLM
            llm_response = await self.llm_client.generate(
                prompt=prompt,
                response_schema=response_schema,
                temperature=0.7,
                max_tokens=4000
            )
            
            logger.debug(f"LLM response received: {llm_response.model_dump()}")
            
            # Step 4: Process response and update state
            processor = self.processors[case.status]
            updated_case, turn_metadata = await processor.process(
                case=case,
                user_message=user_message,
                llm_response=llm_response,
                attachments=attachments
            )
            
            # Step 5: Increment turn counter
            updated_case.current_turn += 1
            
            # Step 6: Save case
            await self.state_manager.save_case(updated_case)
            
            logger.info(
                f"Turn processed successfully. "
                f"New status: {updated_case.status}, "
                f"Progress made: {turn_metadata.get('progress_made', False)}"
            )
            
            return {
                "agent_response": llm_response.agent_response,
                "case_updated": updated_case,
                "metadata": {
                    **turn_metadata,
                    **prompt_metadata,
                    "turn_number": updated_case.current_turn,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            }
            
        except Exception as e:
            logger.error(
                f"Error processing turn for case {case.case_id}: {e}",
                exc_info=True
            )
            raise AgentError(f"Turn processing failed: {e}") from e
    
    def _get_response_schema(self, status: CaseStatus):
        """Get appropriate response schema for status"""
        
        schema_map = {
            CaseStatus.CONSULTING: ConsultingResponse,
            CaseStatus.INVESTIGATING: InvestigationResponse,
            CaseStatus.RESOLVED: TerminalResponse,
            CaseStatus.CLOSED: TerminalResponse,
        }
        
        return schema_map[status]


class AgentError(Exception):
    """Base exception for agent errors"""
    pass
```

---

## 3. LLM Integration

```python
# app/agent/llm/client.py

"""
LLM Client - Anthropic Claude Integration

Handles API communication with Claude for structured output.
"""

import logging
import anthropic
from typing import Type, TypeVar, Optional
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class LLMClient:
    """
    Client for LLM API (Anthropic Claude).
    
    Features:
    - Structured output via response schemas
    - Automatic retry on rate limits
    - Token usage tracking
    - Error handling
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize LLM client.
        
        Args:
            api_key: Anthropic API key (defaults to settings)
        """
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = settings.LLM_MODEL  # "claude-sonnet-4-20250514" or similar
        
    async def generate(
        self,
        prompt: str,
        response_schema: Type[T],
        temperature: float = 0.7,
        max_tokens: int = 4000
    ) -> T:
        """
        Generate structured response from LLM.
        
        Args:
            prompt: Complete prompt text
            response_schema: Pydantic model for response
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens in response
            
        Returns:
            Validated response matching schema
            
        Raises:
            LLMError: If API call fails
        """
        
        try:
            # Call Claude API with structured output
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{
                    "role": "user",
                    "content": prompt
                }],
                # Request JSON response matching schema
                response_format={
                    "type": "json_object",
                    "schema": response_schema.model_json_schema()
                }
            )
            
            # Extract JSON content
            content = response.content[0].text
            
            # Parse and validate against schema
            validated_response = response_schema.model_validate_json(content)
            
            # Log token usage
            usage = response.usage
            logger.info(
                f"LLM call completed. "
                f"Input tokens: {usage.input_tokens}, "
                f"Output tokens: {usage.output_tokens}"
            )
            
            return validated_response
            
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            raise LLMError(f"API call failed: {e}") from e
        
        except Exception as e:
            logger.error(f"Unexpected error in LLM generation: {e}")
            raise LLMError(f"LLM generation failed: {e}") from e


class LLMError(Exception):
    """Exception for LLM-related errors"""
    pass
```

```python
# app/agent/llm/schemas.py

"""
LLM Response Schemas

Pydantic models matching the output format specified in prompts.
These define the structured output we expect from the LLM.
"""

from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from enum import Enum


# ============================================================================
# CONSULTING Response Schema
# ============================================================================

class ProblemConfirmation(BaseModel):
    """Agent's initial understanding of problem"""
    problem_type: str = Field(
        description="error | slowness | unavailability | data_issue | other"
    )
    severity_guess: str = Field(
        description="critical | high | medium | low | unknown"
    )
    preliminary_guidance: Optional[str] = None


class ConsultingStateUpdate(BaseModel):
    """State updates during CONSULTING"""
    problem_confirmation: Optional[ProblemConfirmation] = None
    proposed_problem_statement: Optional[str] = None
    quick_suggestions: List[str] = Field(default_factory=list)


class ConsultingResponse(BaseModel):
    """Complete response for CONSULTING status"""
    agent_response: str = Field(description="Natural language response")
    state_updates: ConsultingStateUpdate


# ============================================================================
# INVESTIGATING Response Schema
# ============================================================================

class TurnOutcome(str, Enum):
    """What happened this turn"""
    MILESTONE_COMPLETED = "milestone_completed"
    DATA_PROVIDED = "data_provided"
    DATA_REQUESTED = "data_requested"
    DATA_NOT_PROVIDED = "data_not_provided"
    HYPOTHESIS_TESTED = "hypothesis_tested"
    CASE_RESOLVED = "case_resolved"
    CONVERSATION = "conversation"
    OTHER = "other"


class MilestoneUpdates(BaseModel):
    """Milestone completion updates"""
    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    root_cause_method: Optional[str] = None
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None
    solution_verified: Optional[bool] = None


class EvidenceStance(str, Enum):
    """How evidence relates to hypothesis"""
    STRONGLY_SUPPORTS = "strongly_supports"
    SUPPORTS = "supports"
    NEUTRAL = "neutral"
    REFUTES = "refutes"
    STRONGLY_REFUTES = "strongly_refutes"


class EvidenceToAdd(BaseModel):
    """Evidence object LLM creates"""
    summary: str = Field(max_length=500)
    analysis: Optional[str] = Field(default=None, max_length=2000)
    tests_hypothesis_id: Optional[str] = None
    stance: Optional[EvidenceStance] = None


class EvidenceRequestToAdd(BaseModel):
    """Evidence request LLM creates"""
    description: str = Field(max_length=500)
    rationale: str = Field(max_length=1000)
    acquisition_guidance: Optional[str] = Field(default=None, max_length=2000)
    validates_milestone: Optional[str] = None
    tests_hypothesis_id: Optional[str] = None


class HypothesisToAdd(BaseModel):
    """Hypothesis LLM generates"""
    statement: str = Field(max_length=500)
    likelihood: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=1000)
    evidence_requirements: List[str] = Field(default_factory=list)


class HypothesisUpdate(BaseModel):
    """Update to existing hypothesis"""
    status: Optional[str] = None  # VALIDATED | REFUTED | INCONCLUSIVE
    likelihood: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reasoning: Optional[str] = None


class SolutionToAdd(BaseModel):
    """Solution LLM proposes"""
    title: str = Field(max_length=200)
    solution_type: str
    immediate_action: str = Field(max_length=2000)
    longterm_fix: Optional[str] = Field(default=None, max_length=2000)
    implementation_steps: List[str] = Field(default_factory=list)
    risks: Optional[str] = None


class WorkingConclusionUpdate(BaseModel):
    """Working conclusion update"""
    statement: str = Field(max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=2000)
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    caveats: List[str] = Field(default_factory=list)
    next_evidence_needed: List[str] = Field(default_factory=list)


class RootCauseConclusionUpdate(BaseModel):
    """Root cause conclusion"""
    root_cause: str = Field(max_length=500)
    mechanism: str = Field(max_length=2000)
    confidence_score: float = Field(ge=0.0, le=1.0)


class ProblemVerificationUpdate(BaseModel):
    """Problem verification data"""
    symptom_statement: Optional[str] = None
    temporal_state: Optional[str] = None  # ONGOING | HISTORICAL
    urgency_level: Optional[str] = None  # CRITICAL | HIGH | MEDIUM | LOW
    started_at: Optional[str] = None
    affected_services: List[str] = Field(default_factory=list)


class InvestigationStateUpdate(BaseModel):
    """State updates during INVESTIGATING"""
    milestones: Optional[MilestoneUpdates] = None
    verification_updates: Optional[ProblemVerificationUpdate] = None
    evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
    hypotheses_to_add: List[HypothesisToAdd] = Field(default_factory=list)
    hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
    hypothesis_evidence_links: List[HypothesisEvidenceLinkToAdd] = Field(default_factory=list)
    solutions_to_add: List[SolutionToAdd] = Field(default_factory=list)
    working_conclusion: Optional[WorkingConclusionUpdate] = None
    root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
    outcome: TurnOutcome


class InvestigationResponse(BaseModel):
    """Complete response for INVESTIGATING status"""
    agent_response: str = Field(description="Natural language response")
    state_updates: InvestigationStateUpdate


# ============================================================================
# TERMINAL Response Schema
# ============================================================================

class DocumentType(str, Enum):
    """Document types for generation"""
    INCIDENT_REPORT = "incident_report"
    POST_MORTEM = "post_mortem"
    RUNBOOK = "runbook"
    CHAT_SUMMARY = "chat_summary"
    OTHER = "other"


class DocumentationUpdate(BaseModel):
    """Documentation generation request"""
    lessons_learned: List[str] = Field(default_factory=list)
    what_went_well: List[str] = Field(default_factory=list)
    what_could_improve: List[str] = Field(default_factory=list)
    preventive_measures: List[str] = Field(default_factory=list)
    monitoring_recommendations: List[str] = Field(default_factory=list)
    documents_to_generate: List[DocumentType] = Field(default_factory=list)


class TerminalStateUpdate(BaseModel):
    """State updates for terminal status"""
    documentation_updates: Optional[DocumentationUpdate] = None


class TerminalResponse(BaseModel):
    """Complete response for RESOLVED/CLOSED status"""
    agent_response: str = Field(description="Natural language response")
    state_updates: TerminalStateUpdate
```

---

## 4. Response Processing

### 4.1 Consulting Processor

```python
# app/agent/processors/consulting.py

"""
CONSULTING Response Processor

Processes LLM responses during consultation phase.
"""

import logging
from typing import Dict, Any, Tuple
from datetime import datetime, timezone

from app.models import Case, CaseStatus, ConsultingData
from app.agent.llm.schemas import ConsultingResponse
from app.agent.state.manager import StateManager

logger = logging.getLogger(__name__)


class ConsultingProcessor:
    """
    Process CONSULTING responses and update case state.
    
    Responsibilities:
    - Update problem confirmation
    - Update proposed problem statement
    - Detect user confirmation signals
    - Detect user decision to investigate
    - Trigger transition to INVESTIGATING
    """
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
    
    async def process(
        self,
        case: Case,
        user_message: str,
        llm_response: ConsultingResponse,
        attachments: list = None
    ) -> Tuple[Case, Dict[str, Any]]:
        """
        Process consulting response and update case.
        
        Args:
            case: Current case
            user_message: User's message
            llm_response: LLM response
            attachments: File attachments
            
        Returns:
            (updated_case, turn_metadata)
        """
        
        state_updates = llm_response.state_updates
        
        # Update problem confirmation
        if state_updates.problem_confirmation:
            case.consulting.problem_confirmation = state_updates.problem_confirmation
        
        # Update proposed problem statement
        if state_updates.proposed_problem_statement:
            case.consulting.proposed_problem_statement = state_updates.proposed_problem_statement
        
        # Update quick suggestions
        if state_updates.quick_suggestions:
            case.consulting.quick_suggestions = state_updates.quick_suggestions
        
        # Detect user confirmation signals
        confirmation_signals = ["yes", "correct", "that's right", "accurate", "exactly"]
        if any(signal in user_message.lower() for signal in confirmation_signals):
            if case.consulting.proposed_problem_statement:
                case.consulting.problem_statement_confirmed = True
        
        # Detect user decision to investigate
        investigation_signals = ["investigate", "go ahead", "proceed", "start", "please help"]
        if any(signal in user_message.lower() for signal in investigation_signals):
            if case.consulting.problem_statement_confirmed:
                case.consulting.decided_to_investigate = True
        
        # Check if should transition to INVESTIGATING
        if (case.consulting.problem_statement_confirmed and 
            case.consulting.decided_to_investigate):
            
            # Transition to INVESTIGATING
            await self.state_manager.transition_to_investigating(case)
        
        # Build metadata
        metadata = {
            "problem_confirmed": case.consulting.problem_statement_confirmed,
            "investigation_decided": case.consulting.decided_to_investigate,
            "status_transitioned": case.status == CaseStatus.INVESTIGATING
        }
        
        return case, metadata
```

### 4.2 Investigating Processor

```python
# app/agent/processors/investigating.py

"""
INVESTIGATING Response Processor

Processes LLM responses during investigation, applies updates to case state.
"""

import logging
from typing import Dict, Any, Tuple
from datetime import datetime, timezone
from uuid import uuid4

from app.models import (
    Case, Evidence, EvidenceCategory, EvidenceRequest, EvidenceStatus,
    Hypothesis, HypothesisStatus, Solution, WorkingConclusion,
    RootCauseConclusion, TurnProgress, InvestigationStage
)
from app.agent.llm.schemas import InvestigationResponse
from app.agent.state.manager import StateManager

logger = logging.getLogger(__name__)


class InvestigatingProcessor:
    """
    Process INVESTIGATING responses and update case state.
    
    Responsibilities:
    - Apply milestone updates
    - Process evidence (infer category, calculate advancement)
    - Update evidence request mention counts
    - Determine path selection
    - Detect progress
    - Update degraded mode
    - Check status transitions
    """
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
    
    async def process(
        self,
        case: Case,
        user_message: str,
        llm_response: InvestigationResponse,
        attachments: list = None
    ) -> Tuple[Case, Dict[str, Any]]:
        """
        Process investigation response and update case.
        
        Args:
            case: Current case
            user_message: User's message
            llm_response: LLM response
            attachments: File attachments
            
        Returns:
            (updated_case, turn_metadata)
        """
        
        state_updates = llm_response.state_updates
        
        # Track changes for metadata
        milestones_completed = []
        evidence_added = []
        hypotheses_added = []
        solutions_added = []
        
        # 1. Apply milestone updates
        if state_updates.milestones:
            milestones_completed = self._apply_milestone_updates(
                case, state_updates.milestones
            )
        
        # 2. Apply verification updates
        if state_updates.verification_updates:
            self._apply_verification_updates(
                case, state_updates.verification_updates
            )
        
        # 3. Process evidence
        for evidence_data in state_updates.evidence_to_add:
            evidence = self._create_evidence(case, evidence_data, attachments)
            case.evidence.append(evidence)
            evidence_added.append(evidence.evidence_id)
        
        # 4. Process hypothesis-evidence links (many-to-many evaluation)
        for link_data in state_updates.hypothesis_evidence_links:
            if link_data.hypothesis_id in case.hypotheses:
                # Skip if IRRELEVANT (optimization - don't create links for unrelated evidence)
                if link_data.stance != EvidenceStance.IRRELEVANT:
                    hypothesis = case.hypotheses[link_data.hypothesis_id]
                    hypothesis.evidence_links[link_data.evidence_id] = HypothesisEvidenceLink(
                        hypothesis_id=link_data.hypothesis_id,
                        evidence_id=link_data.evidence_id,
                        stance=link_data.stance,
                        reasoning=link_data.reasoning,
                        completeness=link_data.completeness,
                        analyzed_at=datetime.now(timezone.utc)
                    )
        
        # 5. Process hypotheses
        for hyp_data in state_updates.hypotheses_to_add:
            hypothesis = self._create_hypothesis(case, hyp_data)
            case.hypotheses[hypothesis.hypothesis_id] = hypothesis
            hypotheses_added.append(hypothesis.hypothesis_id)
        
        # Update existing hypotheses
        for hyp_id, hyp_update in state_updates.hypotheses_to_update.items():
            if hyp_id in case.hypotheses:
                self._update_hypothesis(case.hypotheses[hyp_id], hyp_update)
        
        # 7. Process solutions
        for solution_data in state_updates.solutions_to_add:
            solution = self._create_solution(case, solution_data)
            case.solutions.append(solution)
            solutions_added.append(solution.solution_id)
        
        # 8. Update working conclusion
        if state_updates.working_conclusion:
            case.working_conclusion = WorkingConclusion(
                **state_updates.working_conclusion.model_dump()
            )
        
        # 9. Update root cause conclusion
        if state_updates.root_cause_conclusion:
            case.root_cause_conclusion = RootCauseConclusion(
                **state_updates.root_cause_conclusion.model_dump(),
                confidence_level=self._determine_confidence_level(
                    state_updates.root_cause_conclusion.confidence_score
                )
            )
        
        # 10. Determine path selection (if verification just completed)
        if (case.progress.verification_complete and 
            case.path_selection is None):
            path_selection = self.state_manager.determine_investigation_path(
                case.problem_verification
            )
            case.path_selection = path_selection
        
        # 11. Detect progress
        progress_made = len(milestones_completed) > 0 or len(evidence_added) > 0
        
        # 12. Update turns_without_progress
        if progress_made:
            case.turns_without_progress = 0
        else:
            case.turns_without_progress += 1
        
        # 13. Check degraded mode trigger
        if (case.turns_without_progress >= 3 and 
            case.degraded_mode is None):
            self.state_manager.enter_degraded_mode(case, "no_progress")
        
        # 14. Record turn
        turn = TurnProgress(
            turn_number=case.current_turn + 1,
            timestamp=datetime.now(timezone.utc),
            milestones_completed=milestones_completed,
            evidence_added=evidence_added,
            hypotheses_generated=hypotheses_added,  # ✅ Correct field name
            hypotheses_validated=[h_id for h_id, h_update in state_updates.hypotheses_to_update.items()
                                  if h_update.status in ['VALIDATED', 'REFUTED']],
            solutions_proposed=solutions_added,
            progress_made=progress_made,
            actions_taken=self._extract_actions(llm_response.agent_response),
            outcome=state_updates.outcome,
            user_message_summary=self._summarize_text(user_message, max_length=200),  # ✅ Summary, not full text
            agent_response_summary=self._summarize_text(llm_response.agent_response, max_length=500)
        )
        case.turn_history.append(turn)
        
        # 15. Check status transitions
        self.state_manager.check_automatic_status_transitions(case)
        
        # Build metadata
        metadata = {
            "progress_made": progress_made,
            "milestones_completed": milestones_completed,
            "evidence_added": len(evidence_added),
            "hypotheses_added": len(hypotheses_added),
            "solutions_added": len(solutions_added),
            "stage": case.progress.current_stage,
            "outcome": state_updates.outcome
        }
        
        return case, metadata
    
    def _apply_milestone_updates(
        self, case: Case, updates: Any
    ) -> list:
        """Apply milestone updates and return completed milestones"""
        
        completed = []
        
        if updates.symptom_verified and not case.progress.symptom_verified:
            case.progress.symptom_verified = True
            completed.append("symptom_verified")
        
        if updates.scope_assessed and not case.progress.scope_assessed:
            case.progress.scope_assessed = True
            completed.append("scope_assessed")
        
        if updates.timeline_established and not case.progress.timeline_established:
            case.progress.timeline_established = True
            completed.append("timeline_established")
        
        if updates.changes_identified and not case.progress.changes_identified:
            case.progress.changes_identified = True
            completed.append("changes_identified")
        
        if updates.root_cause_identified and not case.progress.root_cause_identified:
            case.progress.root_cause_identified = True
            case.progress.root_cause_confidence = updates.root_cause_confidence
            case.progress.root_cause_method = updates.root_cause_method
            completed.append("root_cause_identified")
        
        if updates.solution_proposed and not case.progress.solution_proposed:
            case.progress.solution_proposed = True
            completed.append("solution_proposed")
        
        if updates.solution_applied and not case.progress.solution_applied:
            case.progress.solution_applied = True
            completed.append("solution_applied")
        
        if updates.solution_verified and not case.progress.solution_verified:
            case.progress.solution_verified = True
            case.progress.solution_verified_at = datetime.now(timezone.utc)
            completed.append("solution_verified")
        
        return completed
    
    def _apply_verification_updates(self, case: Case, updates: Any):
        """Apply verification updates"""
        
        if not case.problem_verification:
            from app.models import ProblemVerification
            case.problem_verification = ProblemVerification()
        
        pv = case.problem_verification
        
        if updates.symptom_statement:
            pv.symptom_statement = updates.symptom_statement
        
        if updates.temporal_state:
            pv.temporal_state = updates.temporal_state
        
        if updates.urgency_level:
            pv.urgency_level = updates.urgency_level
        
        if updates.started_at:
            pv.started_at = updates.started_at
        
        if updates.affected_services:
            pv.affected_services = updates.affected_services
    
    def _create_evidence(
        self, case: Case, evidence_data: Any, attachments: list
    ) -> Evidence:
        """Create evidence object with system-inferred fields"""
        
        # System infers category from investigation context
        category = self._infer_evidence_category(evidence_data, case)
        
        # System calculates milestone advancement
        advances_milestones = self._determine_milestone_advancement(
            evidence_data, case, category
        )
        
        # Create evidence
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            summary=evidence_data.summary,
            analysis=evidence_data.analysis,
            tests_hypothesis_id=evidence_data.tests_hypothesis_id,
            stance=evidence_data.stance,
            category=category,
            advances_milestones=advances_milestones,
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=case.current_turn + 1,
            # File handling would go here if attachments present
        )
        
        return evidence
    
    def _infer_evidence_category(
        self, evidence_data: Any, case: Case
    ) -> EvidenceCategory:
        """System infers evidence category from context"""
        
        # Rule 1: Testing hypothesis → CAUSAL
        if evidence_data.tests_hypothesis_id:
            return EvidenceCategory.CAUSAL_EVIDENCE
        
        # Rule 2: Verification incomplete → SYMPTOM
        if not case.progress.verification_complete:
            return EvidenceCategory.SYMPTOM_EVIDENCE
        
        # Rule 3: Solution proposed → RESOLUTION
        if case.progress.solution_proposed:
            return EvidenceCategory.RESOLUTION_EVIDENCE
        
        # Rule 4: Default → OTHER
        return EvidenceCategory.OTHER
    
    def _determine_milestone_advancement(
        self, evidence_data: Any, case: Case, category: EvidenceCategory
    ) -> list:
        """Calculate which milestones this evidence advances"""
        
        milestones = []
        
        if category == EvidenceCategory.SYMPTOM_EVIDENCE:
            analysis = (evidence_data.analysis or "").lower()
            
            if "symptom" in analysis and not case.progress.symptom_verified:
                milestones.append("symptom_verified")
            
            if "timeline" in analysis and not case.progress.timeline_established:
                milestones.append("timeline_established")
            
            if ("scope" in analysis or "affected" in analysis) and not case.progress.scope_assessed:
                milestones.append("scope_assessed")
            
            if ("deployment" in analysis or "change" in analysis) and not case.progress.changes_identified:
                milestones.append("changes_identified")
        
        elif category == EvidenceCategory.CAUSAL_EVIDENCE:
            if (evidence_data.stance in ["strongly_supports", "supports"] and
                not case.progress.root_cause_identified):
                milestones.append("root_cause_identified")
        
        elif category == EvidenceCategory.RESOLUTION_EVIDENCE:
            analysis = (evidence_data.analysis or "").lower()
            if ("resolved" in analysis or "fixed" in analysis) and not case.progress.solution_verified:
                milestones.append("solution_verified")
        
        return milestones
    
    def _create_hypothesis(self, case: Case, hyp_data: Any) -> Hypothesis:
        """Create hypothesis"""
        
        return Hypothesis(
            hypothesis_id=f"hyp_{uuid4().hex[:12]}",
            statement=hyp_data.statement,
            likelihood=hyp_data.likelihood,
            reasoning=hyp_data.reasoning,
            evidence_requirements=hyp_data.evidence_requirements,
            status=HypothesisStatus.ACTIVE,
            generated_at=datetime.now(timezone.utc),
            generated_at_turn=case.current_turn + 1
        )
    
    def _update_hypothesis(self, hypothesis: Hypothesis, update: Any):
        """Update existing hypothesis"""
        
        if update.status:
            hypothesis.status = HypothesisStatus(update.status.lower())
        
        if update.likelihood is not None:
            hypothesis.likelihood = update.likelihood
        
        if update.reasoning:
            hypothesis.reasoning = update.reasoning
    
    def _create_solution(self, case: Case, solution_data: Any) -> Solution:
        """Create solution"""
        
        return Solution(
            solution_id=f"sol_{uuid4().hex[:12]}",
            title=solution_data.title,
            solution_type=solution_data.solution_type,
            immediate_action=solution_data.immediate_action,
            longterm_fix=solution_data.longterm_fix,
            implementation_steps=solution_data.implementation_steps,
            risks=solution_data.risks,
            proposed_at=datetime.now(timezone.utc),
            proposed_by="agent"
        )
    
    def _determine_confidence_level(self, score: float) -> str:
        """Determine confidence level from score"""
        
        if score >= 0.9:
            return "verified"
        elif score >= 0.7:
            return "high"
        elif score >= 0.5:
            return "moderate"
        else:
            return "low"
    
    def _extract_actions(self, agent_response: str) -> List[str]:
        """Extract action verbs from agent response for turn tracking"""
        
        action_keywords = ['requested', 'verified', 'identified', 'proposed', 'tested', 'confirmed']
        actions = []
        
        response_lower = agent_response.lower()
        for keyword in action_keywords:
            if keyword in response_lower:
                actions.append(keyword)
        
        return actions[:5]  # Limit to 5 actions
    
    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Summarize long text for storage"""
        
        if len(text) <= max_length:
            return text
        
        # Truncate and add ellipsis
        return text[:max_length - 3] + "..."
```

### 4.3 Terminal Processor

```python
# app/agent/processors/terminal.py

"""
TERMINAL Response Processor

Processes LLM responses for closed cases.
"""

import logging
from typing import Dict, Any, Tuple

from app.models import Case
from app.agent.llm.schemas import TerminalResponse
from app.agent.state.manager import StateManager

logger = logging.getLogger(__name__)


class TerminalProcessor:
    """
    Process TERMINAL responses (RESOLVED/CLOSED status).
    
    Responsibilities:
    - Process documentation requests
    - Prevent state modifications
    - Track documentation generation
    """
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
    
    async def process(
        self,
        case: Case,
        user_message: str,
        llm_response: TerminalResponse,
        attachments: list = None
    ) -> Tuple[Case, Dict[str, Any]]:
        """
        Process terminal response (read-only).
        
        Args:
            case: Current case (terminal status)
            user_message: User's message
            llm_response: LLM response
            attachments: File attachments
            
        Returns:
            (case, turn_metadata)  # Case unchanged
        """
        
        state_updates = llm_response.state_updates
        
        # Process documentation requests
        documents_requested = []
        if state_updates.documentation_updates:
            doc_updates = state_updates.documentation_updates
            
            # Store documentation data (for later generation)
            if not case.documentation:
                from app.models import Documentation
                case.documentation = Documentation()
            
            if doc_updates.lessons_learned:
                case.documentation.lessons_learned = doc_updates.lessons_learned
            
            if doc_updates.what_went_well:
                case.documentation.what_went_well = doc_updates.what_went_well
            
            if doc_updates.what_could_improve:
                case.documentation.what_could_improve = doc_updates.what_could_improve
            
            if doc_updates.preventive_measures:
                case.documentation.preventive_measures = doc_updates.preventive_measures
            
            if doc_updates.monitoring_recommendations:
                case.documentation.monitoring_recommendations = doc_updates.monitoring_recommendations
            
            documents_requested = [doc.value for doc in doc_updates.documents_to_generate]
        
        # Build metadata
        metadata = {
            "terminal_status": case.status,
            "documentation_requested": len(documents_requested) > 0,
            "document_types": documents_requested
        }
        
        return case, metadata
```

### 4.4 Processor Registry

```python
# app/agent/processors/__init__.py

"""
Response Processors

Export all processors.
"""

from app.agent.processors.consulting import ConsultingProcessor
from app.agent.processors.investigating import InvestigatingProcessor
from app.agent.processors.terminal import TerminalProcessor

__all__ = [
    "ConsultingProcessor",
    "InvestigatingProcessor",
    "TerminalProcessor",
]
```

---

## 5. State Management

```python
# app/agent/state/manager.py

"""
State Manager

Manages case state, transitions, and persistence.
"""

import logging
from typing import Optional
from datetime import datetime, timezone

from app.models import (
    Case, CaseStatus, InvestigationProgress, InvestigationPath, 
    PathSelection, TemporalState, UrgencyLevel, ProblemVerification,
    DegradedMode, DegradedModeType
)
from app.repositories import CaseRepository

logger = logging.getLogger(__name__)


class StateManager:
    """
    Manage investigation state and transitions.
    
    Responsibilities:
    - Case CRUD operations
    - Status transitions
    - Path selection logic
    - Degraded mode management
    """
    
    def __init__(self, case_repo: CaseRepository):
        self.case_repo = case_repo
    
    async def save_case(self, case: Case) -> Case:
        """Save case to database"""
        return await self.case_repo.save(case)
    
    async def load_case(self, case_id: str) -> Optional[Case]:
        """Load case from database"""
        return await self.case_repo.get(case_id)
    
    async def transition_to_investigating(self, case: Case):
        """
        Transition case from CONSULTING to INVESTIGATING.
        
        Creates initial investigation structures.
        """
        
        if case.status != CaseStatus.CONSULTING:
            raise ValueError(f"Cannot transition from {case.status} to INVESTIGATING")
        
        # Change status
        case.status = CaseStatus.INVESTIGATING
        
        # Initialize investigation progress
        case.progress = InvestigationProgress()
        
        # Initialize problem verification with confirmed statement
        from app.models import ProblemVerification
        case.problem_verification = ProblemVerification(
            symptom_statement=case.consulting.proposed_problem_statement
        )
        
        # Initialize empty collections
        case.evidence = []
        case.hypotheses = {}
        case.solutions = []
        case.turn_history = []
        
        logger.info(f"Case {case.case_id} transitioned to INVESTIGATING")
    
    def determine_investigation_path(
        self, problem_verification: ProblemVerification
    ) -> PathSelection:
        """
        Determine investigation path from matrix.
        System logic, not LLM decision.
        """
        
        temporal = problem_verification.temporal_state
        urgency = problem_verification.urgency_level
        
        # Matrix logic
        if temporal == TemporalState.ONGOING:
            if urgency in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
                # Ongoing + High urgency → MITIGATION_FIRST
                return PathSelection(
                    path=InvestigationPath.MITIGATION_FIRST,
                    auto_selected=True,
                    rationale=f"Ongoing {urgency} issue requires immediate mitigation first, then RCA",
                    alternate_path=InvestigationPath.ROOT_CAUSE,
                    temporal_state=temporal,
                    urgency_level=urgency,
                    selected_at=datetime.now(timezone.utc)
                )
            else:
                # Ongoing + Low urgency → USER_CHOICE
                return PathSelection(
                    path=InvestigationPath.USER_CHOICE,
                    auto_selected=False,
                    rationale=f"Ongoing {urgency} issue - user should choose approach",
                    temporal_state=temporal,
                    urgency_level=urgency,
                    selected_at=datetime.now(timezone.utc)
                )
        
        else:  # HISTORICAL
            if urgency in [UrgencyLevel.LOW, UrgencyLevel.MEDIUM]:
                # Historical + Low urgency → ROOT_CAUSE
                return PathSelection(
                    path=InvestigationPath.ROOT_CAUSE,
                    auto_selected=True,
                    rationale=f"Historical {urgency} issue allows thorough investigation",
                    temporal_state=temporal,
                    urgency_level=urgency,
                    selected_at=datetime.now(timezone.utc)
                )
            else:
                # Historical + High urgency → USER_CHOICE (ambiguous)
                return PathSelection(
                    path=InvestigationPath.USER_CHOICE,
                    auto_selected=False,
                    rationale=f"Historical {urgency} issue - clarify urgency with user",
                    temporal_state=temporal,
                    urgency_level=urgency,
                    selected_at=datetime.now(timezone.utc)
                )
    
    def enter_degraded_mode(
        self, case: Case, mode_type: str, reason: Optional[str] = None
    ):
        """Enter degraded mode with confidence cap"""
        
        if case.degraded_mode:
            logger.warning(f"Case {case.case_id} already in degraded mode")
            return
        
        # Determine reason if not provided
        if not reason:
            if mode_type == "no_progress":
                reason = f"No progress for {case.turns_without_progress} turns"
            elif mode_type == "limited_data":
                incomplete_milestones = [
                    m for m in ["symptom_verified", "scope_assessed", "timeline_established", "root_cause_identified"]
                    if not getattr(case.progress, m, False)
                ]
                reason = f"Missing data for milestones: {', '.join(incomplete_milestones[:2])}"
            else:
                reason = "Investigation limitations encountered"
        
        case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType(mode_type),
            reason=reason,
            entered_at=datetime.now(timezone.utc),
            entered_at_turn=case.current_turn
        )
        
        logger.info(
            f"Case {case.case_id} entered degraded mode: {mode_type} - {reason}"
        )
    
    def check_automatic_status_transitions(self, case: Case):
        """Check if case should transition status"""
        
        # INVESTIGATING → RESOLVED
        if (case.status == CaseStatus.INVESTIGATING and
            case.progress.solution_verified):
            
            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(timezone.utc)
            case.closed_at = datetime.now(timezone.utc)
            case.closure_reason = "resolved"
            
            # Calculate time to resolution
            if case.created_at:
                delta = case.closed_at - case.created_at
                case.time_to_resolution = int(delta.total_seconds())
            
            logger.info(
                f"Case {case.case_id} automatically transitioned to RESOLVED"
            )
```

---

## 6. Error Handling

```python
# app/agent/error_handling.py

"""
Error Handling for Agent

Comprehensive error handling with retry logic and fallbacks.
"""

import logging
from typing import Callable, TypeVar, Any
from functools import wraps
import asyncio

logger = logging.getLogger(__name__)

T = TypeVar('T')


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_retries: Maximum retry attempts
        delay: Initial delay between retries (seconds)
        backoff: Multiplier for delay after each retry
        exceptions: Exceptions to catch and retry
    """
    
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            current_delay = delay
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                
                except exceptions as e:
                    if attempt == max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries} retries: {e}"
                        )
                        raise
                    
                    logger.warning(
                        f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {current_delay}s..."
                    )
                    
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            
            raise RuntimeError(f"{func.__name__} failed unexpectedly")
        
        return wrapper
    return decorator


class ErrorRecoveryManager:
    """
    Manage error recovery strategies for agent.
    """
    
    @staticmethod
    def handle_llm_error(
        case: Case,
        error: Exception,
        attempt: int
    ) -> dict:
        """
        Handle LLM errors with appropriate fallback.
        
        Returns:
            Fallback response dict
        """
        
        logger.error(f"LLM error on case {case.case_id}: {error}")
        
        # Rate limit error
        if "rate_limit" in str(error).lower():
            return {
                "agent_response": (
                    "I'm experiencing high demand right now. "
                    "Please try again in a moment."
                ),
                "error_type": "rate_limit",
                "retry_suggested": True
            }
        
        # Validation error (malformed response)
        if "validation" in str(error).lower():
            if attempt < 2:
                # Retry with simpler prompt
                return {
                    "agent_response": None,
                    "error_type": "validation",
                    "retry_suggested": True,
                    "strategy": "simplify_prompt"
                }
            else:
                # Give up, provide generic response
                return {
                    "agent_response": (
                        "I'm having trouble processing this request. "
                        "Could you rephrase your question?"
                    ),
                    "error_type": "validation",
                    "retry_suggested": False
                }
        
        # Generic error
        return {
            "agent_response": (
                "I encountered an error processing your request. "
                "Please try again or contact support if this persists."
            ),
            "error_type": "generic",
            "retry_suggested": True
        }
    
    @staticmethod
    def handle_state_error(
        case: Case,
        error: Exception
    ) -> dict:
        """Handle state management errors"""
        
        logger.error(f"State error on case {case.case_id}: {error}")
        
        return {
            "agent_response": (
                "I successfully processed your message, but encountered "
                "an issue saving the state. Your progress may not be saved."
            ),
            "error_type": "state_management",
            "error_details": str(error)
        }
```

---

## 7. Testing Framework

```python
# tests/test_agent.py

"""
Agent Testing Framework

Comprehensive tests for agent functionality.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch

from app.models import Case, CaseStatus, InvestigationProgress
from app.agent.core import FaultMavenAgent
from app.agent.llm.schemas import InvestigationResponse, InvestigationStateUpdate
from app.agent.llm.client import LLMClient
from app.agent.state.manager import StateManager


@pytest.fixture
def mock_llm_client():
    """Mock LLM client"""
    client = Mock(spec=LLMClient)
    client.generate = AsyncMock()
    return client


@pytest.fixture
def mock_state_manager():
    """Mock state manager"""
    manager = Mock(spec=StateManager)
    manager.save_case = AsyncMock()
    manager.determine_investigation_path = Mock()
    manager.check_automatic_status_transitions = Mock()
    return manager


@pytest.fixture
def agent(mock_llm_client, mock_state_manager):
    """Create agent with mocked dependencies"""
    return FaultMavenAgent(
        llm_client=mock_llm_client,
        state_manager=mock_state_manager
    )


@pytest.fixture
def investigating_case():
    """Create test case in INVESTIGATING status"""
    return Case(
        case_id="test_case_123",
        status=CaseStatus.INVESTIGATING,
        current_turn=5,
        user_id="user_123",
        progress=InvestigationProgress(
            symptom_verified=False,
            root_cause_identified=False
        ),
        evidence=[],
        hypotheses={},
        solutions=[],
        turn_history=[],
        created_at=datetime.now(timezone.utc)
    )


class TestAgentProcessTurn:
    """Test agent turn processing"""
    
    @pytest.mark.asyncio
    async def test_successful_turn_processing(
        self, agent, investigating_case, mock_llm_client
    ):
        """Test successful turn processing"""
        
        # Mock LLM response
        mock_llm_response = InvestigationResponse(
            agent_response="I've analyzed the logs and found the issue...",
            state_updates=InvestigationStateUpdate(
                milestones={"symptom_verified": True, "root_cause_identified": True},
                evidence_to_add=[{
                    "summary": "Error log showing NullPointerException",
                    "analysis": "Missing null check at line 42"
                }],
                outcome="milestone_completed"
            )
        )
        mock_llm_client.generate.return_value = mock_llm_response
        
        # Process turn
        result = await agent.process_turn(
            case=investigating_case,
            user_message="Here's the error log"
        )
        
        # Assertions
        assert result["agent_response"] == mock_llm_response.agent_response
        assert result["case_updated"].current_turn == 6
        assert result["case_updated"].progress.symptom_verified == True
        assert len(result["case_updated"].evidence) == 1
        assert result["metadata"]["progress_made"] == True
        
        # Verify LLM was called
        mock_llm_client.generate.assert_called_once()
        
        # Verify case was saved
        agent.state_manager.save_case.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_one_turn_resolution(
        self, agent, investigating_case, mock_llm_client
    ):
        """Test case can be resolved in one turn"""
        
        # Mock comprehensive LLM response
        mock_llm_response = InvestigationResponse(
            agent_response="The deployment introduced a bug...",
            state_updates=InvestigationStateUpdate(
                milestones={
                    "symptom_verified": True,
                    "timeline_established": True,
                    "root_cause_identified": True,
                    "solution_proposed": True
                },
                evidence_to_add=[{
                    "summary": "Comprehensive error log",
                    "analysis": "Complete analysis showing root cause"
                }],
                root_cause_conclusion={
                    "root_cause": "Missing null check",
                    "mechanism": "Null pointer exception",
                    "confidence_score": 0.95
                },
                solutions_to_add=[{
                    "title": "Rollback deployment",
                    "solution_type": "rollback",
                    "immediate_action": "kubectl rollout undo"
                }],
                outcome="milestone_completed"
            )
        )
        mock_llm_client.generate.return_value = mock_llm_response
        
        # Process turn
        result = await agent.process_turn(
            case=investigating_case,
            user_message="Here's comprehensive error log showing everything"
        )
        
        # Verify multiple milestones completed in one turn
        case = result["case_updated"]
        assert case.progress.symptom_verified == True
        assert case.progress.timeline_established == True
        assert case.progress.root_cause_identified == True
        assert case.progress.solution_proposed == True
        
        assert len(case.evidence) == 1
        assert len(case.solutions) == 1
        assert case.root_cause_conclusion is not None


class TestEvidenceCategorization:
    """Test evidence categorization logic"""
    
    def test_symptom_evidence_during_verification(self):
        """Test evidence categorized as SYMPTOM during verification"""
        
        from app.agent.processors.investigating import InvestigatingProcessor
        
        case = Case(
            case_id="test",
            status=CaseStatus.INVESTIGATING,
            progress=InvestigationProgress(symptom_verified=False)
        )
        
        processor = InvestigatingProcessor(Mock())
        
        evidence_data = Mock(tests_hypothesis_id=None)
        category = processor._infer_evidence_category(evidence_data, case)
        
        assert category == "SYMPTOM_EVIDENCE"


class TestPathSelection:
    """Test path selection logic"""
    
    def test_ongoing_critical_selects_mitigation_first(self):
        """Test ONGOING + CRITICAL auto-selects MITIGATION_FIRST"""
        
        from app.models import ProblemVerification, TemporalState, UrgencyLevel
        
        pv = ProblemVerification(
            symptom_statement="API down",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.CRITICAL
        )
        
        manager = StateManager(Mock())
        path = manager.determine_investigation_path(pv)

        assert path.path == InvestigationPath.MITIGATION_FIRST
        assert path.auto_selected == True


# Run tests: pytest tests/test_agent.py -v
```

---

## 8. Complete Usage Examples

```python
# examples/agent_usage.py

"""
Complete usage examples for FaultMaven Agent.
"""

import asyncio
from app.models import Case, CaseStatus, ConsultingData
from app.agent.core import FaultMavenAgent
from app.agent.llm.client import LLMClient
from app.agent.state.manager import StateManager
from app.repositories import CaseRepository


async def example_complete_investigation():
    """
    Complete investigation flow from start to finish.
    """
    
    # Setup
    llm_client = LLMClient()
    case_repo = CaseRepository()
    state_manager = StateManager(case_repo)
    agent = FaultMavenAgent(llm_client, state_manager)
    
    # Turn 1: Initial problem description (CONSULTING)
    case = Case(
        case_id="case_001",
        status=CaseStatus.CONSULTING,
        current_turn=0,
        user_id="user_123",
        consulting=ConsultingData()  # Starts empty - LLM fills via conversation
    )
    
    result = await agent.process_turn(
        case=case,
        user_message="Our API has been acting weird lately"
    )
    
    print(f"Turn 1: {result['agent_response']}")
    case = result["case_updated"]
    
    # Turn 2: User provides details
    result = await agent.process_turn(
        case=case,
        user_message="It's timing out sometimes, like 10% of requests fail"
    )
    
    print(f"Turn 2: {result['agent_response']}")
    case = result["case_updated"]
    
    # Turn 3: User confirms and requests investigation
    result = await agent.process_turn(
        case=case,
        user_message="Yes, that's right. Please investigate."
    )
    
    print(f"Turn 3: {result['agent_response']}")
    case = result["case_updated"]
    assert case.status == CaseStatus.INVESTIGATING
    
    # Turn 4: User provides error log
    result = await agent.process_turn(
        case=case,
        user_message="Here's the error log [uploads file]",
        attachments=["error.log"]
    )
    
    print(f"Turn 4: {result['agent_response']}")
    case = result["case_updated"]
    
    print(f"\nInvestigation completed in {case.current_turn} turns!")


if __name__ == "__main__":
    asyncio.run(example_complete_investigation())
```

---

## 9. Configuration

```python
# app/config.py

"""
Application Configuration

Environment-based configuration for FaultMaven.
"""

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings"""
    
    # API Keys
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    
    # LLM Configuration
    LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "4000"))
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://localhost/faultmaven")
    
    # Redis (for caching)
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    # Application
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Prompt Engineering
    TEMPLATE_VERSION: str = "2.0.0"
    ARCHITECTURE_VERSION: str = "Investigation v2.0"
    CASE_MODEL_VERSION: str = "v2.0"
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
```

```bash
# .env.example

# API Keys
ANTHROPIC_API_KEY=your_api_key_here

# LLM Configuration
LLM_MODEL=claude-sonnet-4-20250514
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=4000

# Database
DATABASE_URL=postgresql://user:password@localhost/faultmaven

# Redis
REDIS_URL=redis://localhost:6379

# Application
DEBUG=False
LOG_LEVEL=INFO
```

---

## 10. Deployment Guide

### 10.1 Repository Setup

```python
# app/repositories/case_repository.py

"""
Case Repository

Database operations for Case model.
"""

import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Case

logger = logging.getLogger(__name__)


class CaseRepository:
    """
    Repository for Case persistence.
    """
    
    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session
    
    async def save(self, case: Case) -> Case:
        """
        Save case to database.
        
        Args:
            case: Case to save
            
        Returns:
            Saved case
        """
        # Implementation depends on your ORM
        # Example with SQLAlchemy:
        
        # Convert Pydantic model to ORM model
        # db_case = CaseORM.from_pydantic(case)
        # self.session.add(db_case)
        # await self.session.commit()
        # await self.session.refresh(db_case)
        # return Case.from_orm(db_case)
        
        logger.info(f"Saved case {case.case_id}")
        return case
    
    async def get(self, case_id: str) -> Optional[Case]:
        """
        Get case by ID.
        
        Args:
            case_id: Case ID
            
        Returns:
            Case or None if not found
        """
        # Implementation depends on your ORM
        # Example:
        
        # result = await self.session.execute(
        #     select(CaseORM).where(CaseORM.case_id == case_id)
        # )
        # db_case = result.scalars().first()
        # return Case.from_orm(db_case) if db_case else None
        
        logger.info(f"Retrieved case {case_id}")
        return None
```

### 10.2 Service Layer

```python
# app/services/agent_service.py

"""
Agent Service - High-level interface for application.
"""

import logging
from typing import Dict, Any
from app.agent.core import FaultMavenAgent
from app.agent.llm.client import LLMClient
from app.agent.state.manager import StateManager
from app.repositories import CaseRepository

logger = logging.getLogger(__name__)


class AgentService:
    """
    High-level service for FaultMaven agent.
    """
    
    def __init__(self):
        self.case_repo = CaseRepository()
        self.llm_client = LLMClient()
        self.state_manager = StateManager(self.case_repo)
        self.agent = FaultMavenAgent(self.llm_client, self.state_manager)
    
    async def process_message(
        self,
        case_id: str,
        user_message: str,
        attachments: list = None
    ) -> Dict[str, Any]:
        """
        Process user message and return agent response.
        """
        
        case = await self.state_manager.load_case(case_id)
        if not case:
            raise ValueError(f"Case not found: {case_id}")
        
        result = await self.agent.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments
        )
        
        case_updated = result["case_updated"]
        
        return {
            "message": result["agent_response"],
            "case_id": case_updated.case_id,
            "status": case_updated.status,
            "progress": {
                "symptom_verified": case_updated.progress.symptom_verified,
                "root_cause_identified": case_updated.progress.root_cause_identified,
                "solution_verified": case_updated.progress.solution_verified,
                "current_stage": case_updated.progress.current_stage,
            },
            "metadata": result["metadata"]
        }
```

### 10.3 API Endpoints

```python
# app/api/endpoints/chat.py

"""
Chat endpoint for FaultMaven.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.agent_service import AgentService

router = APIRouter()
agent_service = AgentService()


class ChatRequest(BaseModel):
    case_id: str
    message: str
    attachments: list = []


class ChatResponse(BaseModel):
    message: str
    case_id: str
    status: str
    progress: dict
    metadata: dict


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Process user message and return agent response."""
    
    try:
        result = await agent_service.process_message(
            case_id=request.case_id,
            user_message=request.message,
            attachments=request.attachments
        )
        
        return ChatResponse(**result)
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
```

### 10.4 Application Entry Point

```python
# app/main.py

"""
FaultMaven Application Entry Point
"""

import logging
from fastapi import FastAPI
from app.api.endpoints import chat
from app.config import settings

# Configure logging
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting copilot",
    version="2.0.0"
)

# Include routers
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])


@app.on_event("startup")
async def startup_event():
    """Application startup"""
    logger.info("FaultMaven starting up...")
    logger.info(f"Template version: {settings.TEMPLATE_VERSION}")
    logger.info(f"LLM model: {settings.LLM_MODEL}")


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown"""
    logger.info("FaultMaven shutting down...")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 10.5 Docker Deployment

```dockerfile
# Dockerfile

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/
COPY tests/ ./tests/

# Run application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml

version: '3.8'

services:
  faultmaven:
    build: .
    ports:
      - "8000:8000"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - DATABASE_URL=postgresql://postgres:password@db:5432/faultmaven
      - REDIS_URL=redis://redis:6379
    depends_on:
      - db
      - redis
  
  db:
    image: postgres:15
    environment:
      - POSTGRES_DB=faultmaven
      - POSTGRES_PASSWORD=password
    volumes:
      - postgres_data:/var/lib/postgresql/data
  
  redis:
    image: redis:7
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

### 10.6 Requirements

```txt
# requirements.txt

# Core dependencies
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
pydantic-settings==2.1.0

# LLM
anthropic==0.7.0

# Database
sqlalchemy[asyncio]==2.0.23
asyncpg==0.29.0

# Redis
redis[hiredis]==5.0.1

# Testing
pytest==7.4.3
pytest-asyncio==0.21.1
pytest-cov==4.1.0

# Logging
python-json-logger==2.0.7
```

---

## Summary

You now have the **COMPLETE implementation** with all necessary code:

### ✅ What's Included

**Part 1**: Strategic Guide
- Design philosophy
- Prompt engineering principles
- Milestone-based approach
- LLM vs System responsibilities

**Part 2**: Prompt Templates
- CONSULTING template (ready to use)
- INVESTIGATING template with adaptive instructions (ready to use)
- TERMINAL template (ready to use)
- All helper functions

**Part 3**: Implementation Code
- Agent core (`core.py`)
- LLM client (`llm/client.py`)
- Response schemas (`llm/schemas.py`)
- All processors (consulting, investigating, terminal)
- State manager with transitions
- Error handling with retry logic
- Complete test suite
- Usage examples
- Configuration management
- Deployment setup (Docker, FastAPI)

### 📁 File Structure

```
faultmaven/
├── app/
│   ├── agent/
│   │   ├── core.py                    ✅
│   │   ├── prompts/
│   │   │   ├── templates.py           ✅
│   │   │   └── builder.py             ✅
│   │   ├── llm/
│   │   │   ├── client.py              ✅
│   │   │   └── schemas.py             ✅
│   │   ├── processors/
│   │   │   ├── consulting.py          ✅
│   │   │   ├── investigating.py       ✅
│   │   │   └── terminal.py            ✅
│   │   └── state/
│   │       └── manager.py             ✅
│   ├── models/
│   │   └── case.py                    (from Case Model v2.0)
│   ├── repositories/
│   │   └── case_repository.py         ✅
│   ├── services/
│   │   └── agent_service.py           ✅
│   ├── api/
│   │   └── endpoints/
│   │       └── chat.py                ✅
│   ├── config.py                      ✅
│   └── main.py                        ✅
├── tests/
│   └── test_agent.py                  ✅
├── examples/
│   └── agent_usage.py                 ✅
├── requirements.txt                   ✅
├── Dockerfile                         ✅
├── docker-compose.yml                 ✅
└── .env.example                       ✅
```

### 🚀 Quick Start

```bash
# 1. Clone and setup
git clone <your-repo>
cd faultmaven

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY

# 4. Run tests
pytest tests/ -v

# 5. Start application
python app/main.py
# Or: uvicorn app.main:app --reload

# 6. Test endpoint
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"case_id": "case_123", "message": "API is slow"}'
```

### 📚 Next Steps

1. **Integrate with your database** - Implement `CaseRepository` methods
2. **Add file upload handling** - Process attachments in processors
3. **Add authentication** - Secure API endpoints
4. **Deploy to production** - Use Docker or cloud platform
5. **Monitor and iterate** - Track LLM performance, adjust prompts

---

**END OF COMPLETE DOCUMENT**
