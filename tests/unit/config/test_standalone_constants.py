"""The standalone identity constants, and what is deliberately NOT among them.

Under ADR-017 D8 a standalone deployment is one seeded ENTERPRISE and one seeded
team, and **no organization**: an organization is a billing target created by
payment (D5), and nobody is billed for a self-hosted install. So the
organization sentinel is gone from ``config.constants``, and its absence is the
assertion — a constant that came back would be a billing subject under every
standalone deployment that the rest of the campaign assumes is not there.
"""

import pytest

from faultmaven.config import constants
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider


@pytest.mark.unit
class TestStandaloneConstants:
    def test_the_enterprise_sentinel_is_pinned(self):
        """The value is load-bearing: RLS's global-write arm compares to it."""
        assert (
            constants.STANDALONE_ENTERPRISE_ID == "00000000-0000-0000-0000-000000000002"
        )
        assert constants.STANDALONE_ENTERPRISE_SLUG == "default"

    def test_the_team_sentinel_is_pinned(self):
        assert constants.STANDALONE_TEAM_ID == "00000000-0000-0000-0000-000000000003"

    def test_the_provider_reads_the_constants_rather_than_its_own_copy(self):
        assert (
            SingleTenantProvider.DEFAULT_ENTERPRISE_ID
            == constants.STANDALONE_ENTERPRISE_ID
        )
        assert (
            SingleTenantProvider.DEFAULT_ENTERPRISE_SLUG
            == constants.STANDALONE_ENTERPRISE_SLUG
        )
        assert SingleTenantProvider.DEFAULT_TEAM_ID == constants.STANDALONE_TEAM_ID

    @pytest.mark.security
    @pytest.mark.parametrize(
        "retired",
        ["STANDALONE_ORG_ID", "STANDALONE_ORG_SLUG", "STANDALONE_ORG_NAME"],
    )
    def test_the_organization_sentinel_is_gone(self, retired):
        """Deleted, not deprecated (the owner's rule for this campaign).

        The one place in the suite that names the retired constants, and it
        names them only to prove they are absent.
        """
        assert not hasattr(constants, retired)

    @pytest.mark.security
    def test_the_module_stays_dependency_free(self):
        """Importable from every layer, so it cannot create an import cycle."""
        source = (
            __import__("pathlib").Path(constants.__file__).read_text()  # noqa: PTH123
        )
        assert "import" not in source.split('"""')[2]
