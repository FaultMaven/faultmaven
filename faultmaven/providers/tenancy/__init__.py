"""Tenancy provider module — deployment-neutral organization context.

Tenancy lives in the core, config-selected by ``TENANT_PROVIDER`` (ADR-010):
``single`` (the Standalone default, single-tenant) and ``multi`` (multi-tenant,
Cloud) are both in-core. ``create_tenant_provider`` selects between them.
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
