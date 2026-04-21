"""Regression guard for the file_storage_service DI wiring.

In 2026-04 the storage consolidation refactor constructed
``FileStorageService`` at the composition root but failed to publish it to
the container. Consumers downstream — ``infrastructure.py`` via
``getattr(container, "file_storage_service", None)`` and ``tools.py`` via
``container.get_service("storage_service")`` — silently received ``None``.
``LocalTier2Service.analyze`` then crashed with ``AttributeError`` mid
investigation because it dereferenced the null storage service.

The systemic fix requires two invariants that this test enforces:

1. ``services.py`` publishes the service via ``_register_service`` under the
   canonical name ``"file_storage_service"``.
2. ``tools.py`` retrieves it via ``get_service`` using the **same** name.
   (The old stale key ``"storage_service"`` must not reappear — name drift
   between registration and consumer is what produced the original bug.)

The test is deliberately source-level rather than exercising the real
``register_services`` function — it directly asserts the invariant we care
about (canonical name consistency) without pulling in FakeRedis, database
bootstrap, and the rest of the composition root. A future contributor who
renames the registration or the lookup will see this fail immediately.
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROVIDERS_DIR = _REPO_ROOT / "faultmaven" / "container" / "providers"
_SERVICES_PROVIDER = _PROVIDERS_DIR / "services.py"
_TOOLS_PROVIDER = _PROVIDERS_DIR / "tools.py"


@pytest.mark.unit
class TestFileStorageServiceWiring:
    def test_services_provider_registers_under_canonical_name(self):
        source = _SERVICES_PROVIDER.read_text()
        assert '_register_service("file_storage_service"' in source, (
            "services.py must publish FileStorageService via "
            '_register_service("file_storage_service", ...) — see the 2026-04 '
            "silent-wiring incident where the service was constructed but "
            "never published."
        )

    def test_tools_provider_looks_up_under_canonical_name(self):
        source = _TOOLS_PROVIDER.read_text()
        assert 'get_service("file_storage_service")' in source, (
            "tools.py must retrieve storage via "
            'get_service("file_storage_service") to match services.py '
            "registration."
        )

    def test_tools_provider_does_not_use_stale_storage_service_key(self):
        """Guard against re-introducing the 'storage_service' key that was
        the cause of the original silent breakage.
        """
        source = _TOOLS_PROVIDER.read_text()
        assert 'get_service("storage_service")' not in source, (
            "tools.py still uses the old 'storage_service' registry key. "
            "That name was never registered by services.py; consumers using "
            "it silently received None. Rename to 'file_storage_service'."
        )
