"""Case management interfaces.

This module defines the interface contracts for case persistence and management,
following FaultMaven's interface-based dependency injection pattern.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from .case import (
    Case,
    CaseListFilter,
    CaseMessage,
    CaseSearchRequest,
    CaseSummary,
    ParticipantRole
)


class ICaseStore(ABC):
    """Interface for case data persistence operations.
    
    This interface defines the contract for storing and retrieving case data,
    typically implemented using Redis or another persistent store.
    """
    
    @abstractmethod
    async def create_case(self, case: Case) -> bool:
        """Create a new case in the store.
        
        Args:
            case: Case object to create
            
        Returns:
            True if case was created successfully
        """
        pass
    
    @abstractmethod
    async def get_case(self, case_id: str) -> Optional[Case]:
        """Retrieve a case by ID.
        
        Args:
            case_id: Unique case identifier
            
        Returns:
            Case object if found, None otherwise
        """
        pass
    
    @abstractmethod
    async def update_case(self, case_id: str, updates: Dict[str, Any]) -> bool:
        """Update case data.
        
        Args:
            case_id: Case identifier
            updates: Dictionary of fields to update
            
        Returns:
            True if update was successful
        """
        pass
    
    @abstractmethod
    async def delete_case(self, case_id: str) -> bool:
        """Delete a case from the store.
        
        Args:
            case_id: Case identifier
            
        Returns:
            True if case was deleted successfully
        """
        pass
    
    @abstractmethod
    async def list_cases(self, filters: Optional[CaseListFilter] = None) -> List[CaseSummary]:
        """List cases with optional filtering.
        
        Args:
            filters: Optional filter criteria
            
        Returns:
            List of case summaries matching criteria
        """
        pass
    
    @abstractmethod
    async def search_cases(self, search_request: CaseSearchRequest) -> List[CaseSummary]:
        """Search cases by content.
        
        Args:
            search_request: Search criteria and filters
            
        Returns:
            List of matching case summaries
        """
        pass
    
    @abstractmethod
    async def add_message_to_case(self, case_id: str, message: CaseMessage) -> bool:
        """Add a message to a case.
        
        Args:
            case_id: Case identifier
            message: Message to add
            
        Returns:
            True if message was added successfully
        """
        pass
    
    @abstractmethod
    async def get_case_messages(
        self, 
        case_id: str, 
        limit: int = 50, 
        offset: int = 0
    ) -> List[CaseMessage]:
        """Get messages for a case.
        
        Args:
            case_id: Case identifier
            limit: Maximum number of messages to return
            offset: Offset for pagination
            
        Returns:
            List of case messages
        """
        pass
    
    @abstractmethod
    async def get_user_cases(
        self, 
        user_id: str, 
        filters: Optional[CaseListFilter] = None
    ) -> List[CaseSummary]:
        """Get cases for a specific user.
        
        Args:
            user_id: User identifier
            filters: Optional additional filters
            
        Returns:
            List of user's cases
        """
        pass
    
    @abstractmethod
    async def add_case_participant(
        self, 
        case_id: str, 
        user_id: str, 
        role: ParticipantRole,
        added_by: Optional[str] = None
    ) -> bool:
        """Add a participant to a case.
        
        Args:
            case_id: Case identifier
            user_id: User to add
            role: Role to assign
            added_by: User who added the participant
            
        Returns:
            True if participant was added successfully
        """
        pass
    
    @abstractmethod
    async def remove_case_participant(self, case_id: str, user_id: str) -> bool:
        """Remove a participant from a case.
        
        Args:
            case_id: Case identifier
            user_id: User to remove
            
        Returns:
            True if participant was removed successfully
        """
        pass
    
    @abstractmethod
    async def update_case_activity(self, case_id: str, session_id: Optional[str] = None) -> bool:
        """Update case last activity timestamp.
        
        Args:
            case_id: Case identifier
            session_id: Optional session ID that triggered the activity
            
        Returns:
            True if activity was updated successfully
        """
        pass
    
    @abstractmethod
    async def cleanup_expired_cases(self, batch_size: int = 100) -> int:
        """Clean up expired cases.
        
        Args:
            batch_size: Maximum number of cases to process in one batch
            
        Returns:
            Number of cases cleaned up
        """
        pass
    
    @abstractmethod
    async def get_case_analytics(self, case_id: str) -> Dict[str, Any]:
        """Get analytics data for a case.
        
        Args:
            case_id: Case identifier
            
        Returns:
            Dictionary containing case analytics
        """
        pass


class ICaseService(ABC):
    """Interface for case business logic and orchestration.
    
    This interface defines the contract for case management business operations,
    coordinating between case storage, session management, and other services.
    """
    
    @abstractmethod
    async def create_case(
        self, 
        title: str,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        session_id: Optional[str] = None,
        initial_message: Optional[str] = None
    ) -> Case:
        """Create a new troubleshooting case.
        
        Args:
            title: Case title
            description: Optional case description
            owner_id: Optional owner user ID
            session_id: Optional session to associate with case
            initial_message: Optional initial message content
            
        Returns:
            Created case object
        """
        pass
    
    @abstractmethod
    async def get_case(self, case_id: str, user_id: Optional[str] = None) -> Optional[Case]:
        """Get a case with optional access control.
        
        Args:
            case_id: Case identifier
            user_id: Optional user ID for access control
            
        Returns:
            Case object if found and accessible, None otherwise
        """
        pass
    
    @abstractmethod
    async def update_case(
        self, 
        case_id: str, 
        updates: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> bool:
        """Update case with access control.
        
        Args:
            case_id: Case identifier
            updates: Updates to apply
            user_id: Optional user ID for access control
            
        Returns:
            True if update was successful
        """
        pass
    
    @abstractmethod
    async def share_case(
        self, 
        case_id: str, 
        target_user_id: str, 
        role: ParticipantRole,
        sharer_user_id: Optional[str] = None
    ) -> bool:
        """Share a case with another user.
        
        Args:
            case_id: Case identifier
            target_user_id: User to share with
            role: Role to assign to the user
            sharer_user_id: User performing the share action
            
        Returns:
            True if case was shared successfully
        """
        pass
    
    @abstractmethod
    async def add_message_to_case(
        self, 
        case_id: str, 
        message: CaseMessage,
        session_id: Optional[str] = None
    ) -> bool:
        """Add a message to a case conversation.
        
        Args:
            case_id: Case identifier
            message: Message to add
            session_id: Optional session ID
            
        Returns:
            True if message was added successfully
        """
        pass
    
    @abstractmethod
    async def get_or_create_case_for_session(
        self, 
        session_id: str,
        user_id: Optional[str] = None,
        force_new: bool = False
    ) -> str:
        """Get existing case for session or create new one.
        
        Args:
            session_id: Session identifier
            user_id: Optional user identifier
            force_new: Force creation of new case
            
        Returns:
            Case ID
        """
        pass
    
    @abstractmethod
    async def link_session_to_case(self, session_id: str, case_id: str) -> bool:
        """Link a session to an existing case.
        
        Args:
            session_id: Session identifier
            case_id: Case identifier
            
        Returns:
            True if linking was successful
        """
        pass
    
    @abstractmethod
    async def get_case_conversation_context(
        self, 
        case_id: str, 
        limit: int = 10
    ) -> str:
        """Get formatted conversation context for LLM.
        
        Args:
            case_id: Case identifier
            limit: Maximum number of messages to include
            
        Returns:
            Formatted conversation context string
        """
        pass
    
    @abstractmethod
    async def resume_case_in_session(self, case_id: str, session_id: str) -> bool:
        """Resume an existing case in a new session.
        
        Args:
            case_id: Case identifier
            session_id: Session identifier
            
        Returns:
            True if case was resumed successfully
        """
        pass
    
    @abstractmethod
    async def archive_case(
        self, 
        case_id: str, 
        reason: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> bool:
        """Archive a case.
        
        Args:
            case_id: Case identifier
            reason: Optional archive reason
            user_id: Optional user ID for access control
            
        Returns:
            True if case was archived successfully
        """
        pass
    
    @abstractmethod
    async def list_user_cases(
        self, 
        user_id: str, 
        filters: Optional[CaseListFilter] = None
    ) -> List[CaseSummary]:
        """List cases for a user.
        
        Args:
            user_id: User identifier
            filters: Optional filter criteria
            
        Returns:
            List of user's cases
        """
        pass
    
    @abstractmethod
    async def search_cases(
        self, 
        search_request: CaseSearchRequest,
        user_id: Optional[str] = None
    ) -> List[CaseSummary]:
        """Search cases with access control.
        
        Args:
            search_request: Search criteria
            user_id: Optional user ID for access control
            
        Returns:
            List of matching cases
        """
        pass
    
    @abstractmethod
    async def get_case_analytics(self, case_id: str) -> Dict[str, Any]:
        """Get case analytics and metrics.
        
        Args:
            case_id: Case identifier
            
        Returns:
            Case analytics dictionary
        """
        pass
    
    @abstractmethod
    async def cleanup_expired_cases(self) -> int:
        """Clean up expired cases.
        
        Returns:
            Number of cases cleaned up
        """
        pass


class ICaseNotificationService(ABC):
    """Interface for case-related notifications.
    
    This interface defines the contract for sending notifications about
    case events like sharing, updates, and status changes.
    """
    
    @abstractmethod
    async def notify_case_shared(
        self, 
        case_id: str, 
        target_user_id: str, 
        sharer_user_id: str,
        role: ParticipantRole
    ) -> bool:
        """Notify user about case being shared with them.
        
        Args:
            case_id: Case identifier
            target_user_id: User receiving the share
            sharer_user_id: User who shared the case
            role: Role assigned to the user
            
        Returns:
            True if notification was sent successfully
        """
        pass
    
    @abstractmethod
    async def notify_case_updated(
        self, 
        case_id: str, 
        update_type: str,
        updated_by: Optional[str] = None
    ) -> bool:
        """Notify participants about case updates.
        
        Args:
            case_id: Case identifier
            update_type: Type of update performed
            updated_by: User who made the update
            
        Returns:
            True if notifications were sent successfully
        """
        pass
    
    @abstractmethod
    async def notify_case_message(
        self, 
        case_id: str, 
        message: CaseMessage
    ) -> bool:
        """Notify participants about new case messages.
        
        Args:
            case_id: Case identifier
            message: New message added
            
        Returns:
            True if notifications were sent successfully
        """
        pass


class ICaseIntegrationService(ABC):
    """Interface for integrating cases with external systems.
    
    This interface defines the contract for integrating case data with
    external ticketing systems, knowledge bases, and other tools.
    """
    
    @abstractmethod
    async def export_case(self, case_id: str, format_type: str) -> Dict[str, Any]:
        """Export case data in specified format.
        
        Args:
            case_id: Case identifier
            format_type: Export format (json, pdf, markdown, etc.)
            
        Returns:
            Exported case data
        """
        pass
    
    @abstractmethod
    async def sync_to_knowledge_base(self, case_id: str) -> bool:
        """Sync resolved case to knowledge base.
        
        Args:
            case_id: Case identifier
            
        Returns:
            True if sync was successful
        """
        pass
    
    @abstractmethod
    async def create_external_ticket(
        self, 
        case_id: str, 
        system: str,
        ticket_data: Dict[str, Any]
    ) -> Optional[str]:
        """Create ticket in external system.
        
        Args:
            case_id: Case identifier
            system: External system identifier
            ticket_data: Ticket creation data
            
        Returns:
            External ticket ID if successful, None otherwise
        """
        pass