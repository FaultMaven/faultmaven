"""Models package - maintains backward compatibility during refactoring.

This module re-exports all models from the original models.py file
to ensure existing imports continue to work during the migration.
"""

# Import everything from the original models.py for backward compatibility
# Using explicit imports to avoid issues with star imports
from ..models_original import (
    AgentState,
    DataInsightsResponse,
    DataType,
    KnowledgeBaseDocument,
    SearchRequest,
    SearchResult,
    SessionContext,
    TroubleshootingResponse,
)

# Import new v3.1.0 API models
from .api import (
    ResponseType,
    SourceType,
    Source,
    PlanStep,
    UploadedData,
    ViewState,
    QueryRequest,
    AgentResponse,
    ErrorDetail,
    ErrorResponse,
)

# Import new interfaces (Phase 1.1 of refactoring)
# These are added for the refactoring but don't break backward compatibility
from .interfaces import (
    ToolResult,
    BaseTool,
    ILLMProvider,
    ITracer,
    ISanitizer,
    IVectorStore,
    ISessionStore,
    # Phase 3.2 additions
    IDataClassifier,
    ILogProcessor,
    IStorageBackend,
    # Phase 3.3 additions
    IKnowledgeIngester,
)

# Import case persistence models (optional)
try:
    from .case import (
        Case,
        CaseMessage,
        CaseParticipant,
        CaseContext,
        CaseStatus,
        CasePriority,
        MessageType,
        ParticipantRole,
        CaseCreateRequest,
        CaseUpdateRequest,
        CaseShareRequest,
        CaseListFilter,
        CaseSearchRequest,
        CaseSummary
    )
    from .interfaces_case import (
        ICaseStore,
        ICaseService,
        ICaseNotificationService,
        ICaseIntegrationService
    )
    CASE_MODELS_AVAILABLE = True
except ImportError:
    CASE_MODELS_AVAILABLE = False

# Utility functions for timestamp formatting
from datetime import datetime

def utc_timestamp() -> str:
    """Generate UTC timestamp with 'Z' suffix format required by API specification.
    
    Returns:
        str: UTC timestamp in ISO format with 'Z' suffix (e.g. "2024-01-15T14:30:00.123Z")
    """
    return datetime.utcnow().isoformat() + 'Z'

def parse_utc_timestamp(timestamp_str: str) -> datetime:
    """Parse UTC timestamp string into timezone-naive datetime object.
    
    Handles both 'Z' suffix format and regular ISO format consistently,
    returning timezone-naive datetime objects to avoid comparison issues.
    
    Args:
        timestamp_str: UTC timestamp string (with or without 'Z' suffix)
        
    Returns:
        datetime: Timezone-naive datetime object in UTC
    """
    if timestamp_str.endswith('Z'):
        # Remove 'Z' suffix and parse as naive datetime (already UTC)
        return datetime.fromisoformat(timestamp_str[:-1])
    else:
        # Parse regular ISO format
        return datetime.fromisoformat(timestamp_str)

# Re-export everything
__all__ = [
    # Original models (backward compatibility)
    "AgentState",
    "DataInsightsResponse", 
    "DataType",
    "KnowledgeBaseDocument",
    "SearchRequest",
    "SearchResult",
    "SessionContext",
    "TroubleshootingResponse",
    # New v3.1.0 API models
    "ResponseType",
    "SourceType", 
    "Source",
    "PlanStep",
    "UploadedData",
    "ViewState",
    "QueryRequest",
    "AgentResponse",
    "ErrorDetail",
    "ErrorResponse",
    # New interfaces (Phase 1.1)
    "ToolResult",
    "BaseTool",
    "ILLMProvider",
    "ITracer",
    "ISanitizer",
    "IVectorStore",
    "ISessionStore",
    # Phase 3.2 additions
    "IDataClassifier",
    "ILogProcessor", 
    "IStorageBackend",
    # Phase 3.3 additions
    "IKnowledgeIngester",
    # Utility functions
    "utc_timestamp",
    "parse_utc_timestamp",
]

# Add case models to exports if available
if CASE_MODELS_AVAILABLE:
    __all__.extend([
        # Case persistence models
        "Case",
        "CaseMessage",
        "CaseParticipant", 
        "CaseContext",
        "CaseStatus",
        "CasePriority",
        "MessageType",
        "ParticipantRole",
        "CaseCreateRequest",
        "CaseUpdateRequest",
        "CaseShareRequest",
        "CaseListFilter",
        "CaseSearchRequest",
        "CaseSummary",
        # Case interfaces
        "ICaseStore",
        "ICaseService",
        "ICaseNotificationService",
        "ICaseIntegrationService",
    ])

# As we migrate, we'll replace the above with:
# from .agent import *
# from .api import *
# from .domain import *
# from .session import *