"""A really-initialized container gives the case service every store's eraser
(#1016).

``tests/unit/modules/case/test_case_deletion_1016.py`` drives
``create_case_service`` directly. This drives ``container.initialize()``, so it
also pins what ``register_services`` hands it: without the file storage service
or the Redis client the delete would bind a raising eraser in their place and
fail every delete.
"""

import pytest

from faultmaven.modules.case.domain.services.case_erasure import (
    UnavailableStoreEraser,
)
from faultmaven.modules.case.domain.services.case_service import CaseService


@pytest.fixture
def skip_service_checks(monkeypatch):
    """Compose under ``SKIP_SERVICE_CHECKS`` on or off, whatever the ambient
    value; the settings singleton is rebuilt from the patched environment and
    again afterwards."""
    from faultmaven.config.settings import reset_settings

    def _set(value: bool) -> None:
        monkeypatch.setenv("SKIP_SERVICE_CHECKS", str(value).lower())
        reset_settings()

    yield _set
    monkeypatch.undo()
    reset_settings()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("skip", [True, False], ids=["test-profile", "production"])
async def test_the_case_service_is_composed_with_every_stores_eraser(
    skip, skip_service_checks, fresh_container
):
    skip_service_checks(skip)
    await fresh_container.initialize()

    service = fresh_container.get_service("case_service")
    assert isinstance(service, CaseService)
    names = [eraser.store_name for eraser in service.store_erasers]
    # A store ``register_services`` did not pass is bound as a raising eraser.
    assert [
        eraser.store_name
        for eraser in service.store_erasers
        if isinstance(eraser, UnavailableStoreEraser)
    ] == []
    assert service.store_erasers[2] is fresh_container.get_service(
        "file_storage_service"
    )
    if skip:
        # The test profile builds no case vector store, so nothing writes one.
        assert names == [
            "session_pointers",
            "runbook_conversion",
            "uploads",
            "pii_redaction_registry",
        ]
        assert service.stores_not_configured == ("case_vectors",)
    else:
        assert names == [
            "session_pointers",
            "runbook_conversion",
            "uploads",
            "case_vectors",
            "pii_redaction_registry",
        ]
        assert service.store_erasers[3] is fresh_container.case_vector_store
        assert service.stores_not_configured == ()
