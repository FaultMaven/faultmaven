"""
Case Model Converter

Handles conversion between Case entity models and API response models.
Follows clean architecture principles by centralizing transformation logic
and maintaining clear separation between persistence and presentation layers.
"""

from typing import List, Optional, Union
from datetime import datetime
from uuid import uuid4
from faultmaven.models.case import Case as CaseEntity, CaseSummary
from faultmaven.models.api import Case as CaseAPI


class CaseConverter:
    """
    Centralized converter for Case entity to API model transformations.
    
    This converter ensures:
    - Consistent field mapping across all API endpoints
    - Type safety with proper enum conversions
    - UTC timestamp formatting compliance
    - Session relationship resolution
    """

    @staticmethod
    def entity_to_api(case_entity: CaseEntity) -> CaseAPI:
        """
        Convert Case entity to API Case model.
        
        Args:
            case_entity: The persistence layer Case entity
            
        Returns:
            CaseAPI: The API response model with proper formatting
        """
        return CaseAPI(
            case_id=getattr(case_entity, 'case_id', str(uuid4())),
            title=getattr(case_entity, 'title', 'Untitled Case'),
            description=getattr(case_entity, 'description', None),
            status=case_entity.status.value if hasattr(case_entity.status, 'value') else str(getattr(case_entity, 'status', 'active')),
            priority=case_entity.priority.value if hasattr(case_entity.priority, 'value') else str(getattr(case_entity, 'priority', 'medium')),
            created_at=case_entity.created_at.isoformat() + 'Z' if hasattr(case_entity.created_at, 'isoformat') else str(getattr(case_entity, 'created_at', datetime.utcnow().isoformat() + 'Z')),
            updated_at=case_entity.updated_at.isoformat() + 'Z' if hasattr(case_entity.updated_at, 'isoformat') else str(getattr(case_entity, 'updated_at', datetime.utcnow().isoformat() + 'Z')),
            message_count=getattr(case_entity, 'message_count', 0),
            session_id=getattr(case_entity, 'current_session_id', None),  # Resolve session relationship
            owner_id=getattr(case_entity, 'owner_id', None)  # Case owner user ID
        )
    
    @staticmethod
    def summary_to_api(case_summary: CaseSummary) -> CaseAPI:
        """
        Convert CaseSummary to API Case model.
        
        Args:
            case_summary: The CaseSummary object from service layer
            
        Returns:
            CaseAPI: The API response model with proper formatting
        """
        return CaseAPI(
            case_id=case_summary.case_id,
            title=case_summary.title,
            description=None,  # CaseSummary doesn't have description field
            status=case_summary.status.value if hasattr(case_summary.status, 'value') else str(case_summary.status),
            priority=case_summary.priority.value if hasattr(case_summary.priority, 'value') else str(case_summary.priority),
            created_at=case_summary.created_at.isoformat() + 'Z' if hasattr(case_summary.created_at, 'isoformat') else str(case_summary.created_at),
            updated_at=case_summary.updated_at.isoformat() + 'Z' if hasattr(case_summary.updated_at, 'isoformat') else str(case_summary.updated_at),
            message_count=case_summary.message_count,
            session_id=None,  # CaseSummary doesn't have session relationship
            owner_id=getattr(case_summary, 'owner_id', None)  # Case owner user ID
        )
    
    @staticmethod
    def entities_to_api_list(case_entities: List[Union[CaseEntity, CaseSummary]]) -> List[CaseAPI]:
        """
        Convert list of Case entities or CaseSummary objects to API Case models.
        
        Args:
            case_entities: List of persistence layer Case entities or CaseSummary objects
            
        Returns:
            List[CaseAPI]: List of API response models
        """
        result = []
        for entity in case_entities:
            if isinstance(entity, CaseSummary):
                result.append(CaseConverter.summary_to_api(entity))
            else:
                result.append(CaseConverter.entity_to_api(entity))
        return result
    
    @staticmethod
    def resolve_session_id(case_entity: CaseEntity, preferred_session_id: Optional[str] = None) -> Optional[str]:
        """
        Resolve the appropriate session_id for API response.
        
        This method implements the session-case relationship resolution logic:
        1. Use current_session_id if available
        2. Use preferred_session_id if provided and in case's session_ids
        3. Return None for standalone cases
        
        Args:
            case_entity: The Case entity
            preferred_session_id: Optional preferred session ID
            
        Returns:
            Optional[str]: The resolved session ID for API response
        """
        # Priority 1: Current active session
        if case_entity.current_session_id:
            return case_entity.current_session_id
        
        # Priority 2: Preferred session if it's associated with the case
        if (preferred_session_id and 
            hasattr(case_entity, 'session_ids') and 
            preferred_session_id in case_entity.session_ids):
            return preferred_session_id
        
        # Priority 3: No specific session relationship
        return None