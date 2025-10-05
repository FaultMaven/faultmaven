"""Doctor/Patient interaction models for adaptive guidance.

This module defines models for the revolutionary doctor/patient prompting architecture
where a single powerful LLM maintains diagnostic state while providing active guidance
through suggested actions and commands.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """Types of suggested actions"""
    QUESTION_TEMPLATE = "question_template"  # Pre-filled question user can submit
    COMMAND = "command"  # Diagnostic command to run
    UPLOAD_DATA = "upload_data"  # Upload logs/configs
    TRANSITION = "transition"  # Transition to diagnostic/learning mode
    CREATE_RUNBOOK = "create_runbook"  # Generate runbook for resolved case


class CommandSafety(str, Enum):
    """Safety levels for suggested commands"""
    SAFE = "safe"  # Read-only, no side effects
    READ_ONLY = "read_only"  # Explicitly read-only
    CAUTION = "caution"  # Requires review before execution


class SuggestedAction(BaseModel):
    """User-clickable action suggestion for active guidance.
    
    Displayed as interactive buttons in the UI to guide conversation flow naturally.
    
    Examples:
        >>> action = SuggestedAction(
        ...     label="🔧 I have a Redis issue",
        ...     type=ActionType.QUESTION_TEMPLATE,
        ...     payload="I'm experiencing issues with my Redis cluster"
        ... )
    """
    label: str = Field(..., description="Display text for the button", max_length=100)
    type: ActionType = Field(..., description="Type of action")
    payload: str = Field(..., description="The actual question/command to submit", max_length=500)
    icon: Optional[str] = Field(None, description="UI icon hint (emoji or icon name)", max_length=10)
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional action metadata")


class CommandSuggestion(BaseModel):
    """Diagnostic command suggestion with explanation.

    Used during troubleshooting to suggest specific commands the user can run
    to gather diagnostic evidence. Includes explanation of WHY the command is useful.

    Examples:
        >>> cmd = CommandSuggestion(
        ...     command="kubectl get pods -n production",
        ...     description="Check pod status in production namespace",
        ...     why="This will show if any pods are failing or restarting",
        ...     safety=CommandSafety.SAFE
        ... )
    """
    command: str = Field(..., description="The command to run", max_length=500)
    description: str = Field(..., description="Brief description of what command does", max_length=200)
    why: str = Field(..., description="Explanation of why this command is useful", max_length=300)
    safety: CommandSafety = Field(default=CommandSafety.SAFE, description="Safety level of command")
    expected_output: Optional[str] = Field(None, description="What to look for in output", max_length=300)


class CommandValidationResponse(BaseModel):
    """Response when user asks to validate a command they want to run.

    Used when user asks "Can I run this command?" or "Should I run X?".
    Provides safety assessment and guidance.

    Examples:
        >>> validation = CommandValidationResponse(
        ...     command="kubectl delete pod my-pod",
        ...     is_safe=True,
        ...     safety_level=CommandSafety.CAUTION,
        ...     explanation="This will delete the pod, but it will be recreated by the deployment",
        ...     concerns=["Pod will be unavailable during recreation"],
        ...     safer_alternative="kubectl rollout restart deployment my-app"
        ... )
    """
    command: str = Field(..., description="The command user wants to validate", max_length=500)
    is_safe: bool = Field(..., description="Overall safety assessment")
    safety_level: CommandSafety = Field(..., description="Safety classification")
    explanation: str = Field(..., description="What the command does and its effects", max_length=500)
    concerns: List[str] = Field(default_factory=list, description="Potential risks or issues", max_length=5)
    safer_alternative: Optional[str] = Field(None, description="Alternative command if risky", max_length=500)
    conditions_for_safety: List[str] = Field(
        default_factory=list,
        description="Conditions under which command is safe",
        max_length=5
    )
    should_diagnose_first: bool = Field(
        default=False,
        description="Whether user should diagnose root cause before running command"
    )


class LLMResponse(BaseModel):
    """Structured LLM response with adaptive guidance.
    
    This model represents the LLM's complete response including:
    - Direct answer to user's question
    - Optional clarifying questions
    - Suggested next actions (clickable buttons)
    - Diagnostic commands (for troubleshooting mode)
    
    The LLM uses this structure to actively lead the conversation while
    respecting user intent (learning vs troubleshooting).
    
    Examples:
        >>> response = LLMResponse(
        ...     answer="Redis offers persistence and more data structures...",
        ...     suggested_actions=[
        ...         SuggestedAction(
        ...             label="I have a caching issue",
        ...             type=ActionType.QUESTION_TEMPLATE,
        ...             payload="I'm experiencing caching issues"
        ...         )
        ...     ]
        ... )
    """
    # Main answer to user's question (always present)
    answer: str = Field(..., description="Natural language response to user query")
    
    # Optional: Questions LLM wants to ask for clarification
    clarifying_questions: List[str] = Field(
        default_factory=list,
        description="Questions to better understand user's intent",
        max_length=5
    )
    
    # Optional: Suggested actions user can take (2-4 options ideal)
    suggested_actions: List[SuggestedAction] = Field(
        default_factory=list,
        description="Clickable action suggestions to guide conversation",
        max_length=6
    )
    
    # Optional: Commands to run for diagnostic evidence (troubleshooting mode only)
    suggested_commands: List[CommandSuggestion] = Field(
        default_factory=list,
        description="Diagnostic commands user can run",
        max_length=5
    )

    # Optional: Command validation (when user asks "Can I run X?")
    command_validation: Optional[CommandValidationResponse] = Field(
        None,
        description="Validation response when user asks to validate a command"
    )

    # Internal: Should this trigger diagnostic state update?
    requires_state_update: bool = Field(
        default=True,
        description="Whether response should trigger state extraction"
    )
    
    # Metadata
    response_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional response metadata (tools used, confidence, etc.)"
    )
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Response generation time")
    
    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}
    
    def has_guidance(self) -> bool:
        """Check if response includes any form of guidance"""
        return bool(
            self.clarifying_questions
            or self.suggested_actions
            or self.suggested_commands
        )
    
    def is_diagnostic_mode(self) -> bool:
        """Check if response indicates diagnostic/troubleshooting mode"""
        return bool(self.suggested_commands)
    
    def get_action_count(self) -> int:
        """Get total number of suggested actions"""
        return len(self.suggested_actions)
    
    def get_command_count(self) -> int:
        """Get total number of suggested commands"""
        return len(self.suggested_commands)


class DiagnosticMode(str, Enum):
    """Current interaction mode (inferred from conversation, not explicitly set)"""
    INFORMATIONAL = "informational"  # User learning/exploring
    EXPLORATORY = "exploratory"  # User evaluating options
    TROUBLESHOOTING = "troubleshooting"  # Active problem diagnosis
    PREVENTIVE = "preventive"  # Routine checkup/best practices
