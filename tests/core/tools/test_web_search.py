"""
Unit tests for WebSearchTool.

This module tests the web search functionality including:
- Tool initialization and configuration
- Search query execution
- Context-aware query enhancement
- Error handling and fallbacks
- Domain filtering and safety
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from faultmaven.tools.web_search import WebSearchTool


class TestWebSearchTool:
    """Test suite for WebSearchTool functionality"""

    def setup_method(self):
        """Set up test fixtures"""
        self.mock_api_key = "test_api_key"
        self.mock_api_endpoint = "https://api.example.com/search"
        self.mock_search_engine_id = "test_engine_id"
        self.trusted_domains = ["stackoverflow.com", "docs.example.com"]

        # Create tool instance
        self.tool = WebSearchTool(
            api_key=self.mock_api_key,
            api_endpoint=self.mock_api_endpoint,
            trusted_domains=self.trusted_domains,
            max_results=3,
        )

    def test_tool_initialization(self):
        """Test tool initialization with various parameters"""
        # Test with all parameters
        tool = WebSearchTool(
            api_key="key123",
            api_endpoint="https://api.test.com",
            trusted_domains=["example.com"],
            max_results=5,
        )

        assert tool._api_key == "key123"
        assert tool._api_endpoint == "https://api.test.com"
        assert "example.com" in tool._trusted_domains
        assert tool._max_results == 5
        assert tool.name == "web_search"

    def test_tool_initialization_with_defaults(self):
        """Test tool initialization with default values"""
        tool = WebSearchTool()

        assert tool._api_key == ""  # Should get from env or default to empty
        assert "stackoverflow.com" in tool._trusted_domains
        assert "docs.microsoft.com" in tool._trusted_domains
        assert tool._max_results == 3

    def test_tool_initialization_with_env_variables(self):
        """Test tool initialization using environment variables"""
        with patch.dict(
            "os.environ",
            {
                "WEB_SEARCH_API_KEY": "env_key",
                "WEB_SEARCH_API_ENDPOINT": "https://env.api.com",
            },
        ):
            # Reset settings to force reinitialization with new environment variables
            from faultmaven.config.settings import reset_settings
            reset_settings()
            
            tool = WebSearchTool()
            assert tool._api_key == "env_key"
            assert tool._api_endpoint == "https://env.api.com"

    def test_is_available_with_api_key(self):
        """Test is_available method with API key"""
        tool = WebSearchTool(api_key="test_key")
        assert tool.is_available() is True

    def test_is_available_without_api_key(self):
        """Test is_available method without API key"""
        tool = WebSearchTool(api_key="")
        assert tool.is_available() is False

    def test_synchronous_run_raises_not_implemented(self):
        """Test that synchronous _run method raises NotImplementedError"""
        with pytest.raises(NotImplementedError):
            self.tool._run("test query")

    @pytest.mark.asyncio
    async def test_arun_without_api_key(self):
        """Test _arun method without API key"""
        tool = WebSearchTool(api_key="")
        result = await tool._arun("test query")
        assert "Web search is not available" in result
        assert "No API key configured" in result

    @pytest.mark.asyncio
    async def test_arun_with_successful_search(self):
        """Test _arun method with successful search"""
        # Mock response data
        mock_response_data = {
            "items": [
                {
                    "title": "Test Result 1",
                    "link": "https://example.com/1",
                    "snippet": "This is a test result snippet",
                },
                {
                    "title": "Test Result 2",
                    "link": "https://example.com/2",
                    "snippet": "Another test result snippet",
                },
            ]
        }

        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.json.return_value = mock_response_data
                mock_response.raise_for_status.return_value = None

                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_response
                )

                result = await self.tool._arun("test query")

                assert "Web search results for 'test query'" in result
                assert "Test Result 1" in result
                assert "Test Result 2" in result
                assert "https://example.com/1" in result
                assert "verify solutions in a test environment" in result

    @pytest.mark.asyncio
    async def test_arun_with_network_error(self):
        """Test _arun method with network error"""
        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    side_effect=httpx.RequestError("Network error")
                )

                result = await self.tool._arun("test query")

                assert "Web search encountered an unexpected error" in result

    @pytest.mark.asyncio
    async def test_arun_with_http_error(self):
        """Test _arun method with HTTP error"""
        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "HTTP Error", request=Mock(), response=Mock()
                )

                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_response
                )

                result = await self.tool._arun("test query")

                assert "Web search encountered an unexpected error" in result

    @pytest.mark.asyncio
    async def test_arun_with_empty_results(self):
        """Test _arun method with empty search results"""
        mock_response_data = {"items": []}

        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.json.return_value = mock_response_data
                mock_response.raise_for_status.return_value = None

                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_response
                )

                result = await self.tool._arun("test query")

                assert "No relevant results found" in result
                assert "test query" in result

    @pytest.mark.asyncio
    async def test_arun_with_no_items_key(self):
        """Test _arun method with response missing items key"""
        mock_response_data = {"other_key": "value"}

        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.json.return_value = mock_response_data
                mock_response.raise_for_status.return_value = None

                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_response
                )

                result = await self.tool._arun("test query")

                assert "No relevant results found" in result

    def test_enhance_query_with_context_no_context(self):
        """Test query enhancement without context"""
        query = "test error message"
        enhanced = self.tool._enhance_query_with_context(query, None)
        assert enhanced == query

    def test_enhance_query_with_context_phase_specific(self):
        """Test query enhancement with phase-specific context"""
        query = "database error"
        context = {"phase": "define_blast_radius"}

        enhanced = self.tool._enhance_query_with_context(query, context)
        assert "impact scope affected systems" in enhanced
        assert "database error" in enhanced

    def test_enhance_query_with_context_environment(self):
        """Test query enhancement with environment context"""
        query = "connection timeout"
        context = {"environment": "production"}

        enhanced = self.tool._enhance_query_with_context(query, context)
        assert "production environment" in enhanced
        assert "connection timeout" in enhanced

    def test_enhance_query_with_context_multiple_phases(self):
        """Test query enhancement for different phases"""
        query = "exception occurred"

        # Test different phases
        phase_contexts = {
            "define_blast_radius": "impact scope affected systems",
            "establish_timeline": "when started occurred timeline",
            "formulate_hypothesis": "root cause possible reasons",
            "validate_hypothesis": "test validate confirm",
            "propose_solution": "fix solution resolve",
        }

        for phase, expected_addition in phase_contexts.items():
            context = {"phase": phase}
            enhanced = self.tool._enhance_query_with_context(query, context)
            assert expected_addition in enhanced

    def test_format_search_results_with_long_snippet(self):
        """Test formatting of search results with long snippets"""
        results = {
            "items": [
                {
                    "title": "Long Result",
                    "link": "https://example.com/long",
                    "snippet": "A" * 250,  # Very long snippet
                }
            ]
        }

        formatted = self.tool._format_search_results(results, "test query")

        assert "Long Result" in formatted
        assert "..." in formatted  # Should be truncated
        assert len(formatted.split("Summary: ")[1].split("\n")[0]) <= 200

    def test_format_search_results_with_missing_fields(self):
        """Test formatting of search results with missing fields"""
        results = {
            "items": [
                {
                    "title": "Complete Result",
                    "link": "https://example.com/complete",
                    "snippet": "Complete snippet",
                },
                {
                    "link": "https://example.com/notitle"
                    # Missing title and snippet
                },
                {
                    "title": "No Link Result"
                    # Missing link and snippet
                },
            ]
        }

        formatted = self.tool._format_search_results(results, "test query")

        assert "Complete Result" in formatted
        assert "No title" in formatted
        assert "No link" in formatted
        assert "No description available" in formatted

    def test_get_search_domains(self):
        """Test getting trusted domains"""
        domains = self.tool.get_search_domains()
        assert "stackoverflow.com" in domains
        assert "docs.example.com" in domains
        assert isinstance(domains, list)

    def test_add_trusted_domain(self):
        """Test adding a trusted domain"""
        initial_count = len(self.tool._trusted_domains)
        self.tool.add_trusted_domain("newdomain.com")

        assert len(self.tool._trusted_domains) == initial_count + 1
        assert "newdomain.com" in self.tool._trusted_domains

    def test_add_trusted_domain_duplicate(self):
        """Test adding a duplicate trusted domain"""
        self.tool.add_trusted_domain("stackoverflow.com")  # Already exists

        # Should not add duplicate
        domain_count = self.tool._trusted_domains.count("stackoverflow.com")
        assert domain_count == 1

    def test_remove_trusted_domain(self):
        """Test removing a trusted domain"""
        self.tool.add_trusted_domain("removeme.com")
        initial_count = len(self.tool._trusted_domains)

        self.tool.remove_trusted_domain("removeme.com")

        assert len(self.tool._trusted_domains) == initial_count - 1
        assert "removeme.com" not in self.tool._trusted_domains

    def test_remove_trusted_domain_not_exists(self):
        """Test removing a domain that doesn't exist"""
        initial_count = len(self.tool._trusted_domains)
        self.tool.remove_trusted_domain("nonexistent.com")

        # Should not change the list
        assert len(self.tool._trusted_domains) == initial_count

    @pytest.mark.asyncio
    async def test_make_search_request_success(self):
        """Test successful search request"""
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None

        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            mock_client = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await self.tool._make_search_request(mock_client, "test query")

            assert result == mock_response
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_make_search_request_request_error(self):
        """Test search request with RequestError"""
        mock_client = Mock()
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("Network error"))

        with pytest.raises(httpx.RequestError):
            await self.tool._make_search_request(mock_client, "test query")

    @pytest.mark.asyncio
    async def test_make_search_request_http_status_error(self):
        """Test search request with HTTPStatusError"""
        mock_client = Mock()
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "HTTP Error", request=Mock(), response=Mock()
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            await self.tool._make_search_request(mock_client, "test query")

    @pytest.mark.asyncio
    async def test_arun_with_context_enhancement(self):
        """Test _arun method with context enhancement"""
        context = {"phase": "formulate_hypothesis", "environment": "production"}

        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_response = Mock()
                mock_response.json.return_value = {"items": []}
                mock_response.raise_for_status.return_value = None

                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_response
                )

                result = await self.tool._arun("database error", context)

                # Verify the result is not None
                assert result is not None
                assert (
                    "No relevant results found" in result
                    or "Web search results" in result
                )

                # Verify the query was enhanced
                call_args = (
                    mock_client.return_value.__aenter__.return_value.get.call_args
                )
                params = call_args[1]["params"]
                query = params["q"]

                assert "root cause possible reasons" in query
                assert "production environment" in query

    def test_tool_description_and_name(self):
        """Test tool name and description"""
        assert self.tool.name == "web_search"
        assert "last resort" in self.tool.description
        assert "KnowledgeBaseTool returns no results" in self.tool.description
        assert "trusted domains" in self.tool.description


class TestWebSearchToolIntegration:
    """Integration tests for WebSearchTool with external dependencies"""

    def test_tool_with_real_environment_variables(self):
        """Test tool configuration with real environment variables"""
        # This test would use real environment variables if available
        # but should not fail if they're not set
        tool = WebSearchTool()

        # Should initialize without errors
        assert tool.name == "web_search"
        assert isinstance(tool._trusted_domains, list)
        assert len(tool._trusted_domains) > 0

    def test_tool_logging_setup(self):
        """Test that logging is properly configured"""
        tool = WebSearchTool()

        # Should have a logger instance
        assert hasattr(tool, "_logger")
        assert tool._logger is not None

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test that requests have proper timeout handling"""
        tool = WebSearchTool(api_key="test_key")

        # Mock a slow response
        with patch.dict("os.environ", {"WEB_SEARCH_ENGINE_ID": "test_engine"}):
            with patch("httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                    side_effect=httpx.TimeoutException("Request timed out")
                )

                result = await tool._arun("test query")

                # Should handle timeout gracefully
                assert "Web search encountered an unexpected error" in result
