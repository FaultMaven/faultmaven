"""API Routes Package
"""

# NOTE: Most routes have been moved to modules (faultmaven/modules/*/api/routes.py)
# or to faultmaven/api/routes/*
# This file now only handles optional/conditional route imports

# REMOVED: agent - replaced by case routes with real AgentService integration
# REMOVED: data - functionality moved to modules/case (data ingestion)
# REMOVED: knowledge - moved to modules/knowledge/api/routes.py
# REMOVED: session - moved to modules/auth (session management)
# REMOVED: auth - moved to faultmaven/api/routes/auth.py

# Import case persistence routes
try:
    from . import case
    CASE_ROUTES_AVAILABLE = True
except ImportError:
    CASE_ROUTES_AVAILABLE = False
    case = None

# Import organization and team routes
try:
    from . import organizations
    from . import teams
    ORG_TEAM_ROUTES_AVAILABLE = True
except ImportError:
    ORG_TEAM_ROUTES_AVAILABLE = False
    organizations = None
    teams = None

# Import reports routes (TASK-024)
try:
    from . import reports
    REPORTS_ROUTES_AVAILABLE = True
except ImportError:
    REPORTS_ROUTES_AVAILABLE = False
    reports = None

"""Locked spec excludes enhanced_agent, orchestration, monitoring routes."""

__all__ = []

# Add case routes if available
if CASE_ROUTES_AVAILABLE:
    __all__.append("case")

# Add organization and team routes if available
if ORG_TEAM_ROUTES_AVAILABLE:
    __all__.extend(["organizations", "teams"])

# Add reports routes if available (TASK-024)
if REPORTS_ROUTES_AVAILABLE:
    __all__.append("reports")

# Excluded: enhanced_agent, orchestration, monitoring