import pytest
from unittest.mock import Mock, AsyncMock
from faultmaven.agent.tools.knowledge_base import KnowledgeBaseTool


class TestKnowledgeBaseTool:
    """Test suite for KnowledgeBaseTool class."""

    @pytest.fixture
    def sample_documents(self):
        """Sample documents for testing."""
        return [
            {
                'document': 'Database connection timeout troubleshooting guide',
                'metadata': {'source': 'docs/troubleshooting.md', 'type': 'guide'},
                'distance': 0.1,
                'relevance_score': 0.9
            },
            {
                'document': 'How to configure connection pooling',
                'metadata': {'source': 'docs/config.md', 'type': 'config'},
                'distance': 0.2,
                'relevance_score': 0.8
            },
            {
                'document': 'Common database errors and solutions',
                'metadata': {'source': 'docs/errors.md', 'type': 'reference'},
                'distance': 0.3,
                'relevance_score': 0.7
            }
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
        assert 'Database connection timeout' in result
        assert 'connection pooling' in result
        assert 'Common database errors' in result
        assert 'docs/troubleshooting.md' in result
        assert 'docs/config.md' in result
        assert 'docs/errors.md' in result

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
            'document': 'Single troubleshooting guide',
            'metadata': {'source': 'docs/single.md', 'type': 'guide'},
            'distance': 0.1,
            'relevance_score': 0.95
        }
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[single_doc])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Single topic query"
        result = await tool._arun(query)
        assert 'Single troubleshooting guide' in result
        assert 'docs/single.md' in result

    @pytest.mark.asyncio
    async def test_arun_chromadb_error(self):
        """Test handling of KnowledgeIngester errors."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(side_effect=Exception("ChromaDB error"))
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test query"
        result = await tool._arun(query)
        assert "Error searching knowledge base" in result or "Error querying knowledge base" in result

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
        mock_ingester.search = AsyncMock(return_value=[{
            'document': 'Special chars: @#$%^&*()',
            'metadata': {'source': 'docs/special.md'},
            'distance': 0.1,
            'relevance_score': 0.8
        }])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Query with @#$%^&*() characters"
        result = await tool._arun(query)
        assert 'Special chars: @#$%^&*()' in result

    def test_tool_name_and_description(self, knowledge_tool):
        """Test tool name and description."""
        assert knowledge_tool.name == "knowledge_base_search"
        assert "search" in knowledge_tool.description.lower()
        assert "knowledge" in knowledge_tool.description.lower()

    def test_tool_schema(self, knowledge_tool):
        """Test tool input schema."""
        schema = knowledge_tool.get_tool_schema()
        assert schema is not None
        assert 'query' in schema['args_schema']['properties']

    @pytest.mark.asyncio
    async def test_arun_result_formatting(self):
        """Test that results are properly formatted."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[
            {
                'document': 'Document 1',
                'metadata': {'source': 'source1.md', 'type': 'guide', 'tags': 'tag1,tag2', 'document_type': 'guide', 'chunk_index': 0, 'total_chunks': 2},
                'distance': 0.1,
                'relevance_score': 0.9
            },
            {
                'document': 'Document 2',
                'metadata': {'source': 'source2.md', 'type': 'reference', 'tags': 'tag3', 'document_type': 'reference', 'chunk_index': 1, 'total_chunks': 2},
                'distance': 0.2,
                'relevance_score': 0.8
            }
        ])
        tool = KnowledgeBaseTool(knowledge_ingester=mock_ingester)
        query = "Test formatting"
        result = await tool._arun(query)
        assert 'Document 1' in result
        assert 'Document 2' in result
        assert 'source1.md' in result
        assert 'source2.md' in result
        assert 'guide' in result
        assert 'reference' in result
        assert 'tag1' in result
        assert 'tag3' in result
        assert 'Chunk 1 of 2' in result
        assert 'Chunk 2 of 2' in result

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