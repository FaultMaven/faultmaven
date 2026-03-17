"""Tests for Tier 2 service factory."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.preprocessing.tier2.basic import BasicTier2Service
from faultmaven.core.preprocessing.tier2.external import ExternalTier2Client
from faultmaven.core.preprocessing.tier2.factory import create_tier2_service
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service


class TestCreateTier2Service:
    def test_disabled_returns_none(self):
        result = create_tier2_service(backend="disabled")
        assert result is None

    def test_external_with_url(self):
        result = create_tier2_service(
            backend="external",
            base_url="http://tier2.example.com",
            api_key="test-key",
            timeout_seconds=60,
        )
        assert isinstance(result, ExternalTier2Client)
        assert result.base_url == "http://tier2.example.com"
        assert result.api_key == "test-key"
        assert result.timeout_seconds == 60

    def test_external_without_url_raises(self):
        with pytest.raises(ValueError, match="DEEP_ANALYSIS_URL required"):
            create_tier2_service(backend="external")

    def test_local_with_llm_client(self):
        mock_llm = AsyncMock()
        mock_storage = AsyncMock()
        result = create_tier2_service(
            backend="local",
            llm_client=mock_llm,
            storage_service=mock_storage,
        )
        assert isinstance(result, LocalTier2Service)
        assert result.llm_client is mock_llm

    def test_local_without_llm_raises(self):
        with pytest.raises(ValueError, match="LLM client required"):
            create_tier2_service(backend="local")

    def test_basic_with_storage(self):
        mock_storage = AsyncMock()
        result = create_tier2_service(
            backend="basic",
            storage_service=mock_storage,
        )
        assert isinstance(result, BasicTier2Service)
        assert result.storage_service is mock_storage

    def test_basic_without_storage_raises(self):
        with pytest.raises(ValueError, match="Storage service required"):
            create_tier2_service(backend="basic")

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown DEEP_ANALYSIS_BACKEND"):
            create_tier2_service(backend="quantum")

    def test_default_is_disabled(self):
        result = create_tier2_service()
        assert result is None

    def test_external_with_storage_service(self):
        mock_storage = AsyncMock()
        result = create_tier2_service(
            backend="external",
            base_url="http://example.com",
            storage_service=mock_storage,
        )
        assert isinstance(result, ExternalTier2Client)
        assert result.storage_service is mock_storage
