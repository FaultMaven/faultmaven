"""Analytics Services Package

Provides ML-based analytics, confidence scoring, and dashboard services
for monitoring and optimizing the FaultMaven troubleshooting system.
"""

from .dashboard_service import AnalyticsDashboardService
from .confidence_service import GlobalConfidenceService as ConfidenceService

__all__ = ["AnalyticsDashboardService", "ConfidenceService"]