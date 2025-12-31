"""Bootstrap Module for Application Startup.

Handles application initialization tasks including:
- Default organization creation (single-tenant mode)
- Database schema verification
- Infrastructure provider initialization

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from faultmaven.bootstrap.startup import bootstrap_application

__all__ = ["bootstrap_application"]
