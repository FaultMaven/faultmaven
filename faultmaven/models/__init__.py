"""Models package - maintains backward compatibility during refactoring.

This module re-exports all models from the original models.py file
to ensure existing imports continue to work during the migration.
"""

# Import everything from the legacy models for backward compatibility
# Using explicit imports to avoid issues with star imports
from .legacy import (
    AgentState as AgentStateDict,
    AgentStateEnum,
    DataInsightsResponse,
    SearchRequest,
    SearchResult,
    SessionContext,
    TroubleshootingResponse,
    utc_timestamp,
    parse_utc_timestamp,
)

# Import DataType from api.py where it's currently defined
from .api import DataType, KnowledgeBaseDocument

# For backward compatibility, provide AgentState as the enum (most common usage)
# and make the TypedDict available as AgentStateDict
AgentState = AgentStateEnum
AgentStateDict = AgentStateDict  # Keep this available for core agent tests

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
    TitleGenerateRequest,
    TitleResponse,
    # Authentication models
    User,
    DevLoginRequest,
    AuthResponse,
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

# Utility functions are now imported from legacy.py

# Re-export everything
__all__ = [
    # Original models (backward compatibility)
    "AgentState",
    "AgentStateEnum",
    "AgentStateDict",
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
    "TitleGenerateRequest",
    "TitleResponse",
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