"""Regression guard for the file_storage_service DI wiring.

In 2026-04 the storage consolidation refactor constructed
``FileStorageService`` at the composition root but failed to publish it to
the container. Consumers downstream — ``infrastructure.py`` (Tier 2
deep-analysis service) via ``getattr(container, "file_storage_service",
None)`` and ``tools.py`` via ``container.get_service("storage_service")`` —
silently received ``None``. ``LocalTier2Service.analyze`` then crashed
with ``AttributeError`` mid investigation because it dereferenced the
null storage service.

The systemic fix requires three invariants that this test enforces:

1. ``infrastructure.py`` constructs and publishes the service via
   ``_register_service`` under the canonical name ``"file_storage_service"``.
   It must live in the infrastructure layer because the init order is
   ``register_infrastructure → register_tools → register_services``, and
   the Tier 2 service (created in ``register_infrastructure``) reads
   ``container.file_storage_service`` via ``getattr``. Registering later
   in ``register_services`` would arrive too late — Tier 2 has already
   been constructed with ``None``.
2. ``tools.py`` retrieves via ``get_service`` using the same name.
   The old stale key ``"storage_service"`` must not reappear — name drift
   between registration and consumer is what produced the original bug.
3. ``services.py`` must not construct a second ``FileStorageService``
   instance. Centralizing construction in the infrastructure layer gives
   a single source of truth and prevents two-instance footguns (e.g. one
   with a different storage_root than the other).

The test is deliberately source-level rather than exercising the real
``register_infrastructure`` function — it directly asserts the invariants
we care about (canonical name consistency, init ordering, single
construction site) without pulling in FakeRedis, database bootstrap, and
the rest of the composition root. A future contributor who renames the
registration, duplicates construction, or re-introduces the stale key
will see this fail immediately.
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROVIDERS_DIR = _REPO_ROOT / "faultmaven" / "container" / "providers"
_INFRASTRUCTURE_PROVIDER = _PROVIDERS_DIR / "infrastructure.py"
_TOOLS_PROVIDER = _PROVIDERS_DIR / "tools.py"
_SERVICES_PROVIDER = _PROVIDERS_DIR / "services.py"


@pytest.mark.unit
class TestFileStorageServiceWiring:
    def test_infrastructure_provider_registers_under_canonical_name(self):
        source = _INFRASTRUCTURE_PROVIDER.read_text()
        assert '_register_service("file_storage_service"' in source, (
            "infrastructure.py must publish FileStorageService via "
            '_register_service("file_storage_service", ...) — see the 2026-04 '
            "silent-wiring incident. Registration must happen in "
            "register_infrastructure (not register_services), because the "
            "Tier 2 deep-analysis service is built in register_infrastructure "
            "and would otherwise receive None."
        )

    def test_infrastructure_provider_constructs_before_tier2_consumer(self):
        """The FileStorageService() construction must appear lexically
        before the create_tier2_service() call in infrastructure.py. If the
        order reverses (even on the same function), Tier 2 again gets None.
        """
        source = _INFRASTRUCTURE_PROVIDER.read_text()
        construct_idx = source.find("FileStorageService(")
        tier2_idx = source.find("create_tier2_service(")
        assert construct_idx > 0, (
            "FileStorageService is not constructed in infrastructure.py at "
            "all. The Tier 2 consumer below will receive None."
        )
        assert tier2_idx > 0, "create_tier2_service call not found"
        assert construct_idx < tier2_idx, (
            "FileStorageService must be constructed BEFORE "
            "create_tier2_service in infrastructure.py. Otherwise Tier 2 is "
            "built with storage_service=None and deep_analysis crashes "
            "mid-turn."
        )

    def test_tools_provider_looks_up_under_canonical_name(self):
        source = _TOOLS_PROVIDER.read_text()
        assert 'get_service("file_storage_service")' in source, (
            "tools.py must retrieve storage via "
            'get_service("file_storage_service") to match the registration '
            "in infrastructure.py."
        )

    def test_tools_provider_does_not_use_stale_storage_service_key(self):
        """Guard against re-introducing the 'storage_service' key that was
        the cause of the original silent breakage.
        """
        source = _TOOLS_PROVIDER.read_text()
        assert 'get_service("storage_service")' not in source, (
            "tools.py still uses the old 'storage_service' registry key. "
            "That name was never registered; consumers using it silently "
            "received None. Rename to 'file_storage_service'."
        )

    def test_services_provider_does_not_duplicate_construction(self):
        """services.py must retrieve from the container rather than
        constructing a second FileStorageService. Duplicate construction
        risks two instances with different config and obscures the single
        source of truth.
        """
        source = _SERVICES_PROVIDER.read_text()
        assert "FileStorageService(" not in source, (
            "services.py constructs a duplicate FileStorageService — "
            "the canonical construction lives in infrastructure.py. "
            "Retrieve via container.get_service('file_storage_service') "
            "or container.file_storage_service instead."
        )
