"""Knowledge API Routes - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.api.routes

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge.api import router
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.api.routes import router

__all__ = ["router"]
