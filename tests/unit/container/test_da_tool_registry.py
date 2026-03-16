"""
Tests for _create_investigation_tools(), create_milestone_engine(), and
create_da_provider() in container providers.

Validates that investigation tools are correctly wired with search_file and
deep_analysis tools, that create_milestone_engine properly forwards
these dependencies, and that the DA provider selection works correctly.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.container.providers.services import (
    _create_investigation_tools,
    create_milestone_engine,
)
from faultmaven.container.providers.infrastructure import create_da_provider

# =========================================================================
# Mock tools and container
# =========================================================================


def _make_mock_tool(name: str) -> MagicMock:
    """Create a mock AgentTool with the given name."""
    tool = MagicMock()
    tool.name = name
    return tool


def _make_mock_container(
    has_search_file: bool = False,
    has_deep_analysis: bool = False,
    has_web_search: bool = False,
    has_global_kb: bool = False,
    has_user_kb: bool = False,
) -> MagicMock:
    """Create a mock DI container with optional tool attributes."""
    container = MagicMock()

    if has_search_file:
        container.search_file_tool = _make_mock_tool("search_file")
    else:
        container.search_file_tool = None

    if has_deep_analysis:
        container.deep_analysis_tool = _make_mock_tool("deep_analysis")
    else:
        container.deep_analysis_tool = None

    if has_web_search:
        container.web_search_tool = _make_mock_tool("web_search")
    else:
        container.web_search_tool = None

    if has_global_kb:
        container.global_kb_adapter = _make_mock_tool("global_kb_qa")
    else:
        container.global_kb_adapter = None

    if has_user_kb:
        container.user_kb_adapter = _make_mock_tool("user_kb_qa")
    else:
        container.user_kb_adapter = None

    # Ensure remaining tool attributes are explicitly None to avoid MagicMock auto-attributes
    container.vectorize_file_tool = None
    container.case_evidence_qa_adapter = None

    return container


# =========================================================================
# _create_investigation_tools tests
# =========================================================================


@pytest.mark.unit
class TestCreateInvestigationTools:
    """Tests for _create_investigation_tools()."""

    def test_returns_registry_with_both_tools(self):
        """Returns registry with both tools when both are available on container."""
        container = _make_mock_container(
            has_search_file=True,
            has_deep_analysis=True,
        )

        registry = _create_investigation_tools(container)

        assert registry is not None
        tool_names = registry.list_tools()
        assert "search_file" in tool_names
        assert "deep_analysis" in tool_names
        assert len(tool_names) == 2

    def test_returns_registry_with_only_search_file(self):
        """Returns registry with only search_file when deep_analysis not available."""
        container = _make_mock_container(
            has_search_file=True,
            has_deep_analysis=False,
        )

        registry = _create_investigation_tools(container)

        assert registry is not None
        tool_names = registry.list_tools()
        assert "search_file" in tool_names
        assert len(tool_names) == 1

    def test_returns_registry_with_only_deep_analysis(self):
        """Returns registry with only deep_analysis when search_file not available."""
        container = _make_mock_container(
            has_search_file=False,
            has_deep_analysis=True,
        )

        registry = _create_investigation_tools(container)

        assert registry is not None
        tool_names = registry.list_tools()
        assert "deep_analysis" in tool_names
        assert len(tool_names) == 1

    def test_returns_none_when_no_tools_available(self):
        """Returns None when no tools are available on the container."""
        container = _make_mock_container(
            has_search_file=False,
            has_deep_analysis=False,
        )

        registry = _create_investigation_tools(container)

        assert registry is None

    def test_registry_has_exactly_expected_tools(self):
        """Registry should only contain investigation tools, not the full global set."""
        container = _make_mock_container(
            has_search_file=True,
            has_deep_analysis=True,
        )

        registry = _create_investigation_tools(container)

        tool_names = registry.list_tools()
        assert sorted(tool_names) == ["deep_analysis", "search_file"]
        assert len(registry.get_all_tools()) == 2


# =========================================================================
# create_milestone_engine tests
# =========================================================================


@pytest.mark.unit
class TestCreateMilestoneEngine:
    """Tests for create_milestone_engine()."""

    def test_returns_none_without_repository(self):
        """Returns None when case_repository is None."""
        mock_provider = MagicMock()
        mock_tools = MagicMock()
        mock_evidence = MagicMock()
        result = create_milestone_engine(
            llm_provider=mock_provider,
            case_repository=None,
            investigation_tools=mock_tools,
            evidence_service=mock_evidence,
        )
        assert result is None

    def test_creates_engine_with_investigation_tools(self):
        """Forwards investigation_tools and evidence_service to MilestoneEngine."""
        mock_provider = MagicMock()
        mock_repo = MagicMock()
        mock_tools = MagicMock()
        mock_evidence = MagicMock()

        result = create_milestone_engine(
            llm_provider=mock_provider,
            case_repository=mock_repo,
            investigation_tools=mock_tools,
            evidence_service=mock_evidence,
        )

        assert result is not None
        assert result.investigation_tools is mock_tools
        assert result.evidence_service is mock_evidence

    def test_returns_none_on_initialization_error(self):
        """Returns None when MilestoneEngine initialization raises."""
        mock_provider = MagicMock()
        mock_repo = MagicMock()
        mock_tools = MagicMock()
        mock_evidence = MagicMock()

        with patch(
            "faultmaven.core.investigation.milestone_engine.MilestoneEngine.__init__",
            side_effect=Exception("init failed"),
        ):
            result = create_milestone_engine(
                llm_provider=mock_provider,
                case_repository=mock_repo,
                investigation_tools=mock_tools,
                evidence_service=mock_evidence,
            )
            assert result is None

    def test_forwards_da_provider_and_model(self):
        """da_provider and da_model are forwarded to MilestoneEngine."""
        mock_provider = MagicMock()
        mock_repo = MagicMock()
        mock_tools = MagicMock()
        mock_evidence = MagicMock()
        mock_tcp = MagicMock()

        result = create_milestone_engine(
            llm_provider=mock_provider,
            case_repository=mock_repo,
            investigation_tools=mock_tools,
            evidence_service=mock_evidence,
            da_provider=mock_tcp,
            da_model="claude-sonnet-4-5-20250929",
        )

        assert result is not None
        assert result.da_provider is mock_tcp
        assert result.da_model == "claude-sonnet-4-5-20250929"

    def test_da_provider_defaults_to_none(self):
        """da_provider is None when not provided."""
        mock_provider = MagicMock()
        mock_repo = MagicMock()
        mock_tools = MagicMock()
        mock_evidence = MagicMock()

        result = create_milestone_engine(
            llm_provider=mock_provider,
            case_repository=mock_repo,
            investigation_tools=mock_tools,
            evidence_service=mock_evidence,
        )

        assert result is not None
        assert result.da_provider is None
        assert result.da_model is None


# =========================================================================
# create_da_provider tests
# =========================================================================


def _make_mock_provider(name: str, available: bool = True) -> MagicMock:
    """Create a mock BaseLLMProvider."""
    provider = MagicMock()
    provider.provider_name = name
    provider.is_available.return_value = available
    return provider


def _mock_settings(da_provider_value: str, da_model: str = "test-model"):
    """Create a mock settings object with DA_PROVIDER configured."""
    from faultmaven.config.settings import LLMProvider

    mock_settings = MagicMock()
    mock_settings.llm.get_da_provider.return_value = LLMProvider(da_provider_value)
    mock_settings.llm.get_da_model.return_value = da_model
    return mock_settings


@pytest.mark.unit
class TestCreateDaProvider:
    """Tests for create_da_provider()."""

    def test_returns_configured_provider(self):
        """Returns provider matching DA_PROVIDER setting."""
        anthropic = _make_mock_provider("anthropic")

        mock_registry = MagicMock()
        mock_registry.get_provider.return_value = anthropic

        with (
            patch(
                "faultmaven.infrastructure.llm.providers.get_registry",
                return_value=mock_registry,
            ),
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=_mock_settings("anthropic", "claude-sonnet-4-5-20250929"),
            ),
        ):
            provider, model = create_da_provider()

        assert provider is anthropic
        assert model == "claude-sonnet-4-5-20250929"
        mock_registry.get_provider.assert_called_with("anthropic")

    def test_returns_none_when_not_explicitly_set(self):
        """When DA_PROVIDER not set, returns (None, None) to use default router."""
        mock_settings = MagicMock()
        mock_settings.llm.da_provider = None  # Not explicitly set

        with patch(
            "faultmaven.config.settings.get_settings",
            return_value=mock_settings,
        ):
            provider, model = create_da_provider()

        assert provider is None
        assert model is None

    def test_returns_none_when_provider_unavailable(self):
        """Returns (None, None) when configured provider is not available."""
        mock_registry = MagicMock()
        mock_registry.get_provider.return_value = None

        with (
            patch(
                "faultmaven.infrastructure.llm.providers.get_registry",
                return_value=mock_registry,
            ),
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=_mock_settings("anthropic"),
            ),
        ):
            provider, model = create_da_provider()

        assert provider is None
        assert model is None

    def test_returns_none_when_provider_not_available(self):
        """Returns (None, None) when provider exists but is_available() is False."""
        anthropic = _make_mock_provider("anthropic", available=False)

        mock_registry = MagicMock()
        mock_registry.get_provider.return_value = anthropic

        with (
            patch(
                "faultmaven.infrastructure.llm.providers.get_registry",
                return_value=mock_registry,
            ),
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=_mock_settings("anthropic"),
            ),
        ):
            provider, model = create_da_provider()

        assert provider is None
        assert model is None
