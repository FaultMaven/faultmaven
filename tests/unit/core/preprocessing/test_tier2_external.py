"""Tests for ExternalTier2Client (HTTP-based external service)."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.preprocessing.models import (
    AnalysisContext,
    DeepAnalysisResult,
    UnifiedDataType,
)
from faultmaven.core.preprocessing.tier2.external import ExternalTier2Client
from faultmaven.core.preprocessing.tier2.interface import ITier2SearchService


@pytest.fixture
def context():
    return AnalysisContext(case_id="case_abc", case_summary="Server down")


@pytest.fixture
def mock_storage():
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(return_value=b"raw file content")
    return storage


def _make_httpx_mock(post_responses=None, get_response=None):
    """Create a mock httpx module with AsyncClient."""
    mock_client = AsyncMock()

    if post_responses:
        mock_client.post = AsyncMock(side_effect=post_responses)
    if get_response:
        mock_client.get = AsyncMock(return_value=get_response)

    mock_httpx = MagicMock()
    mock_httpx.AsyncClient.return_value = mock_client
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return mock_httpx, mock_client


class TestExternalTier2Init:
    def test_implements_interface(self):
        assert issubclass(ExternalTier2Client, ITier2SearchService)

    def test_base_url_stripped(self):
        client = ExternalTier2Client(base_url="http://example.com/")
        assert client.base_url == "http://example.com"

    def test_defaults(self):
        client = ExternalTier2Client(base_url="http://example.com")
        assert client.api_key is None
        assert client.storage_service is None
        assert client.timeout_seconds == 30
        assert client._staged_files == {}


class TestExternalTier2Analyze:
    @pytest.mark.asyncio
    async def test_successful_analysis(self, context, mock_storage):
        client = ExternalTier2Client(
            base_url="http://tier2.example.com",
            api_key="test-key",
            storage_service=mock_storage,
        )

        # Stage response
        mock_stage_resp = MagicMock()
        mock_stage_resp.json.return_value = {"file_id": "staged_123"}
        mock_stage_resp.raise_for_status = MagicMock()

        # Analyze response
        mock_analyze_resp = MagicMock()
        mock_analyze_resp.json.return_value = {
            "answer": "Found memory leak",
            "excerpts": [],
            "confidence": 0.8,
            "tokens_used": 200,
        }
        mock_analyze_resp.raise_for_status = MagicMock()

        mock_httpx, _ = _make_httpx_mock(
            post_responses=[mock_stage_resp, mock_analyze_resp]
        )

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.analyze(
                file_ref="ref_1",
                query="check memory",
                context=context,
                data_type=UnifiedDataType.LOGS,
            )

        assert result.backend_used == "external"
        assert result.processing_time_ms >= 0

    @pytest.mark.asyncio
    async def test_network_error_handled(self, context, mock_storage):
        client = ExternalTier2Client(
            base_url="http://tier2.example.com",
            storage_service=mock_storage,
        )

        mock_httpx, mock_client = _make_httpx_mock()
        # The staging call will fail
        mock_client.post = AsyncMock(side_effect=ConnectionError("Network down"))

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.analyze(
                file_ref="ref_1",
                query="check",
                context=context,
                data_type=UnifiedDataType.LOGS,
            )

        assert "failed" in result.answer.lower() or "error" in result.answer.lower()
        assert result.backend_used == "external"

    @pytest.mark.asyncio
    async def test_no_storage_passes_ref_directly(self, context):
        client = ExternalTier2Client(base_url="http://tier2.example.com")

        mock_analyze_resp = MagicMock()
        mock_analyze_resp.json.return_value = {
            "answer": "Analysis result",
            "confidence": 0.5,
        }
        mock_analyze_resp.raise_for_status = MagicMock()

        mock_httpx, _ = _make_httpx_mock(post_responses=[mock_analyze_resp])

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.analyze(
                file_ref="ref_1",
                query="check",
                context=context,
                data_type=UnifiedDataType.LOGS,
            )

        assert result.backend_used == "external"


class TestExternalTier2IsAvailable:
    @pytest.mark.asyncio
    async def test_available_on_200(self):
        client = ExternalTier2Client(base_url="http://tier2.example.com")

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_httpx, _ = _make_httpx_mock(get_response=mock_response)

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.is_available()

        assert result is True

    @pytest.mark.asyncio
    async def test_not_available_on_error(self):
        client = ExternalTier2Client(base_url="http://tier2.example.com")

        mock_httpx, mock_client = _make_httpx_mock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("down"))

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.is_available()

        assert result is False

    @pytest.mark.asyncio
    async def test_not_available_on_non_200(self):
        client = ExternalTier2Client(base_url="http://tier2.example.com")

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_httpx, _ = _make_httpx_mock(get_response=mock_response)

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client.is_available()

        assert result is False


class TestEnsureStaged:
    @pytest.mark.asyncio
    async def test_cached_file_returned(self, mock_storage):
        client = ExternalTier2Client(
            base_url="http://tier2.example.com",
            storage_service=mock_storage,
        )
        client._staged_files["ref_1"] = "staged_abc"

        result = await client._ensure_staged("ref_1")
        assert result == "staged_abc"
        mock_storage.retrieve_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_storage_returns_file_ref(self):
        client = ExternalTier2Client(base_url="http://tier2.example.com")
        result = await client._ensure_staged("ref_1")
        assert result == "ref_1"

    @pytest.mark.asyncio
    async def test_staging_failure_returns_file_ref(self, mock_storage):
        mock_storage.retrieve_file.side_effect = RuntimeError("Storage error")
        client = ExternalTier2Client(
            base_url="http://tier2.example.com",
            storage_service=mock_storage,
        )
        result = await client._ensure_staged("ref_1")
        assert result == "ref_1"

    @pytest.mark.asyncio
    async def test_successful_staging_caches_result(self, mock_storage):
        client = ExternalTier2Client(
            base_url="http://tier2.example.com",
            storage_service=mock_storage,
        )

        mock_stage_resp = MagicMock()
        mock_stage_resp.json.return_value = {"file_id": "staged_xyz"}
        mock_stage_resp.raise_for_status = MagicMock()

        mock_httpx, _ = _make_httpx_mock(post_responses=[mock_stage_resp])

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = await client._ensure_staged("ref_1")

        assert result == "staged_xyz"
        assert client._staged_files["ref_1"] == "staged_xyz"
