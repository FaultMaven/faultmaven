"""Knowledge API Layer.

This package contains FastAPI routes for knowledge management endpoints:
- router: FastAPI router for /knowledge/* endpoints
"""

from faultmaven.modules.knowledge.api.routes import router

__all__ = ["router"]
