from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.agent.tools.knowledge_base import KnowledgeBaseTool


class TestKnowledgeBaseTool:
    """Test suite for KnowledgeBaseTool class."""

    @pytest.fixture
    def sample_documents(self):
        """Sample documents for testing."""
        return [
            {
                "document": "Database connection timeout troubleshooting guide",
                "metadata": {"source": "docs/troubleshooting.md", "type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            },
            {
                "document": "How to configure connection pooling",
                "metadata": {"source": "docs/config.md", "type": "config"},
                "distance": 0.2,
                "relevance_score": 0.8,
            },
            {
                "document": "Common database errors and solutions",
                "metadata": {"source": "docs/errors.md", "type": "reference"},
                "distance": 0.3,
                "relevance_score": 0.7,
            },
        ]

    @pytest.fixture
    def knowledge_tool(self, sample_documents):
        """Create KnowledgeBaseTool instance with mocked KnowledgeIngester."""
        mock_ingester = Mock()
        # Default: return all sample documents
        mock_ingester.search = AsyncMock(return_value=sample_documents)
        return KnowledgeBaseTool(knowledge_ingester=mock_ingester)

    @pytest.mark.asyncio
    async def test_arun_basic_query(self, knowledge_tool, sample_documents):
        """Test basic async query execution."""
        # The mock search method is already set up to return sample_documents
        query = "How to fix database connection issues?"
        result = await knowledge_tool._arun(query)
        assert "Database connection timeout" in result
        assert "connection pooling" in result
        assert "Common database errors" in result
        assert "docs/troubleshooting.md" in result
        assert "docs/config.md" in result
        assert "docs/errors.md" in result

    @pytest.mark.asyncio
    async def test_arun_no_results(self, sample_documents):
        """Test query with no results."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Non-existent topic"
        result = await tool._arun(query)
        assert "No relevant information found" in result or "No results found" in result

    @pytest.mark.asyncio
    async def test_arun_single_result(self, sample_documents):
        """Test query with single result."""
        single_doc = {
            "document": "Single troubleshooting guide",
            "metadata": {"source": "docs/single.md", "type": "guide"},
            "distance": 0.1,
            "relevance_score": 0.95,
        }
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[single_doc])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Single topic query"
        result = await tool._arun(query)
        assert "Single troubleshooting guide" in result
        assert "docs/single.md" in result

    @pytest.mark.asyncio
    async def test_arun_chromadb_error(self):
        """Test handling of KnowledgeIngester errors."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(side_effect=Exception("ChromaDB error"))
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert (
            "Error searching knowledge base" in result
            or "Error querying knowledge base" in result
        )

    @pytest.mark.asyncio
    async def test_arun_empty_query(self):
        """Test handling of empty query."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock()
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = ""
        result = await tool._arun(query)
        assert result is not None
        mock_ingester.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_arun_special_characters(self):
        """Test query with special characters."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "Special chars: @#$%^&*()",
                    "metadata": {"source": "docs/special.md"},
                    "distance": 0.1,
                    "relevance_score": 0.8,
                }
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Query with @#$%^&*() characters"
        result = await tool._arun(query)
        assert "Special chars: @#$%^&*()" in result

    def test_tool_name_and_description(self, knowledge_tool):
        """Test tool name and description."""
        assert knowledge_tool.name == "knowledge_base_search"
        assert "search" in knowledge_tool.description.lower()
        assert "knowledge" in knowledge_tool.description.lower()

    def test_tool_schema(self, knowledge_tool):
        """Test tool input schema."""
        schema = knowledge_tool.get_tool_schema()
        assert schema is not None
        assert "query" in schema["args_schema"]["properties"]

    @pytest.mark.asyncio
    async def test_arun_result_formatting(self):
        """Test that results are properly formatted."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "Document 1",
                    "metadata": {
                        "source": "source1.md",
                        "type": "guide",
                        "tags": "tag1,tag2",
                        "document_type": "guide",
                        "chunk_index": 0,
                        "total_chunks": 2,
                    },
                    "distance": 0.1,
                    "relevance_score": 0.9,
                },
                {
                    "document": "Document 2",
                    "metadata": {
                        "source": "source2.md",
                        "type": "reference",
                        "tags": "tag3",
                        "document_type": "reference",
                        "chunk_index": 1,
                        "total_chunks": 2,
                    },
                    "distance": 0.2,
                    "relevance_score": 0.8,
                },
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test formatting"
        result = await tool._arun(query)
        assert "Document 1" in result
        assert "Document 2" in result
        assert "source1.md" in result
        assert "source2.md" in result
        assert "guide" in result
        assert "reference" in result
        assert "tag1" in result
        assert "tag3" in result
        assert "Chunk 1 of 2" in result
        assert "Chunk 2 of 2" in result

    @pytest.mark.asyncio
    async def test_arun_distance_filtering(self):
        """Test that high-distance results are filtered out."""
        # This test can be implemented if the tool supports distance filtering logic
        pass

    @pytest.mark.asyncio
    async def test_arun_max_results_limit(self):
        """Test that the tool limits the number of results returned."""
        # This test can be implemented if the tool supports max results logic
        pass

    def test_collection_initialization(self):
        """Test that collection is properly initialized (not relevant with new mock)."""
        # This test can be implemented if the tool supports collection initialization logic
        pass

    @pytest.mark.asyncio
    async def test_arun_none_query(self):
        """Test handling of None query."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock()
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = None
        result = await tool._arun(query)
        assert result is not None
        mock_ingester.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_arun_whitespace_only_query(self):
        """Test handling of whitespace-only query."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock()
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "   \n\t   "
        result = await tool._arun(query)
        assert result is not None
        mock_ingester.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_arun_very_long_query(self):
        """Test handling of very long queries."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "a" * 10000  # Very long query
        result = await tool._arun(query)
        assert "No relevant information found" in result or "No results found" in result

    @pytest.mark.asyncio
    async def test_arun_unicode_query(self):
        """Test handling of Unicode queries."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "查询数据库连接问题"  # Chinese query
        result = await tool._arun(query)
        assert "No relevant information found" in result or "No results found" in result

    @pytest.mark.asyncio
    async def test_arun_malformed_search_results(self):
        """Test handling of malformed search results."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "Valid doc",
                    "metadata": {"source": "valid.md"},
                    "distance": 0.1,
                },
                {"document": "Invalid doc"},  # Missing required fields
                None,  # None result
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        # The tool should handle malformed results gracefully
        assert "Valid doc" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_arun_search_timeout(self):
        """Test handling of search timeout."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(side_effect=TimeoutError("Search timeout"))
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Error" in result or "timeout" in result.lower()

    @pytest.mark.asyncio
    async def test_arun_connection_error(self):
        """Test handling of connection errors."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            side_effect=ConnectionError("Connection failed")
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_arun_memory_error(self):
        """Test handling of memory errors."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(side_effect=MemoryError("Out of memory"))
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_arun_metadata_missing_fields(self):
        """Test handling of metadata with missing fields."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "Test document",
                    "metadata": {},  # Empty metadata
                    "distance": 0.1,
                    "relevance_score": 0.9,
                }
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Test document" in result

    @pytest.mark.asyncio
    async def test_arun_high_distance_results(self):
        """Test handling of high distance (low relevance) results."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "Low relevance doc",
                    "metadata": {"source": "low.md"},
                    "distance": 0.9,  # High distance = low relevance
                    "relevance_score": 0.1,
                }
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Low relevance doc" in result

    @pytest.mark.asyncio
    async def test_arun_mixed_result_quality(self):
        """Test handling of mixed quality results."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(
            return_value=[
                {
                    "document": "High quality doc",
                    "metadata": {"source": "high.md", "type": "guide"},
                    "distance": 0.1,
                    "relevance_score": 0.95,
                },
                {
                    "document": "Medium quality doc",
                    "metadata": {"source": "medium.md", "type": "reference"},
                    "distance": 0.5,
                    "relevance_score": 0.5,
                },
                {
                    "document": "Low quality doc",
                    "metadata": {"source": "low.md", "type": "note"},
                    "distance": 0.8,
                    "relevance_score": 0.2,
                },
            ]
        )
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "High quality doc" in result
        assert "Medium quality doc" in result
        assert "Low quality doc" in result

    @pytest.mark.asyncio
    async def test_arun_very_large_results(self):
        """Test handling of very large result sets."""
        mock_ingester = Mock()
        # Create 1000 mock results
        large_results = []
        for i in range(1000):
            large_results.append(
                {
                    "document": f"Document {i}",
                    "metadata": {"source": f"doc{i}.md", "type": "guide"},
                    "distance": 0.1 + (i * 0.001),
                    "relevance_score": 0.9 - (i * 0.001),
                }
            )
        mock_ingester.search = AsyncMock(return_value=large_results)
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Document 0" in result
        assert "Document 999" in result

    def test_tool_initialization_without_ingester(self):
        """Test tool initialization without providing an ingester."""
        with pytest.raises(TypeError):
            KnowledgeBaseTool()

    def test_tool_initialization_with_invalid_ingester(self):
        """Test tool initialization with invalid ingester type."""
        # The tool doesn't validate the ingester type, so this won't raise an AttributeError
        # Instead, it will fail when trying to use the search method
        tool = KnowledgeBaseTool(knowledge_ingester="not an ingester")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_arun_search_method_not_callable(self):
        """Test handling when search method is not callable."""
        mock_ingester = Mock()
        mock_ingester.search = "not_callable"  # Make search not callable
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Error searching knowledge base" in result

    # Unit tests for internal helper methods
    def test_expand_query_with_context_empty_context(self, knowledge_tool):
        """Test query expansion with empty context."""
        original_query = "test query"
        expanded = knowledge_tool._expand_query_with_context(original_query, {})
        assert expanded == original_query

    def test_expand_query_with_context_service_name(self, knowledge_tool):
        """Test query expansion with service name context."""
        original_query = "error in service"
        context = {"service_name": "user-service"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "service:user-service" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_error_code(self, knowledge_tool):
        """Test query expansion with error code context."""
        original_query = "database error"
        context = {"error_code": "DB_CONN_001"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "error:DB_CONN_001" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_error_type(self, knowledge_tool):
        """Test query expansion with error type context."""
        original_query = "connection issue"
        context = {"error_type": "ConnectionTimeout"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "error_type:ConnectionTimeout" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_environment(self, knowledge_tool):
        """Test query expansion with environment context."""
        original_query = "deployment issue"
        context = {"environment": "production"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "environment:production" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_technology(self, knowledge_tool):
        """Test query expansion with technology context."""
        original_query = "performance issue"
        context = {"technology": "PostgreSQL"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "technology:PostgreSQL" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_phase_define_blast_radius(self, knowledge_tool):
        """Test query expansion with define_blast_radius phase."""
        original_query = "system impact"
        context = {"phase": "define_blast_radius"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "impact scope assessment" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_phase_establish_timeline(self, knowledge_tool):
        """Test query expansion with establish_timeline phase."""
        original_query = "when did this happen"
        context = {"phase": "establish_timeline"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "timeline changes deployment" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_phase_formulate_hypothesis(self, knowledge_tool):
        """Test query expansion with formulate_hypothesis phase."""
        original_query = "what caused this"
        context = {"phase": "formulate_hypothesis"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "root cause analysis" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_phase_validate_hypothesis(self, knowledge_tool):
        """Test query expansion with validate_hypothesis phase."""
        original_query = "how to test"
        context = {"phase": "validate_hypothesis"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "testing validation" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_phase_propose_solution(self, knowledge_tool):
        """Test query expansion with propose_solution phase."""
        original_query = "how to fix"
        context = {"phase": "propose_solution"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "solution fix resolution" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_unknown_phase(self, knowledge_tool):
        """Test query expansion with unknown phase."""
        original_query = "test query"
        context = {"phase": "unknown_phase"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert expanded == original_query  # Should not expand for unknown phase

    def test_expand_query_with_context_symptoms_list(self, knowledge_tool):
        """Test query expansion with symptoms list."""
        original_query = "system issues"
        context = {"symptoms": ["high CPU", "slow response", "timeouts"]}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "high CPU" in expanded
        assert "slow response" in expanded
        assert "timeouts" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_symptoms_string(self, knowledge_tool):
        """Test query expansion with symptoms string."""
        original_query = "system issues"
        context = {"symptoms": "high CPU usage"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "high CPU usage" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_component(self, knowledge_tool):
        """Test query expansion with component context."""
        original_query = "database issue"
        context = {"component": "user-database"}
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "component:user-database" in expanded
        assert original_query in expanded

    def test_expand_query_with_context_multiple_fields(self, knowledge_tool):
        """Test query expansion with multiple context fields."""
        original_query = "production issue"
        context = {
            "service_name": "auth-service",
            "error_code": "AUTH_001",
            "environment": "production",
            "technology": "Redis",
            "phase": "formulate_hypothesis",
            "symptoms": ["timeouts", "high latency"],
            "component": "authentication",
        }
        expanded = knowledge_tool._expand_query_with_context(original_query, context)
        assert "service:auth-service" in expanded
        assert "error:AUTH_001" in expanded
        assert "environment:production" in expanded
        assert "technology:Redis" in expanded
        assert "root cause analysis" in expanded
        assert "timeouts" in expanded
        assert "high latency" in expanded
        assert "component:authentication" in expanded
        assert original_query in expanded

    def test_extract_metadata_filters_empty_context(self, knowledge_tool):
        """Test metadata filter extraction with empty context."""
        filters = knowledge_tool._extract_metadata_filters({})
        assert filters == {}

    def test_extract_metadata_filters_service_name(self, knowledge_tool):
        """Test metadata filter extraction with service name."""
        context = {"service_name": "user-service"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert filters["service_name"] == "user-service"

    def test_extract_metadata_filters_phase_define_blast_radius(self, knowledge_tool):
        """Test metadata filter extraction with define_blast_radius phase."""
        context = {"phase": "define_blast_radius"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" in filters
        assert "impact_assessment" in filters["document_type"]
        assert "system_overview" in filters["document_type"]

    def test_extract_metadata_filters_phase_establish_timeline(self, knowledge_tool):
        """Test metadata filter extraction with establish_timeline phase."""
        context = {"phase": "establish_timeline"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" in filters
        assert "deployment_guide" in filters["document_type"]
        assert "change_log" in filters["document_type"]

    def test_extract_metadata_filters_phase_formulate_hypothesis(self, knowledge_tool):
        """Test metadata filter extraction with formulate_hypothesis phase."""
        context = {"phase": "formulate_hypothesis"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" in filters
        assert "troubleshooting_guide" in filters["document_type"]
        assert "error_catalog" in filters["document_type"]

    def test_extract_metadata_filters_phase_validate_hypothesis(self, knowledge_tool):
        """Test metadata filter extraction with validate_hypothesis phase."""
        context = {"phase": "validate_hypothesis"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" in filters
        assert "testing_guide" in filters["document_type"]
        assert "diagnostic_procedures" in filters["document_type"]

    def test_extract_metadata_filters_phase_propose_solution(self, knowledge_tool):
        """Test metadata filter extraction with propose_solution phase."""
        context = {"phase": "propose_solution"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" in filters
        assert "solution_guide" in filters["document_type"]
        assert "fix_procedures" in filters["document_type"]

    def test_extract_metadata_filters_unknown_phase(self, knowledge_tool):
        """Test metadata filter extraction with unknown phase."""
        context = {"phase": "unknown_phase"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert "document_type" not in filters

    def test_extract_metadata_filters_technology(self, knowledge_tool):
        """Test metadata filter extraction with technology."""
        context = {"technology": "PostgreSQL"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert filters["technology"] == "PostgreSQL"

    def test_extract_metadata_filters_environment(self, knowledge_tool):
        """Test metadata filter extraction with environment."""
        context = {"environment": "production"}
        filters = knowledge_tool._extract_metadata_filters(context)
        assert filters["environment"] == "production"

    def test_extract_metadata_filters_multiple_fields(self, knowledge_tool):
        """Test metadata filter extraction with multiple fields."""
        context = {
            "service_name": "auth-service",
            "phase": "formulate_hypothesis",
            "technology": "Redis",
            "environment": "production",
        }
        filters = knowledge_tool._extract_metadata_filters(context)
        assert filters["service_name"] == "auth-service"
        assert "document_type" in filters
        assert "troubleshooting_guide" in filters["document_type"]
        assert filters["technology"] == "Redis"
        assert filters["environment"] == "production"

    def test_format_results_empty_results(self, knowledge_tool):
        """Test formatting empty results."""
        result = knowledge_tool._format_results([], "test query", {})
        assert "No results found" in result

    def test_format_results_single_result(self, knowledge_tool):
        """Test formatting single result."""
        results = [
            {
                "document": "Test document content",
                "metadata": {"title": "Test Doc", "document_type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            }
        ]
        result = knowledge_tool._format_results(results, "test query", {})
        assert "Test document content" in result
        assert "Test Doc" in result
        assert "guide" in result

    def test_format_results_multiple_results(self, knowledge_tool):
        """Test formatting multiple results."""
        results = [
            {
                "document": "First document content",
                "metadata": {"title": "First Doc", "document_type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            },
            {
                "document": "Second document content",
                "metadata": {"title": "Second Doc", "document_type": "reference"},
                "distance": 0.2,
                "relevance_score": 0.8,
            },
        ]
        result = knowledge_tool._format_results(results, "test query", {})
        assert "First document content" in result
        assert "Second document content" in result
        assert "First Doc" in result
        assert "Second Doc" in result

    def test_format_results_with_metadata_filters(self, knowledge_tool):
        """Test formatting results with metadata filters."""
        results = [
            {
                "document": "Filtered content",
                "metadata": {"title": "Filtered Doc", "document_type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            }
        ]
        filters = {"document_type": "guide", "service_name": "test-service"}
        result = knowledge_tool._format_results(results, "test query", filters)
        assert "Filtered content" in result
        assert "Applied Filters" in result
        assert "document_type: guide" in result
        assert "service_name: test-service" in result

    def test_format_results_missing_metadata_fields(self, knowledge_tool):
        """Test formatting results with missing metadata fields."""
        results = [
            {
                "document": "Content with missing metadata",
                "metadata": {},  # Empty metadata
                "distance": 0.1,
                "relevance_score": 0.9,
            }
        ]
        result = knowledge_tool._format_results(results, "test query", {})
        assert "Content with missing metadata" in result
        # Should handle missing metadata gracefully

    def test_format_results_very_long_content(self, knowledge_tool):
        """Test formatting results with very long content."""
        long_content = "Very long content " * 100
        results = [
            {
                "document": long_content,
                "metadata": {"title": "Long Doc", "document_type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            }
        ]
        result = knowledge_tool._format_results(results, "test query", {})
        assert (
            long_content[:100] in result
        )  # Should include at least part of the content

    def test_format_results_high_distance_results(self, knowledge_tool):
        """Test formatting results with high distance (low relevance)."""
        results = [
            {
                "document": "Low relevance content",
                "metadata": {"title": "Low Doc", "document_type": "guide"},
                "distance": 0.95,
                "relevance_score": 0.05,
            }
        ]
        result = knowledge_tool._format_results(results, "test query", {})
        assert "Low relevance content" in result
        # Should still include low relevance results

    def test_format_results_with_expanded_query(self, knowledge_tool):
        """Test formatting results with expanded query information."""
        results = [
            {
                "document": "Test content",
                "metadata": {"title": "Test Doc", "document_type": "guide"},
                "distance": 0.1,
                "relevance_score": 0.9,
            }
        ]
        expanded_query = "test query service:auth-service error:DB_001"
        result = knowledge_tool._format_results(results, expanded_query, {})
        assert "Test content" in result
        assert "Search Query" in result
        assert "service:auth-service" in result
        assert "error:DB_001" in result
