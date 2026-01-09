"""Models package - central exports for FaultMaven models.

This module provides convenient imports for commonly used models across the application.
Models are now primarily located in their respective modules under faultmaven/modules/.
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

# Import case models from the new module location
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    CaseSeverity,
    MessageType,
    UrgencyLevel,
)

# Import case API models from api_models.py
from .api_models import (
    CaseMessage,
    CaseParticipant,
    CaseCreateRequest,
    CaseUpdateRequest,
    CaseListFilter,
    CaseSearchRequest,
    CaseSummary,
)

# Import case interfaces
from .interfaces_case import (
    ICaseStore,
    ICaseService,
    ICaseNotificationService,
    ICaseIntegrationService
)

# Import report generation models from the new module location
from faultmaven.modules.report.domain.models import (
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

# Import agentic models from the new module location
from faultmaven.modules.agent.domain.models.agentic import SuggestedAction

# Import session model from auth module
from faultmaven.modules.auth.domain.models.session import Session

# Re-export everything
__all__ = [
    # Common models
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
    # v3.1.0 API models
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
    # Interfaces
    "ToolResult",
    "BaseTool",
    "ILLMProvider",
    "ITracer",
    "ISanitizer",
    "IVectorStore",
    "ISessionStore",
    "IDataClassifier",
    "ILogProcessor",
    "IStorageBackend",
    "IKnowledgeIngester",
    # Utility functions
    "utc_timestamp",
    "parse_utc_timestamp",
    # Case models (from modules/case)
    "Case",
    "CaseStatus",
    "CaseSeverity",
    "MessageType",
    "UrgencyLevel",
    # Case API models (from api_models.py)
    "CaseMessage",
    "CaseParticipant",
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
    # Report models (from modules/report)
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
    # Agentic models
    "SuggestedAction",
    # Auth models
    "Session",
]