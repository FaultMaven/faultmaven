"""Tenancy Provider Module for Deployment Neutrality (TASK-023).

This module provides TenantProvider abstraction to enable deployment-neutral
case and organization management that works in both:
1. Local deployment (single tenant, development, community edition)
2. Cloud deployment (multi-tenant, production, enterprise edition)
"""

from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.factory import create_tenant_provider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

__all__ = [
    "TenantProvider",
    "SingleTenantProvider",
    "MultiTenantProvider",
    "create_tenant_provider",
]
