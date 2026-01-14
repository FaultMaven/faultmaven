"""Services package (legacy compatibility).

This package remains as a legacy compatibility surface after module extraction.

IMPORTANT: Keep this module import-light.
Python imports the package (`faultmaven.services`) before importing submodules
like `faultmaven.services.base`, so importing extracted modules here can create
circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.services.auth_session_service import (
        AuthSessionService as SessionService,
    )
    from faultmaven.modules.case.domain.services.case_data_ingestion_service import (
        CaseDataIngestionService as DataService,
    )
    from faultmaven.modules.case.domain.services.case_service import CaseService

    from .analytics import AnalyticsDashboardService, ConfidenceService
    from .base import BaseService

__all__ = [
    "BaseService",
    "CaseService",
    "SessionService",
    "DataService",
    "AnalyticsDashboardService",
    "ConfidenceService",
]


def __getattr__(name: str) -> Any:
    """Lazy attribute resolver for backward-compatible imports."""
    if name == "BaseService":
        from .base import BaseService as _BaseService

        return _BaseService

    if name == "AnalyticsDashboardService":
        from .analytics import AnalyticsDashboardService as _AnalyticsDashboardService

        return _AnalyticsDashboardService

    if name == "ConfidenceService":
        from .analytics import ConfidenceService as _ConfidenceService

        return _ConfidenceService

    if name == "CaseService":
        from faultmaven.modules.case.domain.services.case_service import (
            CaseService as _CaseService,
        )

        return _CaseService

    if name == "DataService":
        from faultmaven.modules.case.domain.services.case_data_ingestion_service import (
            CaseDataIngestionService as _DataService,
        )

        return _DataService

    if name == "SessionService":
        from faultmaven.modules.auth.domain.services.auth_session_service import (
            AuthSessionService as _SessionService,
        )

        return _SessionService

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
