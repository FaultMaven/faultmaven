"""Case persistence data models.

This module defines Pydantic models for case persistence across sessions,
enabling conversation continuity and collaborative troubleshooting.

Key Models:
- Case: Main case entity with lifecycle and metadata
- CaseMessage: Individual conversation messages 
- CaseParticipant: User access and collaboration management
- CaseContext: Contextual information and artifacts
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, ValidationInfo, validator, model_validator

# Import evidence-centric models
from faultmaven.models.evidence import (
    EvidenceRequest,
    EvidenceProvided,
    InvestigationMode,
    CaseStatus as EvidenceCaseStatus,
)


class CaseStatus(str, Enum):
    """Case lifecycle status enumeration"""
    ACTIVE = "active"
    INVESTIGATING = "investigating"
    SOLVED = "solved"
    RESOLVED = "resolved"  # Alias for solved - used by frontend/API
    STALLED = "stalled"
    ARCHIVED = "archived"
    SHARED = "shared"


class CasePriority(str, Enum):
    """Case priority levels"""
    LOW = "low"
    NORMAL = "normal"  # Added for API compatibility
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MessageType(str, Enum):
    """Types of messages in a case conversation"""
    USER_QUERY = "user_query"
    AGENT_RESPONSE = "agent_response"
    SYSTEM_EVENT = "system_event"
    DATA_UPLOAD = "data_upload"
    CASE_NOTE = "case_note"
    STATUS_CHANGE = "status_change"


class ParticipantRole(str, Enum):
    """Participant roles in case collaboration"""
    OWNER = "owner"
    COLLABORATOR = "collaborator"
    VIEWER = "viewer"
    SUPPORT = "support"


class CaseMessage(BaseModel):
    """Individual message within a case conversation"""
    
    message_id: str = Field(default_factory=lambda: str(uuid4()), description="Unique message identifier")
    case_id: str = Field(..., description="Case this message belongs to")
    session_id: Optional[str] = Field(None, description="Session where message was created")
    author_id: Optional[str] = Field(None, description="User who created the message")
    message_type: MessageType = Field(..., description="Type of message")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Message creation time")
    
    # Message metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional message metadata")
    attachments: List[str] = Field(default_factory=list, description="Attached file/data IDs")
    
    # Context and processing
    confidence_score: Optional[float] = Field(None, description="Confidence score for agent responses")
    processing_time_ms: Optional[int] = Field(None, description="Time taken to process message")
    
    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


class CaseParticipant(BaseModel):
    """Case participant with role and permissions"""
    
    user_id: str = Field(..., description="User identifier")
    role: ParticipantRole = Field(..., description="User's role in the case")
    added_at: datetime = Field(default_factory=datetime.utcnow, description="When user was added")
    added_by: Optional[str] = Field(None, description="Who added this participant")
    last_accessed: Optional[datetime] = Field(None, description="Last time user accessed case")
    
    # Permissions - use None as default to detect when not explicitly set, but always return bool
    can_edit: bool = Field(default=None, description="Can edit case details")
    can_share: bool = Field(default=None, description="Can share case with others")
    can_archive: bool = Field(default=None, description="Can archive case")
    
    @model_validator(mode='after')
    def set_permissions_by_role(self):
        """Set permissions based on role - runs after all fields are set"""
        
        if self.role == ParticipantRole.OWNER:
            # Owners get all permissions unless explicitly set to False
            if self.can_edit is None:
                self.can_edit = True
            if self.can_share is None:
                self.can_share = True
            if self.can_archive is None:
                self.can_archive = True
        elif self.role == ParticipantRole.COLLABORATOR:
            # Fixed: For collaborators, default edit and share to True, archive to False
            if self.can_edit is None:
                self.can_edit = True
            if self.can_share is None:
                self.can_share = True
            if self.can_archive is None:
                self.can_archive = False
        elif self.role == ParticipantRole.SUPPORT:
            # Support gets default permissions (False) unless explicitly set
            if self.can_edit is None:
                self.can_edit = False
            if self.can_share is None:
                self.can_share = False
            if self.can_archive is None:
                self.can_archive = False
        else:  # VIEWER
            # Viewers get no permissions regardless of explicit settings
            self.can_edit = False
            self.can_share = False
            self.can_archive = False
            
        return self
    
    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


class UrgencyLevel(str, Enum):
    """Urgency level for problem resolution"""
    NORMAL = "normal"  # Standard troubleshooting pace
    HIGH = "high"  # User indicates urgency
    CRITICAL = "critical"  # Outage, data loss risk, emergency


class CaseDiagnosticState(BaseModel):
    """Server-side diagnostic state tracking (TRANSITIONING TO OODA FRAMEWORK)

    v3.2.0: Transitioning from rigid 5-phase to flexible OODA framework
    with 7 investigation phases (0-6) and dual engagement modes.

    MIGRATION STRATEGY:
    - New investigations use investigation_state_id reference
    - Legacy fields maintained for backward compatibility
    - Gradual migration to new InvestigationState model

    Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
    """

    # =========================================================================
    # NEW: OODA FRAMEWORK (v3.2.0)
    # =========================================================================

    # Reference to new InvestigationState (from investigation.py)
    investigation_state_id: Optional[str] = Field(
        None,
        description="ID of InvestigationState object (v3.2.0 OODA framework)"
    )

    # =========================================================================
    # LEGACY FIELDS (Backward Compatibility - DEPRECATED)
    # =========================================================================

    # Problem tracking
    has_active_problem: bool = Field(default=False, description="Whether user has an active technical problem")
    problem_statement: str = Field(default="", description="Concise statement of the current problem")
    problem_started_at: Optional[datetime] = Field(None, description="When problem tracking began")
    urgency_level: UrgencyLevel = Field(default=UrgencyLevel.NORMAL, description="Problem urgency level")

    # Phase progression (DEPRECATED: Legacy 0-5 phases)
    # NOTE: v3.2.0 uses 0-6 phases in InvestigationState
    # MIGRATION: Use investigation_state_id to access InvestigationState.lifecycle.current_phase
    current_phase: int = Field(
        default=0,
        description="DEPRECATED: Legacy phase (0-5). Use InvestigationState.lifecycle.current_phase for v3.2.0 (0-6)",
        deprecated=True
    )

    # Phase-specific data collected (DEPRECATED: Use InvestigationState)
    # MIGRATION: Use InvestigationState.evidence, .ooda_engine, .problem_confirmation
    symptoms: List[str] = Field(
        default_factory=list,
        description="DEPRECATED: Use InvestigationState.evidence.evidence_provided",
        deprecated=True
    )
    blast_radius: Dict[str, Any] = Field(
        default_factory=dict,
        description="DEPRECATED: Use InvestigationState.problem_confirmation.scope",
        deprecated=True
    )
    timeline_info: Dict[str, Any] = Field(
        default_factory=dict,
        description="DEPRECATED: Use InvestigationState.problem_confirmation.timeline",
        deprecated=True
    )
    hypotheses: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="DEPRECATED: Use InvestigationState.ooda_engine.hypotheses",
        deprecated=True
    )
    tests_performed: List[str] = Field(
        default_factory=list,
        description="DEPRECATED: Use InvestigationState.evidence.evidence_provided",
        deprecated=True
    )

    # Solution tracking
    root_cause: str = Field(default="", description="Identified root cause")
    solution_proposed: bool = Field(default=False, description="Whether solution has been proposed")
    solution_text: str = Field(default="", description="Proposed solution details")
    solution_implemented: bool = Field(default=False, description="Whether user confirmed implementation")

    # Case closure tracking
    case_resolved: bool = Field(default=False, description="Whether case is considered resolved")
    resolution_summary: str = Field(default="", description="Summary of how problem was resolved")

    # =========================================================================
    # EVIDENCE-CENTRIC FIELDS (v3.1.0)
    # =========================================================================

    # Investigation mode and status
    investigation_mode: InvestigationMode = Field(
        default=InvestigationMode.ACTIVE_INCIDENT,
        description="Investigation approach: active_incident (speed) vs post_mortem (depth)"
    )
    evidence_case_status: EvidenceCaseStatus = Field(
        default=EvidenceCaseStatus.INTAKE,
        description="Current evidence-centric case status"
    )

    # Evidence tracking
    evidence_requests: List[EvidenceRequest] = Field(
        default_factory=list,
        description="Active evidence requests generated by agent"
    )
    evidence_provided: List[EvidenceProvided] = Field(
        default_factory=list,
        description="Evidence user has submitted"
    )

    # Confidence tracking (required for POST_MORTEM mode)
    overall_confidence_score: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Overall confidence in findings (required ≥0.85 for post_mortem resolution)"
    )

    # Conflict resolution tracking
    awaiting_refutation_confirmation: bool = Field(
        default=False,
        description="Whether agent is waiting for user to confirm/dispute refuting evidence"
    )
    pending_refutations: List[str] = Field(
        default_factory=list,
        description="Hypothesis IDs pending refutation confirmation"
    )

    # Progress tracking for stall detection
    turns_without_phase_advance: int = Field(
        default=0,
        description="Number of turns without phase progression (stall detection)"
    )
    turns_in_current_phase: int = Field(
        default=0,
        description="Number of turns in current phase (stall detection)"
    )

    # Deliverables (generated on resolution)
    case_report_url: Optional[str] = Field(None, description="URL to generated case report")
    runbook_url: Optional[str] = Field(None, description="URL to generated runbook")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


class CaseContext(BaseModel):
    """Contextual information and artifacts for a case"""

    # Troubleshooting context
    problem_description: Optional[str] = Field(None, description="Initial problem description")
    system_info: Dict[str, Any] = Field(default_factory=dict, description="System information")
    environment_details: Dict[str, Any] = Field(default_factory=dict, description="Environment context")

    # Data and artifacts
    uploaded_files: List[str] = Field(default_factory=list, description="Uploaded file IDs")
    log_snippets: List[Dict[str, Any]] = Field(default_factory=list, description="Relevant log excerpts")
    error_patterns: List[str] = Field(default_factory=list, description="Identified error patterns")

    # SRE doctrine progress (deprecated - use CaseDiagnosticState instead)
    blast_radius_defined: bool = Field(default=False, description="Blast radius analysis completed")
    timeline_established: bool = Field(default=False, description="Timeline analysis completed")
    hypothesis_formulated: List[str] = Field(default_factory=list, description="Working hypotheses")
    hypothesis_validated: List[Dict[str, Any]] = Field(default_factory=list, description="Validation results")
    solutions_proposed: List[Dict[str, Any]] = Field(default_factory=list, description="Proposed solutions")

    # Analysis results
    root_causes: List[str] = Field(default_factory=list, description="Identified root causes")
    recommendations: List[str] = Field(default_factory=list, description="Recommended actions")
    knowledge_base_refs: List[str] = Field(default_factory=list, description="Related KB document IDs")


class Case(BaseModel):
    """Main case entity for troubleshooting session persistence"""
    
    # Core identification
    case_id: str = Field(default_factory=lambda: str(uuid4()), description="Unique case identifier")
    title: str = Field(..., description="Case title/summary")
    description: Optional[str] = Field(None, description="Detailed case description")
    
    # Phase 3: Auto-title generation tracking
    title_manually_set: bool = Field(default=False, description="Whether title was manually set by user")
    
    # Ownership and collaboration
    owner_id: Optional[str] = Field(None, description="Case owner user ID")
    participants: List[CaseParticipant] = Field(default_factory=list, description="Case participants")
    
    # Lifecycle management
    status: CaseStatus = Field(default=CaseStatus.ACTIVE, description="Current case status")
    priority: CasePriority = Field(default=CasePriority.MEDIUM, description="Case priority level")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Case creation time")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update time")
    last_activity_at: datetime = Field(default_factory=datetime.utcnow, description="Last activity time")
    
    # Persistence and expiration
    expires_at: Optional[datetime] = Field(None, description="Case expiration time")
    auto_archive_after_days: int = Field(default=30, description="Days until auto-archive")
    
    # Conversation and content
    messages: List[CaseMessage] = Field(default_factory=list, description="Case conversation messages")
    message_count: int = Field(default=0, description="Total message count")
    
    # Context and artifacts
    context: CaseContext = Field(default_factory=CaseContext, description="Case context and artifacts")

    # Diagnostic state (server-side only - not exposed in API)
    diagnostic_state: CaseDiagnosticState = Field(default_factory=CaseDiagnosticState, description="SRE diagnostic methodology state")

    # Metadata and tags
    tags: List[str] = Field(default_factory=list, description="Case tags for organization")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional case metadata")
    
    # Analytics and metrics
    resolution_time_hours: Optional[float] = Field(None, description="Time to resolution in hours")
    participant_count: int = Field(default=0, description="Number of participants")
    share_count: int = Field(default=0, description="Number of times shared")
    
    @validator('expires_at', pre=True, always=True)
    def set_expiration_date(cls, v, values):
        """Set expiration date based on auto_archive_after_days if not provided"""
        if v is None:
            created_at = values.get('created_at', datetime.utcnow())
            auto_archive_days = values.get('auto_archive_after_days', 30)
            return created_at + timedelta(days=auto_archive_days)
        return v
    
    @validator('participant_count', pre=True, always=True)
    def update_participant_count(cls, v, values):
        """Update participant count based on participants list"""
        participants = values.get('participants', [])
        return len(participants)
    
    def add_participant(self, user_id: str, role: ParticipantRole, added_by: Optional[str] = None) -> bool:
        """Add a participant to the case"""
        # Check if user is already a participant
        for participant in self.participants:
            if participant.user_id == user_id:
                return False  # Already exists
        
        # Create new participant
        new_participant = CaseParticipant(
            user_id=user_id,
            role=role,
            added_by=added_by
        )
        
        self.participants.append(new_participant)
        self.participant_count = len(self.participants)
        self.updated_at = datetime.utcnow()
        
        return True
    
    def remove_participant(self, user_id: str) -> bool:
        """Remove a participant from the case"""
        for i, participant in enumerate(self.participants):
            if participant.user_id == user_id:
                # Don't remove owner
                if participant.role == ParticipantRole.OWNER:
                    return False
                
                del self.participants[i]
                self.participant_count = len(self.participants)
                self.updated_at = datetime.utcnow()
                return True
        
        return False  # Participant not found
    
    def update_participant_role(self, user_id: str, new_role: ParticipantRole) -> bool:
        """Update a participant's role"""
        for participant in self.participants:
            if participant.user_id == user_id:
                # Don't change owner role
                if participant.role == ParticipantRole.OWNER:
                    return False
                
                participant.role = new_role
                self.updated_at = datetime.utcnow()
                return True
        
        return False  # Participant not found
    
    def add_message(self, message: CaseMessage) -> None:
        """Add a message to the case"""
        message.case_id = self.case_id
        self.messages.append(message)
        self.message_count = len(self.messages)
        self.last_activity_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
    
    def get_participant_role(self, user_id: str) -> Optional[ParticipantRole]:
        """Get a user's role in the case"""
        for participant in self.participants:
            if participant.user_id == user_id:
                return participant.role
        return None
    
    def can_user_access(self, user_id: str) -> bool:
        """Check if a user can access this case"""
        return any(p.user_id == user_id for p in self.participants)
    
    def can_user_edit(self, user_id: str) -> bool:
        """Check if a user can edit this case"""
        for participant in self.participants:
            if participant.user_id == user_id:
                return participant.can_edit
        return False
    
    def can_user_share(self, user_id: str) -> bool:
        """Check if a user can share this case"""
        for participant in self.participants:
            if participant.user_id == user_id:
                return participant.can_share
        return False
    
    def is_expired(self) -> bool:
        """Check if the case has expired"""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at
    
    def extend_expiration(self, additional_days: int = 30) -> None:
        """Extend case expiration by additional days"""
        if self.expires_at is None:
            self.expires_at = datetime.utcnow() + timedelta(days=additional_days)
        else:
            self.expires_at = self.expires_at + timedelta(days=additional_days)
        
        self.updated_at = datetime.utcnow()
    
    def mark_as_solved(self, resolution_summary: Optional[str] = None) -> None:
        """Mark case as solved"""
        self.status = CaseStatus.SOLVED
        self.updated_at = datetime.utcnow()
        
        if self.created_at:
            duration = datetime.utcnow() - self.created_at
            self.resolution_time_hours = duration.total_seconds() / 3600
        
        if resolution_summary:
            self.metadata['resolution_summary'] = resolution_summary
    
    def archive(self, reason: Optional[str] = None) -> None:
        """Archive the case"""
        self.status = CaseStatus.ARCHIVED
        self.updated_at = datetime.utcnow()
        
        if reason:
            self.metadata['archive_reason'] = reason
    
    def get_recent_messages(self, limit: int = 10) -> List[CaseMessage]:
        """Get recent messages from the case"""
        return sorted(self.messages, key=lambda m: m.timestamp, reverse=True)[:limit]
    
    def get_conversation_summary(self) -> Dict[str, Any]:
        """Get a summary of the case conversation"""
        message_types = {}
        for message in self.messages:
            msg_type = message.message_type.value
            message_types[msg_type] = message_types.get(msg_type, 0) + 1
        
        recent_activity = None
        if self.messages:
            recent_message = max(self.messages, key=lambda m: m.timestamp)
            recent_activity = {
                'last_message_time': recent_message.timestamp,
                'last_message_type': recent_message.message_type.value,
                'last_author': recent_message.author_id
            }
        
        return {
            'total_messages': self.message_count,
            'message_types': message_types,
            'participants': len(self.participants),
            'recent_activity': recent_activity,
            'case_duration_hours': (
                (self.last_activity_at - self.created_at).total_seconds() / 3600
                if self.created_at else 0
            )
        }
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() + 'Z',
            set: lambda v: list(v)  # Convert sets to lists for JSON serialization
        }


class CaseCreateRequest(BaseModel):
    """Request model for creating a new case"""
    
    title: str = Field(..., description="Case title", max_length=200)
    description: Optional[str] = Field(None, description="Case description", max_length=2000)
    priority: CasePriority = Field(default=CasePriority.MEDIUM, description="Case priority")
    tags: List[str] = Field(default_factory=list, description="Case tags")
    session_id: Optional[str] = Field(None, description="Associated session ID")
    initial_message: Optional[str] = Field(None, description="Initial case message")


class CaseUpdateRequest(BaseModel):
    """Request model for updating case details"""
    
    title: Optional[str] = Field(None, description="Updated case title")
    description: Optional[str] = Field(None, description="Updated case description")
    status: Optional[CaseStatus] = Field(None, description="Updated case status")
    priority: Optional[CasePriority] = Field(None, description="Updated case priority")
    tags: Optional[List[str]] = Field(None, description="Updated case tags")


class CaseShareRequest(BaseModel):
    """Request model for sharing a case with other users"""
    
    user_id: str = Field(..., description="User ID to share with")
    role: ParticipantRole = Field(default=ParticipantRole.VIEWER, description="Role to assign")
    message: Optional[str] = Field(None, description="Optional message to include")


class CaseListFilter(BaseModel):
    """Filter criteria for listing cases"""
    
    user_id: Optional[str] = Field(None, description="Filter by participant user ID")
    status: Optional[CaseStatus] = Field(None, description="Filter by case status")
    priority: Optional[CasePriority] = Field(None, description="Filter by case priority")
    owner_id: Optional[str] = Field(None, description="Filter by case owner")
    tags: Optional[List[str]] = Field(None, description="Filter by tags (any match)")
    created_after: Optional[datetime] = Field(None, description="Filter by creation date")
    created_before: Optional[datetime] = Field(None, description="Filter by creation date")
    
    # New filtering parameters for Phase 1 implementation
    include_empty: bool = Field(default=False, description="Include cases with message_count == 0")
    include_archived: bool = Field(default=False, description="Include archived cases")
    include_deleted: bool = Field(default=False, description="Include deleted cases (admin only)")
    
    limit: int = Field(default=50, description="Maximum number of results", le=100)
    offset: int = Field(default=0, description="Result offset for pagination", ge=0)


class CaseSearchRequest(BaseModel):
    """Request model for searching cases"""
    
    query: str = Field(..., description="Search query", min_length=1)
    filters: Optional[CaseListFilter] = Field(None, description="Additional filters")
    search_in_messages: bool = Field(default=True, description="Search in message content")
    search_in_context: bool = Field(default=True, description="Search in case context")


class CaseSummary(BaseModel):
    """Summary view of a case for list operations"""

    case_id: str
    title: str
    status: CaseStatus
    priority: CasePriority
    owner_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    message_count: int
    participant_count: int
    tags: List[str]

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}