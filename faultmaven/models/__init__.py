"""Models package - central exports for FaultMaven models.

This module provides convenient imports for commonly used models across the application.
"""

# Import common models used throughout the application
from .common import (
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
        CaseDiagnosticState,
        CaseStatus,
        CasePriority,
        MessageType,
        CaseCreateRequest,
        CaseUpdateRequest,
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

# Import report generation models (FR-CM-006)
try:
    from .report import (
        ReportType,
        ReportStatus,
        RunbookSource,
        RunbookMetadata,
        CaseReport,
        SimilarRunbook,
        RunbookRecommendation,
        ReportRecommendation,
        ReportGenerationRequest,
        ReportGenerationResponse,
        CaseClosureRequest,
        CaseClosureResponse
    )
    REPORT_MODELS_AVAILABLE = True
except ImportError:
    REPORT_MODELS_AVAILABLE = False

# Import agentic models (active OODA framework)
from .agentic import SuggestedAction
from .case import UrgencyLevel

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
        "CaseDiagnosticState",
        "CaseStatus",
        "CasePriority",
        "MessageType",
        "CaseCreateRequest",
        "CaseUpdateRequest",
        "CaseListFilter",
        "CaseSearchRequest",
        "CaseSummary",
        # Case interfaces
        "ICaseStore",
        "ICaseService",
        "ICaseNotificationService",
        "ICaseIntegrationService",
    ])

# Add agentic models to exports (always available)
__all__.extend([
    "SuggestedAction",
    "UrgencyLevel",
])

# Add report models to exports if available (FR-CM-006)
if REPORT_MODELS_AVAILABLE:
    __all__.extend([
        "ReportType",
        "ReportStatus",
        "RunbookSource",
        "RunbookMetadata",
        "CaseReport",
        "SimilarRunbook",
        "RunbookRecommendation",
        "ReportRecommendation",
        "ReportGenerationRequest",
        "ReportGenerationResponse",
        "CaseClosureRequest",
        "CaseClosureResponse",
    ])

# As we migrate, we'll replace the above with:
# from .agent import *
# from .api import *
# from .domain import *
# from .session import *