"""Standalone single-tenant identity constants + org-isolation-removal guards.

ADR-006: the Community Edition is single-tenant. The implicit org/enterprise
identity lives in one place (``faultmaven.config.constants``), and CE
repositories do NOT enforce tenant isolation with per-query
``WHERE organization_id`` filters — cloud isolation is PostgreSQL RLS in
faultmaven-cloud. These tests pin both facts so neither silently regresses.
"""

import inspect

import pytest

from faultmaven.config import constants


@pytest.mark.unit
class TestStandaloneConstants:
    def test_org_and_enterprise_ids_are_the_canonical_uuids(self):
        assert constants.STANDALONE_ORG_ID == "00000000-0000-0000-0000-000000000001"
        assert (
            constants.STANDALONE_ENTERPRISE_ID == "00000000-0000-0000-0000-000000000002"
        )

    def test_single_tenant_provider_sources_identity_from_constants(self):
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        assert SingleTenantProvider.DEFAULT_ORG_ID == constants.STANDALONE_ORG_ID
        assert SingleTenantProvider.DEFAULT_ORG_SLUG == constants.STANDALONE_ORG_SLUG
        assert SingleTenantProvider.DEFAULT_ORG_NAME == constants.STANDALONE_ORG_NAME
        assert (
            SingleTenantProvider.DEFAULT_ENTERPRISE_ID
            == constants.STANDALONE_ENTERPRISE_ID
        )

    def test_session_context_default_org_uses_the_constant(self):
        from faultmaven.models.common import SessionContext

        ctx = SessionContext(session_id="s", user_id="u")
        assert ctx.organization_id == constants.STANDALONE_ORG_ID

    def test_no_residual_magic_org_uuid_in_case_repositories(self):
        # The default-org literal must come from the constant, never be
        # re-hardcoded inline in repository SQL.
        from faultmaven.modules.case.infrastructure import (
            postgresql_hybrid_case_repository,
            sqlite_case_repository,
        )

        for module in (sqlite_case_repository, postgresql_hybrid_case_repository):
            src = inspect.getsource(module)
            assert "'00000000-0000-0000-0000-000000000001'" not in src, (
                f"{module.__name__} re-hardcodes the default org UUID; "
                "reference config.constants.STANDALONE_ORG_ID instead"
            )


@pytest.mark.unit
class TestOrgIsolationNotEnforcedInCE:
    """CE reads must not be scoped by a per-query org filter (ADR-006)."""

    def test_repositories_have_no_org_read_filter(self):
        from faultmaven.modules.case.infrastructure import (
            case_repository,
            postgresql_hybrid_case_repository,
            sqlite_case_repository,
        )

        # Match the read-filter form specifically — a where-clause append — so
        # the UPDATE ... SET organization_id = :organization_id (a write) does
        # not trip the guard.
        for module in (
            sqlite_case_repository,
            postgresql_hybrid_case_repository,
        ):
            src = inspect.getsource(module)
            assert (
                'where_clauses.append("organization_id = :organization_id")' not in src
            ), (
                f"{module.__name__} reintroduced a per-query org read filter; "
                "tenant isolation belongs in cloud RLS, not CE (ADR-006)"
            )
            assert (
                'where_clauses.append("c.organization_id = :organization_id")'
                not in src
            )

        # In-memory base repo: no list-comprehension / guard org filter either.
        base_src = inspect.getsource(case_repository)
        assert "c.organization_id == organization_id" not in base_src
        assert "case.organization_id != organization_id" not in base_src
