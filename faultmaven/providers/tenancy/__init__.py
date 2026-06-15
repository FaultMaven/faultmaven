"""Tenancy Provider Module for Deployment Neutrality (TASK-023).

This module provides the TenantProvider abstraction for deployment neutrality.
The Community Edition ships only ``SingleTenantProvider``; multi-tenant
implementations are provided by faultmaven-cloud and discovered at runtime via
the ``faultmaven.providers.tenancy`` entry-point seam (ADR-006).
"""

from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.factory import create_tenant_provider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

__all__ = [
    "TenantProvider",
    "SingleTenantProvider",
    "create_tenant_provider",
]
