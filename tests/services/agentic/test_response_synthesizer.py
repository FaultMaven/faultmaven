"""
Unit tests for ResponseSynthesizer - multi-source response assembly with quality validation.

This module tests the response synthesis system that combines multiple information sources,
applies templates, validates quality, and formats responses for optimal user experience.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio
from datetime import datetime

from faultmaven.services.agentic.response_synthesizer import ResponseSynthesizer
from faultmaven.models.agentic import (
    ProcessingResult, QualityMetrics, ResponseTemplate, ResponseSource,
    SynthesisRequest, SynthesisResult, ContentFormat, QualityAssessment
)


class TestResponseSynthesizer:
    """Test suite for Response Synthesizer."""
    
    @pytest.fixture
    def mock_template_engine(self):
        """Mock template engine for response formatting."""
        mock = AsyncMock()
        mock.render_template.return_value = """
        # Troubleshooting Response
        
        ## Issue Analysis
        {{analysis}}
        
        ## Recommended Solutions
        {{solutions}}
        
        ## Additional Resources
        {{resources}}
        """
        mock.get_template.return_value = ResponseTemplate(
            name='troubleshooting_response',
            format='markdown',
            sections=['analysis', 'solutions', 'resources']
        )
        return mock

    @pytest.fixture
    def mock_quality_checker(self):
        """Mock quality checker for response validation."""
        mock = AsyncMock()
        mock.assess_quality.return_value = QualityAssessment(
            overall_score=0.85,
            completeness_score=0.9,
            relevance_score=0.8,
            clarity_score=0.85,
            actionability_score=0.88,
            issues=[]
        )
        mock.validate_format.return_value = True
        return mock

    @pytest.fixture
    def response_synthesizer(self, mock_template_engine, mock_quality_checker):
        """Create response synthesizer with mocked dependencies."""
        return ResponseSynthesizer(
            template_engine=mock_template_engine,
            quality_checker=mock_quality_checker
        )

    @pytest.mark.asyncio
    async def test_init_response_synthesizer(self, response_synthesizer):
        """Test response synthesizer initialization."""
        assert response_synthesizer.template_engine is not None
        assert response_synthesizer.quality_checker is not None
        assert hasattr(response_synthesizer, 'synthesis_cache')
        assert hasattr(response_synthesizer, 'quality_thresholds')

    @pytest.mark.asyncio
    async def test_synthesize_response_single_source(self, response_synthesizer):
        """Test response synthesis from single source."""
        sources = [
            ResponseSource(
                type='agent_analysis',
                content='System performance issue detected in memory usage',
                confidence=0.9,
                metadata={'analysis_time': '2023-01-01T10:00:00Z'}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            template_name='troubleshooting_response',
            format=ContentFormat.MARKDOWN,
            quality_threshold=0.8
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        assert isinstance(result, ProcessingResult)
        assert result.quality_score >= 0.8
        assert 'memory usage' in result.content

    @pytest.mark.asyncio
    async def test_synthesize_response_multiple_sources(self, response_synthesizer):
        """Test response synthesis from multiple sources."""
        sources = [
            ResponseSource(
                type='agent_analysis',
                content='Performance issue in memory subsystem',
                confidence=0.9,
                metadata={}
            ),
            ResponseSource(
                type='knowledge_base',
                content='Common memory optimization techniques',
                confidence=0.85,
                metadata={'document_id': 'doc123'}
            ),
            ResponseSource(
                type='web_search',
                content='Latest best practices for memory tuning',
                confidence=0.7,
                metadata={'url': 'https://example.com/guide'}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            template_name='comprehensive_response',
            format=ContentFormat.MARKDOWN,
            quality_threshold=0.75
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        # Should integrate all sources
        assert 'memory' in result.content
        assert 'optimization' in result.content
        assert result.quality_score >= 0.75
        
        # Should include source attribution
        assert len(result.metadata.get('sources_used', [])) == 3

    @pytest.mark.asyncio
    async def test_template_application(self, response_synthesizer, mock_template_engine):
        """Test template application during synthesis."""
        sources = [
            ResponseSource(
                type='analysis',
                content='Test analysis content',
                confidence=0.8,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            template_name='structured_response',
            format=ContentFormat.MARKDOWN
        )
        
        await response_synthesizer.synthesize_response(request)
        
        # Verify template engine was called
        mock_template_engine.get_template.assert_called_with('structured_response')
        mock_template_engine.render_template.assert_called()

    @pytest.mark.asyncio
    async def test_quality_validation_pass(self, response_synthesizer, mock_quality_checker):
        """Test quality validation with passing score."""
        sources = [
            ResponseSource(
                type='high_quality_source',
                content='Comprehensive and accurate troubleshooting information',
                confidence=0.95,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            quality_threshold=0.8
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        # Quality should pass threshold
        assert result.quality_score >= 0.8
        mock_quality_checker.assess_quality.assert_called()

    @pytest.mark.asyncio
    async def test_quality_validation_fail(self, response_synthesizer, mock_quality_checker):
        """Test quality validation with failing score."""
        # Set up low quality assessment
        mock_quality_checker.assess_quality.return_value = QualityAssessment(
            overall_score=0.6,  # Below threshold
            completeness_score=0.5,
            relevance_score=0.7,
            clarity_score=0.6,
            actionability_score=0.5,
            issues=['incomplete_analysis', 'low_relevance']
        )
        
        sources = [
            ResponseSource(
                type='low_quality_source',
                content='Brief and unclear response',
                confidence=0.6,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            quality_threshold=0.8
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        # Should indicate quality issues
        assert result.quality_score < 0.8
        assert 'quality_issues' in result.metadata
        assert len(result.metadata['quality_issues']) > 0

    @pytest.mark.asyncio
    async def test_format_conversion(self, response_synthesizer):
        """Test format conversion capabilities."""
        sources = [
            ResponseSource(
                type='text_source',
                content='Test content for format conversion',
                confidence=0.8,
                metadata={}
            )
        ]
        
        # Test different output formats
        formats = [ContentFormat.MARKDOWN, ContentFormat.JSON, ContentFormat.PLAIN_TEXT]
        
        for fmt in formats:
            request = SynthesisRequest(
                sources=sources,
                format=fmt
            )
            
            result = await response_synthesizer.synthesize_response(request)
            
            # Verify format-specific characteristics
            if fmt == ContentFormat.MARKDOWN:
                assert '#' in result.content or '*' in result.content
            elif fmt == ContentFormat.JSON:
                assert '{' in result.content and '}' in result.content
            # Plain text should not have markdown formatting
            elif fmt == ContentFormat.PLAIN_TEXT:
                assert '#' not in result.content.replace('C#', 'CSharp')

    @pytest.mark.asyncio
    async def test_source_confidence_weighting(self, response_synthesizer):
        """Test weighting of sources based on confidence scores."""
        sources = [
            ResponseSource(
                type='high_confidence',
                content='High confidence information about the issue',
                confidence=0.95,
                metadata={}
            ),
            ResponseSource(
                type='low_confidence',
                content='Uncertain information about possible causes',
                confidence=0.4,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(sources=sources)
        result = await response_synthesizer.synthesize_response(request)
        
        # High confidence content should be more prominent
        assert 'High confidence information' in result.content
        # Low confidence content should be de-emphasized or excluded
        
        # Should track confidence weighting in metadata
        assert 'confidence_weighting' in result.metadata

    @pytest.mark.asyncio
    async def test_context_aware_synthesis(self, response_synthesizer):
        """Test context-aware synthesis based on user needs."""
        sources = [
            ResponseSource(
                type='technical_details',
                content='Detailed technical analysis of memory allocation patterns',
                confidence=0.9,
                metadata={}
            ),
            ResponseSource(
                type='simple_explanation',
                content='Your system is using too much memory',
                confidence=0.85,
                metadata={}
            )
        ]
        
        # Test for technical user
        technical_request = SynthesisRequest(
            sources=sources,
            context={'user_level': 'expert', 'detail_preference': 'high'}
        )
        
        technical_result = await response_synthesizer.synthesize_response(technical_request)
        
        # Should include technical details for expert user
        assert 'allocation patterns' in technical_result.content
        
        # Test for non-technical user
        simple_request = SynthesisRequest(
            sources=sources,
            context={'user_level': 'beginner', 'detail_preference': 'low'}
        )
        
        simple_result = await response_synthesizer.synthesize_response(simple_request)
        
        # Should use simpler explanation
        assert 'too much memory' in simple_result.content

    @pytest.mark.asyncio
    async def test_source_attribution(self, response_synthesizer):
        """Test proper attribution of information sources."""
        sources = [
            ResponseSource(
                type='knowledge_base',
                content='Solution from knowledge base',
                confidence=0.9,
                metadata={'document_id': 'kb_doc_123', 'title': 'Memory Troubleshooting'}
            ),
            ResponseSource(
                type='web_search',
                content='External solution reference',
                confidence=0.75,
                metadata={'url': 'https://example.com/solution', 'title': 'Expert Guide'}
            )
        ]
        
        request = SynthesisRequest(sources=sources, include_attribution=True)
        result = await response_synthesizer.synthesize_response(request)
        
        # Should include source attribution
        assert 'sources' in result.metadata
        assert len(result.metadata['sources']) == 2
        assert any('kb_doc_123' in str(source) for source in result.metadata['sources'])

    @pytest.mark.asyncio
    async def test_content_deduplication(self, response_synthesizer):
        """Test deduplication of similar content from multiple sources."""
        sources = [
            ResponseSource(
                type='source1',
                content='Restart the service to resolve the issue',
                confidence=0.9,
                metadata={}
            ),
            ResponseSource(
                type='source2',
                content='Restarting the service should fix the problem',
                confidence=0.85,
                metadata={}
            ),
            ResponseSource(
                type='source3',
                content='Different solution: check configuration files',
                confidence=0.8,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(sources=sources)
        result = await response_synthesizer.synthesize_response(request)
        
        # Should not repeat similar content
        restart_mentions = result.content.lower().count('restart')
        assert restart_mentions <= 2  # Should consolidate similar recommendations

    @pytest.mark.asyncio
    async def test_error_handling_template_failure(self, response_synthesizer, mock_template_engine):
        """Test error handling when template rendering fails."""
        mock_template_engine.render_template.side_effect = Exception("Template error")
        
        sources = [
            ResponseSource(
                type='test_source',
                content='Test content',
                confidence=0.8,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(
            sources=sources,
            template_name='failing_template'
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        # Should fallback to basic formatting
        assert result.content is not None
        assert 'template_error' in result.metadata.get('warnings', [])

    @pytest.mark.asyncio
    async def test_synthesis_caching(self, response_synthesizer):
        """Test caching of synthesis results for performance."""
        sources = [
            ResponseSource(
                type='cached_source',
                content='Content for caching test',
                confidence=0.8,
                metadata={}
            )
        ]
        
        request = SynthesisRequest(sources=sources)
        
        # First synthesis should populate cache
        result1 = await response_synthesizer.synthesize_response(request)
        
        # Second identical synthesis should use cache
        result2 = await response_synthesizer.synthesize_response(request)
        
        assert result1.content == result2.content
        assert result1.quality_score == result2.quality_score

    @pytest.mark.asyncio
    async def test_progressive_quality_improvement(self, response_synthesizer):
        """Test progressive quality improvement through iterations."""
        initial_sources = [
            ResponseSource(
                type='initial',
                content='Basic troubleshooting information',
                confidence=0.7,
                metadata={}
            )
        ]
        
        enhanced_sources = initial_sources + [
            ResponseSource(
                type='enhanced',
                content='Additional detailed analysis and solutions',
                confidence=0.9,
                metadata={}
            )
        ]
        
        initial_request = SynthesisRequest(sources=initial_sources)
        initial_result = await response_synthesizer.synthesize_response(initial_request)
        
        enhanced_request = SynthesisRequest(sources=enhanced_sources)
        enhanced_result = await response_synthesizer.synthesize_response(enhanced_request)
        
        # Enhanced version should have better quality
        assert enhanced_result.quality_score >= initial_result.quality_score
        assert len(enhanced_result.content) > len(initial_result.content)

    def test_quality_threshold_configuration(self, response_synthesizer):
        """Test configuration of quality thresholds."""
        # Test default thresholds
        thresholds = response_synthesizer.get_quality_thresholds()
        assert isinstance(thresholds, dict)
        assert 'completeness' in thresholds
        assert 'relevance' in thresholds
        
        # Test threshold updates
        new_thresholds = {
            'completeness': 0.9,
            'relevance': 0.85,
            'clarity': 0.8
        }
        
        response_synthesizer.update_quality_thresholds(new_thresholds)
        updated_thresholds = response_synthesizer.get_quality_thresholds()
        
        assert updated_thresholds['completeness'] == 0.9
        assert updated_thresholds['relevance'] == 0.85

    def test_template_validation(self, response_synthesizer):
        """Test template validation and error handling."""
        # Valid template
        valid_template = ResponseTemplate(
            name='valid_template',
            format='markdown',
            sections=['introduction', 'analysis', 'conclusion']
        )
        
        assert response_synthesizer.validate_template(valid_template) == True
        
        # Invalid template (missing required sections)
        invalid_template = ResponseTemplate(
            name='invalid_template',
            format='markdown',
            sections=[]  # Empty sections
        )
        
        assert response_synthesizer.validate_template(invalid_template) == False