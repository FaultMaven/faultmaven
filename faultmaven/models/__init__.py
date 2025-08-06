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
    QueryRequest,
    SearchRequest,
    SearchResult,
    SessionContext,
    TroubleshootingResponse,
    UploadedData,
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

# Re-export everything
__all__ = [
    # Original models (backward compatibility)
    "AgentState",
    "DataInsightsResponse", 
    "DataType",
    "KnowledgeBaseDocument",
    "QueryRequest",
    "SearchRequest",
    "SearchResult",
    "SessionContext",
    "TroubleshootingResponse",
    "UploadedData",
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
]

# As we migrate, we'll replace the above with:
# from .agent import *
# from .api import *
# from .domain import *
# from .session import *