"""The lifespan's KB step runs the pack bootstrap only where it can succeed (#2171).

Branch order: multi-tenancy skip (#770), no KB vector store skip
(``SKIP_SERVICE_CHECKS=true``: every runbook would be written, indexed into
nothing and failed), run, no knowledge service. The store is set on the
container, where composition leaves it, not on a mock service.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.kb_init import BootstrapResult
from faultmaven.bootstrap.lifespan import _bootstrap_kb_step
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

pytestmark = [pytest.mark.unit]

_LOGGER = "faultmaven.bootstrap.lifespan"
_A_STORE = object()


async def _run_step(caplog, *, tenant_provider, store, service, result=None):
    """Return the ``bootstrap_kb`` double and the step's warnings."""
    app = FastAPI()
    if service is not None:
        app.state.knowledge_service = service
    bootstrap_kb = AsyncMock(return_value=result or BootstrapResult())
    with (
        patch(
            "faultmaven.container.container",
            SimpleNamespace(knowledge_vector_store=store),
        ),
        patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value=tenant_provider,
        ),
        patch("faultmaven.bootstrap.kb_init.bootstrap_kb", new=bootstrap_kb),
        caplog.at_level(logging.INFO, logger=_LOGGER),
    ):
        await _bootstrap_kb_step(app)
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno >= logging.WARNING
    ]
    return bootstrap_kb, warnings


async def test_no_kb_vector_store_skips_the_bootstrap_with_one_warning(caplog):
    bootstrap_kb, warnings = await _run_step(
        caplog, tenant_provider=BUILTIN_SINGLE, store=None, service=object()
    )

    bootstrap_kb.assert_not_awaited()
    assert len(warnings) == 1
    assert "SKIP_SERVICE_CHECKS=true" in warnings[0]


async def test_a_kb_vector_store_runs_the_bootstrap_and_reports_failures(caplog):
    service, result = object(), BootstrapResult()
    result.failed.append(("global/example.md", "RuntimeError: boom"))
    bootstrap_kb, warnings = await _run_step(
        caplog,
        tenant_provider=BUILTIN_SINGLE,
        store=_A_STORE,
        service=service,
        result=result,
    )

    bootstrap_kb.assert_awaited_once()
    kwargs = bootstrap_kb.await_args.kwargs
    assert kwargs["knowledge_service"] is service
    assert kwargs["enterprise_id"] == STANDALONE_ENTERPRISE_ID
    assert warnings == [
        "  KB bootstrap failed for global/example.md: RuntimeError: boom"
    ]


async def test_multi_tenancy_skips_before_the_store_is_read(caplog):
    bootstrap_kb, warnings = await _run_step(
        caplog, tenant_provider=BUILTIN_MULTI, store=None, service=object()
    )

    bootstrap_kb.assert_not_awaited()
    assert warnings == []
    assert "skipped under multi-tenancy" in caplog.text


async def test_no_knowledge_service_skips_the_bootstrap(caplog):
    bootstrap_kb, warnings = await _run_step(
        caplog, tenant_provider=BUILTIN_SINGLE, store=_A_STORE, service=None
    )

    bootstrap_kb.assert_not_awaited()
    assert warnings == ["KB bootstrap skipped: knowledge_service not available"]
