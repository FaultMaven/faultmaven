"""Tests for WebSearchTool — provider abstraction and DA loop integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.web_search import (
    GoogleCSEProvider,
    SearchResult,
    TavilyProvider,
    WebSearchTool,
)


@pytest.fixture
def context():
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_1",
        metadata={"stage": "DIAGNOSIS"},
    )


@pytest.fixture
def context_no_stage():
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_1",
    )


@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.search = AsyncMock(
        return_value=[
            SearchResult(
                title="How to fix OOM killer",
                url="https://stackoverflow.com/q/123",
                snippet="The Linux OOM killer terminates processes when memory is exhausted.",
            ),
            SearchResult(
                title="Memory limits in Kubernetes",
                url="https://kubernetes.io/docs/limits",
                snippet="Configure resource limits to prevent OOM kills in pods.",
            ),
        ]
    )
    return provider


@pytest.fixture
def tool(mock_provider):
    return WebSearchTool(provider=mock_provider, max_results=3)


class TestAgentToolInterface:
    """Verify AgentTool interface compliance."""

    def test_name(self, tool):
        assert tool.name == "web_search"

    def test_description_is_string(self, tool):
        assert isinstance(tool.description, str)
        assert len(tool.description) > 20

    def test_parameters_schema_structure(self, tool):
        schema = tool.parameters_schema
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert schema["required"] == ["query"]

    def test_get_schema(self, tool):
        schema = tool.get_schema()
        assert schema["name"] == "web_search"
        assert "description" in schema
        assert "parameters" in schema

    def test_to_tool(self, tool):
        domain_tool = tool.to_tool()
        assert domain_tool.name == "web_search"

    def test_validate_params_missing_query(self, tool):
        error = tool.validate_params({})
        assert error is not None
        assert "query" in error

    def test_validate_params_valid(self, tool):
        error = tool.validate_params({"query": "OOM killer"})
        assert error is None


class TestProviderSelection:
    """Test auto-selection of search provider based on settings."""

    def test_is_available_with_provider(self, tool, mock_provider):
        assert tool.is_available() is True

    def test_is_available_without_provider(self):
        tool = WebSearchTool(provider=None)
        assert tool.is_available() is False

    def test_auto_select_tavily_when_key_set(self):
        settings = MagicMock()
        settings.knowledge.tavily_api_key = MagicMock()
        settings.knowledge.tavily_api_key.get_secret_value.return_value = "tvly-xxx"
        # Google CSE should not be checked when Tavily is available
        settings.tools.web_search_api_key = None

        provider = WebSearchTool._auto_select_provider(settings)
        assert isinstance(provider, TavilyProvider)

    def test_auto_select_google_cse_fallback(self):
        settings = MagicMock()
        settings.knowledge.tavily_api_key = None
        settings.tools.web_search_api_key = MagicMock()
        settings.tools.web_search_api_key.get_secret_value.return_value = "google-key"
        settings.tools.web_search_engine_id = "engine-123"
        settings.tools.web_search_api_endpoint = (
            "https://www.googleapis.com/customsearch/v1"
        )

        provider = WebSearchTool._auto_select_provider(settings)
        assert isinstance(provider, GoogleCSEProvider)

    def test_auto_select_none_when_no_keys(self):
        settings = MagicMock()
        settings.knowledge.tavily_api_key = None
        settings.tools.web_search_api_key = None

        provider = WebSearchTool._auto_select_provider(settings)
        assert provider is None


class TestExecution:
    """Test execute_with_context behavior."""

    @pytest.mark.asyncio
    async def test_successful_search(self, tool, context, mock_provider):
        result = await tool.execute_with_context(
            params={"query": "OOM killer linux"},
            context=context,
        )

        assert result.success is True
        assert "OOM killer" in result.data
        assert "stackoverflow.com" in result.data
        mock_provider.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, tool, context):
        result = await tool.execute_with_context(
            params={"query": ""},
            context=context,
        )

        assert result.success is False
        assert "No query" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_error(self, tool, context):
        result = await tool.execute_with_context(
            params={"query": "   "},
            context=context,
        )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_provider_returns_error(self, context):
        tool = WebSearchTool(provider=None)
        result = await tool.execute_with_context(
            params={"query": "test"},
            context=context,
        )

        assert result.success is False
        assert "not available" in result.error

    @pytest.mark.asyncio
    async def test_provider_error_returns_error(self, tool, context, mock_provider):
        mock_provider.search.side_effect = Exception("API rate limit")

        result = await tool.execute_with_context(
            params={"query": "test query"},
            context=context,
        )

        assert result.success is False
        assert "error" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_results_returns_no_match_message(
        self, tool, context, mock_provider
    ):
        mock_provider.search.return_value = []

        result = await tool.execute_with_context(
            params={"query": "extremely obscure query"},
            context=context,
        )

        assert result.success is True
        assert "No relevant results" in result.data


class TestStageEnrichment:
    """Test stage-aware query enrichment."""

    def test_diagnosis_stage_enriches_error_query(self, tool, context):
        enhanced = tool._enhance_query_with_context("OOM error in production", context)
        assert "root cause" in enhanced or "diagnosis" in enhanced

    def test_mitigation_stage_enrichment(self, tool):
        ctx = ToolContext(
            session_id="s",
            case_id="c",
            organization_id="o",
            user_id="u",
            metadata={"stage": "MITIGATION"},
        )
        enhanced = tool._enhance_query_with_context("service crash", ctx)
        assert "workaround" in enhanced or "mitigation" in enhanced

    def test_treatment_stage_enrichment(self, tool):
        ctx = ToolContext(
            session_id="s",
            case_id="c",
            organization_id="o",
            user_id="u",
            metadata={"stage": "TREATMENT"},
        )
        enhanced = tool._enhance_query_with_context("fix database", ctx)
        assert "fix" in enhanced or "solution" in enhanced or "resolve" in enhanced

    def test_no_stage_no_enrichment(self, tool, context_no_stage):
        query = "OOM error"
        enhanced = tool._enhance_query_with_context(query, context_no_stage)
        assert enhanced == query

    def test_error_keyword_triggers_error_enrichment(self, tool, context):
        enhanced = tool._enhance_query_with_context("exception thrown", context)
        assert enhanced != "exception thrown"

    def test_non_error_keyword_triggers_default_enrichment(self, tool):
        ctx = ToolContext(
            session_id="s",
            case_id="c",
            organization_id="o",
            user_id="u",
            metadata={"stage": "DIAGNOSIS"},
        )
        enhanced = tool._enhance_query_with_context("memory usage high", ctx)
        assert (
            "troubleshoot" in enhanced
            or "diagnose" in enhanced
            or "analyze" in enhanced
        )


class TestGoogleCSEDomainFiltering:
    """Test that Google CSE provider gets domain-filtered queries."""

    @pytest.mark.asyncio
    async def test_google_cse_appends_site_filter(self, context):
        google_provider = AsyncMock(spec=GoogleCSEProvider)
        google_provider.is_available.return_value = True
        google_provider.search.return_value = []
        # Make isinstance check work
        google_provider.__class__ = GoogleCSEProvider

        tool = WebSearchTool(provider=google_provider)
        await tool.execute_with_context(
            params={"query": "test query"},
            context=context,
        )

        # Verify the query passed to provider contains site: filters
        call_args = google_provider.search.call_args
        search_query = call_args[0][0]  # First positional arg
        assert "site:" in search_query


class TestFormatResults:

    def test_format_with_results(self):
        results = [
            SearchResult(
                title="Title 1", url="https://example.com", snippet="Snippet 1"
            ),
        ]
        formatted = WebSearchTool._format_results(results, "test query")
        assert "Title 1" in formatted
        assert "https://example.com" in formatted
        assert "Snippet 1" in formatted
        assert "test query" in formatted

    def test_format_empty_results(self):
        formatted = WebSearchTool._format_results([], "test query")
        assert "No relevant results" in formatted

    def test_format_includes_verification_note(self):
        results = [
            SearchResult(title="T", url="https://x.com", snippet="S"),
        ]
        formatted = WebSearchTool._format_results(results, "q")
        assert "Verify" in formatted
