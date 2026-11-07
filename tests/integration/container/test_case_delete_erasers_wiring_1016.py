"""A really-initialized container gives the case service every store's eraser
(#1016), and the draft and document deletes the case delete's trace eraser
(#2575).

``tests/unit/modules/case/test_case_deletion_1016.py`` drives
``create_case_service`` directly. This drives ``container.initialize()``, so it
also pins what ``register_services`` hands it: without the file storage service
or the Redis client the delete would bind a raising eraser in their place and
fail every delete.
"""

import pytest

from faultmaven.infrastructure.observability import tracing
from faultmaven.infrastructure.observability.trace_erasure import LLMTraceEraser
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
        assert service.stores_not_configured == ("llm_traces", "case_vectors")
    else:
        assert names == [
            "session_pointers",
            "runbook_conversion",
            "uploads",
            "case_vectors",
            "pii_redaction_registry",
        ]
        assert service.store_erasers[3] is fresh_container.case_vector_store
        assert service.stores_not_configured == ("llm_traces",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_configured_trace_backend_binds_the_trace_eraser_first(
    monkeypatch, skip_service_checks, fresh_container
):
    """#2102. ``initialize()`` does not run ``init_opik_tracing`` and no model is
    called here, so no SDK client is built."""
    monkeypatch.setenv("OPIK_ENABLED", "true")
    monkeypatch.setenv("OPIK_URL_OVERRIDE", "http://opik.test")
    monkeypatch.setenv("OPIK_PROJECT_NAME", "fm-project")
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    skip_service_checks(True)
    await fresh_container.initialize()

    service = fresh_container.get_service("case_service")
    eraser = service.store_erasers[0]
    assert isinstance(eraser, LLMTraceEraser)
    assert eraser.project_name == "fm-project"
    assert service.stores_not_configured == ("case_vectors",)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opik_enabled, sdk, kind",
    [
        (False, True, type(None)),
        (True, True, LLMTraceEraser),
        (True, False, UnavailableStoreEraser),
    ],
    ids=["not-configured", "configured", "sdk-absent"],
)
async def test_the_case_draft_and_document_deletes_bind_one_trace_eraser_kind(
    monkeypatch, skip_service_checks, fresh_container, opik_enabled, sdk, kind
):
    """#2575. One builder decides for the three: a draft or document delete
    never skips the traces a case delete would purge, nor purges where it
    would not."""
    monkeypatch.setenv("OPIK_ENABLED", str(opik_enabled).lower())
    monkeypatch.setenv("OPIK_URL_OVERRIDE", "http://opik.test")
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", sdk)
    skip_service_checks(True)
    await fresh_container.initialize()

    case = fresh_container.get_service("case_service")
    traces = [e for e in case.store_erasers if e.store_name == "llm_traces"]
    assert fresh_container.conversion_service is not None
    bound = {
        "case": traces[0] if traces else None,
        "conversion": fresh_container.conversion_service._trace_eraser,
        "knowledge": fresh_container.get_service("knowledge_service")._trace_eraser,
    }
    assert {name: type(e) for name, e in bound.items()} == dict.fromkeys(bound, kind)
    assert ("llm_traces" in case.stores_not_configured) is (kind is type(None))
