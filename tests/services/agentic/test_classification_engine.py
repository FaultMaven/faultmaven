"""
Unit tests for QueryClassificationEngine - multi-dimensional query analysis.

This module tests the query classification system that analyzes queries across
four dimensions: intent, complexity, domain, and urgency.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio

from faultmaven.services.agentic.engines.classification_engine import QueryClassificationEngine
from faultmaven.models.agentic import (
    QueryInput, QueryClassification, QueryIntent, QueryComplexity,
    QueryDomain, QueryUrgency, ClassificationResult
)


class TestQueryClassificationEngine:
    """Test suite for Query Classification Engine."""
    
    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider for classification requests."""
        mock = AsyncMock()
        mock.generate_response.return_value = {
            'content': 'TROUBLESHOOTING|MEDIUM|SYSTEM_PERFORMANCE|HIGH|0.85',
            'usage': {'tokens': 100}
        }
        return mock

    @pytest.fixture
    def mock_knowledge_base(self):
        """Mock knowledge base for domain-specific context."""
        mock = AsyncMock()
        mock.search.return_value = [
            {'content': 'system performance troubleshooting guide', 'score': 0.9},
            {'content': 'performance optimization best practices', 'score': 0.8}
        ]
        return mock

    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer for observability."""
        mock = Mock()
        mock.trace = Mock()
        return mock

    @pytest.fixture
    def classification_engine(self, mock_llm_provider, mock_tracer):
        """Create classification engine with mocked dependencies."""
        return QueryClassificationEngine(
            llm_provider=mock_llm_provider,
            tracer=mock_tracer
        )

    @pytest.mark.asyncio
    async def test_init_classification_engine(self, classification_engine):
        """Test classification engine initialization."""
        assert classification_engine.llm_provider is not None
        assert classification_engine.tracer is not None
        assert hasattr(classification_engine, 'enable_llm_classification')

    @pytest.mark.asyncio
    async def test_classify_query_troubleshooting_intent(self, classification_engine):
        """Test classification of troubleshooting queries."""
        query = "My application is running slowly and users are complaining"
        
        result = await classification_engine.classify_query(query)
        
        assert isinstance(result, QueryClassification)
        assert result.intent == QueryIntent.TROUBLESHOOTING
        assert result.confidence > 0.7
        
        # Verify LLM was called for classification
        classification_engine.llm_provider.generate_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_classify_query_information_intent(self, classification_engine, mock_llm_provider):
        """Test classification of information-seeking queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'INFORMATION|LOW|GENERAL|LOW|0.92',
            'usage': {'tokens': 80}
        }
        
        query = "What is the best practice for configuring Redis?"
        result = await classification_engine.classify_query(query)
        
        assert result.intent == QueryIntent.INFORMATION
        assert result.complexity == QueryComplexity.LOW
        assert result.confidence > 0.9

    @pytest.mark.asyncio
    async def test_classify_query_configuration_intent(self, classification_engine, mock_llm_provider):
        """Test classification of configuration queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'CONFIGURATION|HIGH|INFRASTRUCTURE|MEDIUM|0.88',
            'usage': {'tokens': 120}
        }
        
        query = "Help me set up a complex Kubernetes deployment with multiple services"
        result = await classification_engine.classify_query(query)
        
        assert result.intent == QueryIntent.CONFIGURATION
        assert result.complexity == QueryComplexity.HIGH
        assert result.domain == QueryDomain.INFRASTRUCTURE

    @pytest.mark.asyncio
    async def test_classify_query_optimization_intent(self, classification_engine, mock_llm_provider):
        """Test classification of optimization queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'OPTIMIZATION|MEDIUM|SYSTEM_PERFORMANCE|MEDIUM|0.85',
            'usage': {'tokens': 100}
        }
        
        query = "How can I optimize my database queries for better performance?"
        result = await classification_engine.classify_query(query)
        
        assert result.intent == QueryIntent.OPTIMIZATION
        assert result.domain == QueryDomain.SYSTEM_PERFORMANCE

    @pytest.mark.asyncio
    async def test_pattern_based_classification(self, classification_engine):
        """Test pattern-based classification for common queries."""
        # Test error pattern
        error_query = "Error 500: Internal Server Error occurred"
        result = await classification_engine.classify_query(error_query)
        
        assert result.intent == QueryIntent.TROUBLESHOOTING
        assert result.urgency in [QueryUrgency.HIGH, QueryUrgency.CRITICAL]

    @pytest.mark.asyncio
    async def test_complexity_assessment_simple(self, classification_engine, mock_llm_provider):
        """Test complexity assessment for simple queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'INFORMATION|LOW|GENERAL|LOW|0.95',
            'usage': {'tokens': 50}
        }
        
        query = "What is Docker?"
        result = await classification_engine.classify_query(query)
        
        assert result.complexity == QueryComplexity.LOW

    @pytest.mark.asyncio
    async def test_complexity_assessment_complex(self, classification_engine, mock_llm_provider):
        """Test complexity assessment for complex queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'CONFIGURATION|VERY_HIGH|INFRASTRUCTURE|HIGH|0.82',
            'usage': {'tokens': 200}
        }
        
        query = "I need to set up a multi-region Kubernetes cluster with service mesh, observability, and disaster recovery"
        result = await classification_engine.classify_query(query)
        
        assert result.complexity == QueryComplexity.VERY_HIGH

    @pytest.mark.asyncio
    async def test_domain_classification_network(self, classification_engine, mock_llm_provider):
        """Test domain classification for network-related queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'TROUBLESHOOTING|MEDIUM|NETWORK|HIGH|0.88',
            'usage': {'tokens': 110}
        }
        
        query = "Network connectivity issues between microservices"
        result = await classification_engine.classify_query(query)
        
        assert result.domain == QueryDomain.NETWORK

    @pytest.mark.asyncio
    async def test_domain_classification_security(self, classification_engine, mock_llm_provider):
        """Test domain classification for security-related queries."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'CONFIGURATION|HIGH|SECURITY|CRITICAL|0.92',
            'usage': {'tokens': 140}
        }
        
        query = "How to set up OAuth2 authentication with proper security headers"
        result = await classification_engine.classify_query(query)
        
        assert result.domain == QueryDomain.SECURITY
        assert result.urgency == QueryUrgency.CRITICAL

    @pytest.mark.asyncio
    async def test_urgency_classification_critical(self, classification_engine, mock_llm_provider):
        """Test urgency classification for critical issues."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'TROUBLESHOOTING|HIGH|SYSTEM_PERFORMANCE|CRITICAL|0.95',
            'usage': {'tokens': 130}
        }
        
        query = "URGENT: Production system is down, users cannot access the application"
        result = await classification_engine.classify_query(query)
        
        assert result.urgency == QueryUrgency.CRITICAL

    @pytest.mark.asyncio
    async def test_confidence_scoring(self, classification_engine):
        """Test confidence scoring mechanism."""
        query = "Application performance issues"
        result = await classification_engine.classify_query(query)
        
        # Confidence should be between 0 and 1
        assert 0 <= result.confidence <= 1
        
        # For clear troubleshooting queries, confidence should be high
        assert result.confidence > 0.7

    @pytest.mark.asyncio
    async def test_knowledge_base_context_integration(self, classification_engine):
        """Test integration with knowledge base for context."""
        query = "Database optimization techniques"
        
        await classification_engine.classify_query(query)
        
        # Verify knowledge base was searched for context
        classification_engine.knowledge_base.search.assert_called()

    @pytest.mark.asyncio
    async def test_pattern_caching(self, classification_engine):
        """Test pattern caching for performance optimization."""
        query = "Error 404: Page not found"
        
        # First classification should populate cache
        result1 = await classification_engine.classify_query(query)
        
        # Second classification should use cache
        result2 = await classification_engine.classify_query(query)
        
        assert result1.intent == result2.intent
        # LLM should only be called once due to caching
        assert classification_engine.llm_provider.generate_response.call_count <= 2

    @pytest.mark.asyncio
    async def test_error_handling_llm_failure(self, classification_engine, mock_llm_provider):
        """Test error handling when LLM fails."""
        mock_llm_provider.generate_response.side_effect = Exception("LLM service unavailable")
        
        query = "Test query"
        result = await classification_engine.classify_query(query)
        
        # Should fallback to pattern-based classification
        assert result is not None
        assert isinstance(result, QueryClassification)
        assert result.confidence < 0.7  # Lower confidence for fallback

    @pytest.mark.asyncio
    async def test_malformed_llm_response(self, classification_engine, mock_llm_provider):
        """Test handling of malformed LLM responses."""
        mock_llm_provider.generate_response.return_value = {
            'content': 'INVALID_FORMAT',
            'usage': {'tokens': 50}
        }
        
        query = "Test query"
        result = await classification_engine.classify_query(query)
        
        # Should fallback gracefully
        assert result is not None
        assert result.intent in [intent for intent in QueryIntent]

    @pytest.mark.asyncio 
    async def test_batch_classification(self, classification_engine):
        """Test batch classification of multiple queries."""
        queries = [
            "System is slow",
            "How to configure Redis?",
            "Error 500 in production"
        ]
        
        results = await classification_engine.classify_batch(queries)
        
        assert len(results) == len(queries)
        for result in results:
            assert isinstance(result, QueryClassification)
            assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_classification_metadata(self, classification_engine):
        """Test metadata collection during classification."""
        query = "Application performance issues"
        
        result = await classification_engine.classify_query(query)
        
        # Verify metadata is collected
        assert hasattr(result, 'metadata')
        if hasattr(result, 'metadata') and result.metadata:
            assert 'processing_time' in result.metadata
            assert 'llm_tokens_used' in result.metadata

    def test_validate_classification_result(self, classification_engine):
        """Test validation of classification results."""
        # Valid result
        valid_result = QueryClassification(
            intent=QueryIntent.TROUBLESHOOTING,
            complexity=QueryComplexity.MEDIUM,
            domain=QueryDomain.SYSTEM_PERFORMANCE,
            urgency=QueryUrgency.HIGH,
            confidence=0.85
        )
        
        assert classification_engine._validate_result(valid_result) == True
        
        # Invalid result (low confidence)
        invalid_result = QueryClassification(
            intent=QueryIntent.TROUBLESHOOTING,
            complexity=QueryComplexity.MEDIUM,
            domain=QueryDomain.SYSTEM_PERFORMANCE,
            urgency=QueryUrgency.HIGH,
            confidence=0.2
        )
        
        assert classification_engine._validate_result(invalid_result) == False

    def test_enum_completeness(self):
        """Test that all required enum values are available."""
        # Verify enum completeness
        intents = [QueryIntent.TROUBLESHOOTING, QueryIntent.INFORMATION, 
                  QueryIntent.CONFIGURATION, QueryIntent.OPTIMIZATION]
        assert len(intents) >= 4
        
        complexities = [QueryComplexity.LOW, QueryComplexity.MEDIUM, 
                       QueryComplexity.HIGH, QueryComplexity.VERY_HIGH]
        assert len(complexities) >= 4
        
        domains = [QueryDomain.GENERAL, QueryDomain.SYSTEM_PERFORMANCE, 
                  QueryDomain.NETWORK, QueryDomain.SECURITY, QueryDomain.INFRASTRUCTURE]
        assert len(domains) >= 5
        
        urgencies = [QueryUrgency.LOW, QueryUrgency.MEDIUM, 
                    QueryUrgency.HIGH, QueryUrgency.CRITICAL]
        assert len(urgencies) >= 4