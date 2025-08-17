"""API Routes Package
"""

# Import routes
from . import agent
from . import data
from . import knowledge
from . import session

# Import case persistence routes
try:
    from . import case
    CASE_ROUTES_AVAILABLE = True
except ImportError:
    CASE_ROUTES_AVAILABLE = False
    case = None

# Import Phase 2 enhanced routes
try:
    from . import enhanced_agent
    from . import orchestration
    ENHANCED_ROUTES_AVAILABLE = True
except ImportError:
    ENHANCED_ROUTES_AVAILABLE = False
    enhanced_agent = None
    orchestration = None

# Import monitoring and performance routes
try:
    from . import monitoring
    MONITORING_ROUTES_AVAILABLE = True
except ImportError:
    MONITORING_ROUTES_AVAILABLE = False
    monitoring = None

__all__ = [
    "agent", 
    "data",
    "knowledge",
    "session",
]

# Add case routes if available
if CASE_ROUTES_AVAILABLE:
    __all__.append("case")

# Add enhanced routes if available
if ENHANCED_ROUTES_AVAILABLE:
    __all__.extend(["enhanced_agent", "orchestration"])

# Add monitoring routes if available
if MONITORING_ROUTES_AVAILABLE:
    __all__.append("monitoring")