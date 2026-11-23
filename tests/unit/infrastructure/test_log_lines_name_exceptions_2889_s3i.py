"""The remaining infrastructure, services and utils log lines name an exception
by its class, never its text (#2889 S3i).

Principles §7, "What a log line may hold", rule 1. Stage S3 of #2886. Each case
drives one site with :data:`MARKER` in the exception's message, captures every
record at DEBUG and renders each through the real chain's formatter
(``build_log_formatter``). The marker is on no rendered line, and the class
name is on the site's line. How each site is driven is stated on its case.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.caching.intelligent_cache import IntelligentCache
from faultmaven.infrastructure.jobs.job_service import JobService
from faultmaven.infrastructure.tasks import case_cleanup
from faultmaven.services.base import BaseService
from faultmaven.utils import token_estimation
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _broken(*_args, **_kwargs):
    raise RuntimeError(f"broken {MARKER}")


async def test_a_cache_read_failure_is_named_by_class(lines, monkeypatch):
    """``IntelligentCache.get`` with its L1 read raising: a miss, logged."""
    cache = IntelligentCache(enable_analytics=False)
    monkeypatch.setattr(cache, "_get_from_l1", _broken)
    assert await cache.get("key") == (None, False)
    _named(lines(), "Cache get error for key", ": RuntimeError")


async def test_a_job_store_failure_is_named_by_class(lines):
    """``JobService.get_job`` with the store's read raising."""
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError(f"redis://u:{MARKER}@h"))
    with pytest.raises(Exception):
        await JobService(redis_client=redis).get_job("job-1")
    _named(lines(), "Failed to retrieve job job-1", ": ConnectionError")


async def test_a_case_cleanup_read_failure_is_named_by_class(lines):
    """``cleanup_orphaned_collections_task`` with the case-id read raising: the
    sweep stops before it deletes anything."""
    repository = MagicMock()
    repository.list_all_case_ids = AsyncMock(side_effect=_broken)
    store = MagicMock()
    store.list_case_collection_ids = AsyncMock(return_value=[])
    store.cleanup_orphaned_collections = AsyncMock(return_value=0)
    await case_cleanup.cleanup_orphaned_collections_task(store, repository)
    _named(lines(), "Failed to get case IDs", ": RuntimeError")
    store.list_case_collection_ids.assert_not_called()
    store.cleanup_orphaned_collections.assert_not_called()


class _Service(BaseService):
    pass


async def test_a_service_boundary_failure_is_named_by_class(lines, monkeypatch):
    """``execute_operation`` with an operation raising a third party's error:
    the outbound boundary's ``data["error"]`` is the class. The line renders
    the data's type, size and keys (``UnifiedLogger.log_boundary``), so the
    data handed to it is read too."""
    service = _Service("s3i")
    handed = []
    original = service.logger.log_boundary

    def spy(*args, **kwargs):
        handed.append(kwargs.get("data") or {})
        return original(*args, **kwargs)

    monkeypatch.setattr(service.logger, "log_boundary", spy)
    with pytest.raises(Exception):
        await service.execute_operation("op", _broken)
    assert [d["error"] for d in handed if "error" in d] == ["RuntimeError"]
    _named(lines(), "Service boundary outbound: op")


def test_an_encoder_failure_is_named_by_class(lines, monkeypatch):
    """``_get_tiktoken_encoder`` with tiktoken raising (the cache cleared, so
    the load runs)."""
    if not token_estimation.TIKTOKEN_AVAILABLE:
        pytest.skip("tiktoken is not installed")
    token_estimation._get_tiktoken_encoder.cache_clear()
    monkeypatch.setattr(token_estimation.tiktoken, "get_encoding", _broken)
    try:
        assert token_estimation._get_tiktoken_encoder("gpt-s3i") is None
    finally:
        token_estimation._get_tiktoken_encoder.cache_clear()
    _named(lines(), "Failed to get tiktoken encoder", ": RuntimeError")


class _Client(BaseExternalClient):
    def __init__(self):
        super().__init__("s3i_client", "S3iService", enable_circuit_breaker=False)


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
async def test_an_error_code_off_the_pattern_is_not_a_field(lines, sync):
    """``call_external`` fail-fast on a first-party error whose ``error_code`` is
    not an upper-case identifier and carries the marker: ``_provider_codes``
    keeps the status code and drops the code."""
    error = LLMException("rejected", status_code=400, error_code=f"bad code {MARKER}")
    error.retryable = False
    with pytest.raises(LLMException):
        if sync:
            _Client().call_external_sync("op", MagicMock(side_effect=error))
        else:
            await _Client().call_external("op", AsyncMock(side_effect=error))
    fail_fast = _named(lines(), "Non-retryable error for S3iService.op")
    assert fail_fast.get("status_code") == 400
    assert "error_code" not in fail_fast
