"""Models package - central exports for FaultMaven models.

This module provides convenient imports for commonly used models across the application.
Models are now primarily located in their respective modules under faultmaven/modules/.
"""

# Import agentic models from the new module location
from faultmaven.modules.agent.domain.models.agentic import SuggestedAction

# Import session model from auth module
from faultmaven.modules.auth.domain.models.session import Session

# Import case models from the new module location
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseStatus,
    MessageType,
    UrgencyLevel,
)

# Import report generation models from the new module location
from faultmaven.modules.report.domain.models import (
    CaseClosureRequest,
    CaseClosureResponse,
    CaseReport,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportRecommendation,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookRecommendation,
    RunbookSource,
    SimilarRunbook,
)

# Import new v3.1.0 API models
# Import DataType from api.py where it's currently defined
from .api import (  # Authentication models
    AgentResponse,
    AuthResponse,
    DataType,
    DevLoginRequest,
    ErrorDetail,
    ErrorResponse,
    KnowledgeBaseDocument,
    PlanStep,
    QueryRequest,
    ResponseType,
    Source,
    SourceType,
    TitleGenerateRequest,
    TitleResponse,
    UploadedData,
    User,
    ViewState,
)

# Import case API models from api_models.py
from .api_models import (
    CaseCreateRequest,
    CaseListFilter,
    CaseMessage,
    CaseParticipant,
    CaseSearchRequest,
    CaseSummary,
    CaseUpdateRequest,
)

# Import common models
from .common import AgentState as AgentStateDict
from .common import (
    AgentStateEnum,
    DataInsightsResponse,
    SearchRequest,
    SearchResult,
    SessionContext,
    TroubleshootingResponse,
)

# Import new interfaces (Phase 1.1 of refactoring)
from .interfaces import (  # Phase 3.2 additions; Phase 3.3 additions
    BaseTool,
    IDataClassifier,
    IKnowledgeIngester,
    ILLMProvider,
    ILogProcessor,
    ISanitizer,
    ISessionStore,
    IStorageBackend,
    ITracer,
    IVectorStore,
    ToolResult,
)

# Import case interfaces
from .interfaces_case import (
    ICaseIntegrationService,
    ICaseNotificationService,
    ICaseService,
    ICaseStore,
)

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
