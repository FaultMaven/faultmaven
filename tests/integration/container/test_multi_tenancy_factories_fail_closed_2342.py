"""Under multi, a sharing repository that cannot be built refuses the start (#2342).

Drives the real composition. ``SessionlessShareRepository`` failing to
construct (an image missing its module, the #885 shape) used to compose a
multi-tenant container with ``share_repository=None``: every team-shared case
and runbook dropped out of reads, and nothing reported it. Now the factory
raises ``RequiredServiceConstructionError``, which ``DIContainer.initialize``
re-raises ahead of its lenient path (#2103).
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.container.errors import RequiredServiceConstructionError

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.asyncio]


async def test_a_lenient_multi_tenant_container_refuses_a_share_repository_it_cannot_build(
    fresh_container, monkeypatch
):
    tenancy = MagicMock()
    tenancy.providers.tenant_provider = "multi"
    # The multi provider refuses outside cloud before any of this is reached.
    tenancy.is_cloud = True
    monkeypatch.setattr(
        "faultmaven.providers.tenancy.factory.get_settings", lambda: tenancy
    )
    error = ImportError("marker")

    def _raise(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.sessionless_share_repository."
        "SessionlessShareRepository.__init__",
        _raise,
    )

    with pytest.raises(RequiredServiceConstructionError) as refused:
        await fresh_container.initialize(allow_degraded=True)

    assert type(refused.value) is RequiredServiceConstructionError
    assert refused.value.__cause__ is error
    assert "marker" not in str(refused.value)
    assert not fresh_container.is_initialized
