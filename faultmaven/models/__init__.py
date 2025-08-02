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

# Re-export everything
__all__ = [
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
]

# As we migrate, we'll replace the above with:
# from .agent import *
# from .api import *
# from .domain import *
# from .session import *