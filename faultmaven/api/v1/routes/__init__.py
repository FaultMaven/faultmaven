"""API Routes Package
"""

# Import routes  
# REMOVED: agent - replaced by case routes with real AgentService integration
from . import data
from . import knowledge
from . import session
from . import auth

# Import case persistence routes
try:
    from . import case
    CASE_ROUTES_AVAILABLE = True
except ImportError:
    CASE_ROUTES_AVAILABLE = False
    case = None

"""Locked spec excludes enhanced_agent, orchestration, monitoring routes."""

__all__ = [
    # "agent",  # REMOVED: replaced by case routes with real AgentService integration
    "data",
    "knowledge", 
    "session",
    "auth",
]

# Add case routes if available
if CASE_ROUTES_AVAILABLE:
    __all__.append("case")

# Excluded: enhanced_agent, orchestration, monitoring