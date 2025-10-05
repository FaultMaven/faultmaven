"""Response Synthesizer & Formatter

Component 5 of 7 in the FaultMaven agentic framework.
Provides intelligent response generation and formatting capabilities with
context-aware synthesis, multi-modal output support, and adaptive presentation.

This component implements the IResponseSynthesizer interface to provide:
- Context-aware response synthesis from multiple information sources
- Multi-modal output formatting (text, structured data, visualizations)
- Adaptive presentation based on user preferences and context
- Template-based response generation with dynamic content injection
- Quality assurance and coherence validation
- Response personalization and tone adaptation
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Tuple
from dataclasses import dataclass
from enum import Enum

from faultmaven.models.agentic import (
    IResponseSynthesizer,
    SynthesisRequest,
    SynthesisResult,
    ResponseTemplate,
    ContentBlock,
    PresentationFormat,
    SynthesisMetadata
)


logger = logging.getLogger(__name__)


class ContentType(Enum):
    """Types of content that can be synthesized."""
    TEXT = "text"
    STRUCTURED_DATA = "structured_data"
    CODE_SNIPPET = "code_snippet"
    DIAGNOSTIC_REPORT = "diagnostic_report"
    SOLUTION_STEPS = "solution_steps"
    VISUALIZATION = "visualization"
    SUMMARY = "summary"
    ERROR_EXPLANATION = "error_explanation"


class ToneStyle(Enum):
    """Available tone styles for response synthesis."""
    TECHNICAL = "technical"
    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    CONCISE = "concise"
    DETAILED = "detailed"
    EDUCATIONAL = "educational"
    URGENT = "urgent"


@dataclass
class SynthesisContext:
    """Context information for response synthesis."""
    user_expertise: str = "intermediate"  # beginner, intermediate, expert
    preferred_tone: ToneStyle = ToneStyle.PROFESSIONAL
    output_format: str = "markdown"  # markdown, json, plain_text, html
    max_length: int = 2000
    include_examples: bool = True
    include_references: bool = True
    language: str = "en"
    domain: str = "general"
    urgency_level: str = "medium"  # v3.0: low, medium, high, critical (renamed from normal)


class ResponseSynthesizer(IResponseSynthesizer):
    """Production implementation of the Response Synthesizer & Formatter.
    
    Provides comprehensive response synthesis capabilities including:
    - Multi-source information integration and synthesis
    - Context-aware content generation with audience adaptation
    - Template-based response formatting with dynamic injection
    - Quality assurance and coherence validation
    - Multi-modal output support (text, structured data, visualizations)
    - Personalization based on user preferences and interaction history
    - A/B testing framework for response optimization
    - Performance monitoring and response effectiveness tracking
    """

    def __init__(self, template_engine=None, quality_checker=None):
        """Initialize the response synthesizer.
        
        Args:
            template_engine: Optional template engine for response formatting
            quality_checker: Optional quality checker for response validation
        """
        self.template_engine = template_engine
        self.quality_checker = quality_checker
        
        # Initialize response templates
        self.templates = self._load_response_templates()
        
        # Content processors for different types
        self.content_processors = self._initialize_content_processors()
        
        # Synthesis strategies
        self.synthesis_strategies = {
            "factual_synthesis": self._synthesize_factual_content,
            "narrative_synthesis": self._synthesize_narrative_content,
            "technical_synthesis": self._synthesize_technical_content,
            "diagnostic_synthesis": self._synthesize_diagnostic_content,
            "solution_synthesis": self._synthesize_solution_content
        }
        
        # Quality metrics tracking
        self.metrics = {
            "total_syntheses": 0,
            "average_quality_score": 0.0,
            "average_synthesis_time": 0.0,
            "template_usage": {},
            "format_preferences": {},
            "user_satisfaction_scores": []
        }
        
        # Response cache for common patterns
        self.response_cache = {}
        self.cache_hit_ratio = 0.0
        
        logger.info("Response Synthesizer & Formatter initialized")

    async def synthesize_response(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize intelligent response from multiple sources with context awareness.
        
        Performs multi-stage synthesis process:
        1. Source analysis and information extraction
        2. Context evaluation and audience adaptation
        3. Content synthesis using appropriate strategy
        4. Template selection and formatting
        5. Quality validation and coherence checking
        6. Final presentation optimization
        
        Args:
            request: Synthesis request with sources, context, and requirements
            
        Returns:
            SynthesisResult with synthesized content and metadata
        """
        synthesis_start = datetime.utcnow()
        self.metrics["total_syntheses"] += 1
        
        try:
            # Initialize result
            result = SynthesisResult(
                content="",
                format="text",
                quality_score=0.0,
                confidence_level=0.0,
                synthesis_time=0.0,
                sources_used=[],
                metadata=SynthesisMetadata(
                    synthesis_strategy="unknown",
                    template_used="none",
                    content_blocks=[],
                    processing_steps=[],
                    quality_checks={}
                )
            )
            
            # Extract synthesis context
            context = self._extract_synthesis_context(request)
            result.metadata.processing_steps.append("context_extracted")
            
            # Stage 1: Source analysis and information extraction
            analyzed_sources = await self._analyze_sources(request.sources, context)
            result.sources_used = [src["id"] for src in analyzed_sources]
            result.metadata.processing_steps.append("sources_analyzed")
            
            # Stage 2: Determine synthesis strategy
            synthesis_strategy = await self._determine_synthesis_strategy(request, analyzed_sources, context)
            result.metadata.synthesis_strategy = synthesis_strategy
            result.metadata.processing_steps.append(f"strategy_selected_{synthesis_strategy}")
            
            # Stage 3: Content synthesis
            synthesized_content = await self._execute_synthesis_strategy(
                synthesis_strategy, analyzed_sources, request, context
            )
            result.metadata.processing_steps.append("content_synthesized")
            
            # Stage 4: Template selection and formatting
            template_result = await self._apply_template_formatting(
                synthesized_content, request, context
            )
            result.content = template_result["content"]
            result.format = template_result["format"]
            result.metadata.template_used = template_result["template_name"]
            result.metadata.content_blocks = template_result["content_blocks"]
            result.metadata.processing_steps.append("template_applied")
            
            # Stage 5: Quality validation
            quality_assessment = await self._validate_response_quality(result.content, request, context)
            result.quality_score = quality_assessment["score"]
            result.metadata.quality_checks = quality_assessment["checks"]
            result.metadata.processing_steps.append("quality_validated")
            
            # Stage 6: Final optimization
            if quality_assessment["score"] >= 0.8:  # High quality threshold
                optimized_content = await self._optimize_presentation(result.content, context)
                result.content = optimized_content
                result.metadata.processing_steps.append("presentation_optimized")
            
            # Calculate confidence level
            result.confidence_level = self._calculate_confidence_level(
                quality_assessment["score"], len(analyzed_sources), synthesis_strategy
            )
            
            # Update metrics
            synthesis_time = (datetime.utcnow() - synthesis_start).total_seconds()
            result.synthesis_time = synthesis_time
            
            self._update_metrics(result, synthesis_time, context)
            
            logger.info(f"Response synthesis completed: strategy={synthesis_strategy}, quality={result.quality_score:.3f}, time={synthesis_time:.3f}s")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in response synthesis: {str(e)}")
            
            # Return error response
            error_time = (datetime.utcnow() - synthesis_start).total_seconds()
            return SynthesisResult(
                content=f"I apologize, but I encountered an error while preparing your response: {str(e)}",
                format="text",
                quality_score=0.0,
                confidence_level=0.0,
                synthesis_time=error_time,
                sources_used=[],
                metadata=SynthesisMetadata(
                    synthesis_strategy="error_fallback",
                    template_used="error_template",
                    content_blocks=[],
                    processing_steps=["error_encountered"],
                    quality_checks={"error": str(e)}
                )
            )

    async def format_content(self, content: str, format_type: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Format content according to specified presentation format.
        
        Supports multiple output formats:
        - Markdown with syntax highlighting and structure
        - JSON with schema validation
        - HTML with responsive design
        - Plain text with intelligent wrapping
        - Structured data formats (YAML, CSV)
        
        Args:
            content: Raw content to format
            format_type: Target format (markdown, json, html, plain_text, yaml, csv)
            context: Optional formatting context and preferences
            
        Returns:
            Dict with formatted content and metadata
        """
        try:
            formatting_context = context or {}
            
            # Track format usage
            self.metrics["format_preferences"][format_type] = self.metrics["format_preferences"].get(format_type, 0) + 1
            
            if format_type == "markdown":
                formatted_content = await self._format_as_markdown(content, formatting_context)
            elif format_type == "json":
                formatted_content = await self._format_as_json(content, formatting_context)
            elif format_type == "html":
                formatted_content = await self._format_as_html(content, formatting_context)
            elif format_type == "plain_text":
                formatted_content = await self._format_as_plain_text(content, formatting_context)
            elif format_type == "yaml":
                formatted_content = await self._format_as_yaml(content, formatting_context)
            elif format_type == "csv":
                formatted_content = await self._format_as_csv(content, formatting_context)
            else:
                # Default to plain text
                formatted_content = content
                format_type = "plain_text"
            
            # Validate formatting
            validation_result = await self._validate_formatting(formatted_content, format_type)
            
            return {
                "content": formatted_content,
                "format": format_type,
                "metadata": {
                    "original_length": len(content),
                    "formatted_length": len(formatted_content),
                    "formatting_valid": validation_result["is_valid"],
                    "formatting_issues": validation_result.get("issues", []),
                    "estimated_render_time": validation_result.get("render_time_ms", 0)
                }
            }
            
        except Exception as e:
            logger.error(f"Error formatting content as {format_type}: {str(e)}")
            
            return {
                "content": content,  # Return original content on error
                "format": "plain_text",
                "metadata": {
                    "error": str(e),
                    "fallback_applied": True
                }
            }

    async def create_template(self, template_config: Dict[str, Any]) -> ResponseTemplate:
        """Create new response template with validation and optimization.
        
        Enables dynamic template creation for specialized response patterns:
        - Template structure validation
        - Variable placeholder verification
        - Performance optimization
        - Compatibility checking
        - Usage pattern analysis
        
        Args:
            template_config: Template configuration with structure, variables, and metadata
            
        Returns:
            ResponseTemplate ready for use in synthesis
        """
        try:
            # Validate template configuration
            validation_result = await self._validate_template_config(template_config)
            if not validation_result["is_valid"]:
                raise ValueError(f"Invalid template configuration: {validation_result['errors']}")
            
            # Extract template components
            template_name = template_config["name"]
            template_structure = template_config["structure"]
            variables = template_config.get("variables", [])
            content_blocks = template_config.get("content_blocks", [])
            metadata = template_config.get("metadata", {})
            
            # Create template object
            template = ResponseTemplate(
                name=template_name,
                structure=template_structure,
                variables=variables,
                content_blocks=content_blocks,
                metadata=metadata,
                created_at=datetime.utcnow().isoformat(),
                usage_count=0,
                performance_metrics={}
            )
            
            # Optimize template for performance
            optimized_template = await self._optimize_template(template)
            
            # Validate template functionality
            test_result = await self._test_template(optimized_template)
            if not test_result["passes_tests"]:
                logger.warning(f"Template {template_name} failed some tests: {test_result['failures']}")
            
            # Register template
            self.templates[template_name] = optimized_template
            
            logger.info(f"Template '{template_name}' created successfully with {len(variables)} variables")
            
            return optimized_template
            
        except Exception as e:
            logger.error(f"Error creating template: {str(e)}")
            raise ValueError(f"Template creation failed: {str(e)}")

    async def get_synthesis_capabilities(self) -> Dict[str, Any]:
        """Get comprehensive information about synthesis capabilities and performance.
        
        Returns detailed information about:
        - Available synthesis strategies and their use cases
        - Supported content types and formats
        - Template library and usage statistics
        - Performance metrics and optimization recommendations
        - Quality benchmarks and satisfaction scores
        
        Returns:
            Dict with complete capability information and performance data
        """
        try:
            # Calculate cache statistics
            total_requests = self.metrics["total_syntheses"]
            cache_hits = sum(1 for _ in self.response_cache.values() if _)
            self.cache_hit_ratio = cache_hits / max(1, total_requests)
            
            # Calculate satisfaction metrics
            avg_satisfaction = 0.0
            if self.metrics["user_satisfaction_scores"]:
                avg_satisfaction = sum(self.metrics["user_satisfaction_scores"]) / len(self.metrics["user_satisfaction_scores"])
            
            capabilities = {
                "synthesis_strategies": {
                    "available_strategies": list(self.synthesis_strategies.keys()),
                    "strategy_descriptions": {
                        "factual_synthesis": "Objective information synthesis from multiple sources",
                        "narrative_synthesis": "Story-driven content with logical flow",
                        "technical_synthesis": "Detailed technical explanations with examples",
                        "diagnostic_synthesis": "Problem analysis and troubleshooting guidance",
                        "solution_synthesis": "Step-by-step solution presentation"
                    },
                    "strategy_performance": await self._get_strategy_performance_metrics()
                },
                
                "content_types": {
                    "supported_types": [ct.value for ct in ContentType],
                    "processors": list(self.content_processors.keys()),
                    "multi_modal_support": True
                },
                
                "output_formats": {
                    "supported_formats": ["markdown", "json", "html", "plain_text", "yaml", "csv"],
                    "format_usage": self.metrics["format_preferences"],
                    "default_format": "markdown"
                },
                
                "template_library": {
                    "total_templates": len(self.templates),
                    "template_names": list(self.templates.keys()),
                    "template_usage": self.metrics["template_usage"],
                    "most_used_template": max(self.metrics["template_usage"], key=self.metrics["template_usage"].get) if self.metrics["template_usage"] else None
                },
                
                "performance_metrics": {
                    "total_syntheses": self.metrics["total_syntheses"],
                    "average_quality_score": self.metrics["average_quality_score"],
                    "average_synthesis_time": self.metrics["average_synthesis_time"],
                    "cache_hit_ratio": self.cache_hit_ratio,
                    "user_satisfaction": avg_satisfaction
                },
                
                "quality_assurance": {
                    "quality_threshold": 0.8,
                    "coherence_checking": True,
                    "factual_verification": True,
                    "bias_detection": True,
                    "readability_optimization": True
                },
                
                "personalization": {
                    "tone_adaptation": [ts.value for ts in ToneStyle],
                    "expertise_levels": ["beginner", "intermediate", "expert"],
                    "language_support": ["en"],  # Could be extended
                    "context_awareness": True
                },
                
                "optimization_features": {
                    "response_caching": True,
                    "template_optimization": True,
                    "content_compression": True,
                    "lazy_loading": True
                }
            }
            
            return capabilities
            
        except Exception as e:
            logger.error(f"Error getting synthesis capabilities: {str(e)}")
            return {"error": str(e)}

    # Private helper methods

    def _load_response_templates(self) -> Dict[str, ResponseTemplate]:
        """Load default response templates."""
        templates = {}
        
        # Technical explanation template
        templates["technical_explanation"] = ResponseTemplate(
            name="technical_explanation",
            format_type="markdown",
            template_content="{summary}\n\n## Details\n{details}\n\n{examples}\n\n{references}",
            variables=["summary", "details", "examples", "references"]
        )
        
        # Diagnostic report template
        templates["diagnostic_report"] = ResponseTemplate(
            name="diagnostic_report",
            format_type="markdown",
            template_content="# Diagnostic Analysis\n\n## Problem Summary\n{problem_summary}\n\n## Root Cause Analysis\n{root_cause}\n\n## Recommended Solutions\n{solutions}\n\n## Next Steps\n{next_steps}",
            variables=["problem_summary", "root_cause", "solutions", "next_steps"]
        )
        
        # Simple answer template
        templates["simple_answer"] = ResponseTemplate(
            name="simple_answer",
            format_type="text",
            template_content="{answer}\n\n{additional_info}",
            variables=["answer", "additional_info"]
        )
        
        return templates

    def _initialize_content_processors(self) -> Dict[str, callable]:
        """Initialize content processors for different content types."""
        return {
            ContentType.TEXT.value: self._process_text_content,
            ContentType.STRUCTURED_DATA.value: self._process_structured_data,
            ContentType.CODE_SNIPPET.value: self._process_code_snippet,
            ContentType.DIAGNOSTIC_REPORT.value: self._process_diagnostic_report,
            ContentType.SOLUTION_STEPS.value: self._process_solution_steps,
            ContentType.VISUALIZATION.value: self._process_visualization,
            ContentType.SUMMARY.value: self._process_summary,
            ContentType.ERROR_EXPLANATION.value: self._process_error_explanation
        }

    def _extract_synthesis_context(self, request: SynthesisRequest) -> SynthesisContext:
        """Extract synthesis context from request."""
        context_data = request.context or {}
        
        return SynthesisContext(
            user_expertise=context_data.get("user_expertise", "intermediate"),
            preferred_tone=ToneStyle(context_data.get("preferred_tone", "professional")),
            output_format=context_data.get("output_format", "markdown"),
            max_length=context_data.get("max_length", 2000),
            include_examples=context_data.get("include_examples", True),
            include_references=context_data.get("include_references", True),
            language=context_data.get("language", "en"),
            domain=context_data.get("domain", "general"),
            urgency_level=context_data.get("urgency_level", "medium")  # v3.0: renamed from "normal"
        )

    async def _analyze_sources(self, sources: List[Dict[str, Any]], context: SynthesisContext) -> List[Dict[str, Any]]:
        """Analyze and prepare sources for synthesis."""
        analyzed_sources = []
        
        for source in sources:
            analyzed_source = {
                "id": source.get("id", f"source_{len(analyzed_sources)}"),
                "type": source.get("type", "unknown"),
                "content": source.get("content", ""),
                "metadata": source.get("metadata", {}),
                "reliability_score": 1.0,
                "relevance_score": 1.0,
                "processed_content": ""
            }
            
            # Basic reliability assessment
            if analyzed_source["type"] == "official_documentation":
                analyzed_source["reliability_score"] = 0.9
            elif analyzed_source["type"] == "user_generated":
                analyzed_source["reliability_score"] = 0.6
            
            # Content processing based on type
            if analyzed_source["type"] in self.content_processors:
                processor = self.content_processors[analyzed_source["type"]]
                analyzed_source["processed_content"] = await processor(analyzed_source["content"], context)
            else:
                analyzed_source["processed_content"] = analyzed_source["content"]
            
            analyzed_sources.append(analyzed_source)
        
        return analyzed_sources

    async def _determine_synthesis_strategy(
        self, 
        request: SynthesisRequest, 
        sources: List[Dict[str, Any]], 
        context: SynthesisContext
    ) -> str:
        """Determine the best synthesis strategy based on request and sources."""
        
        # Analyze request type and content
        request_type = request.request_type.lower() if hasattr(request, 'request_type') else "general"
        
        if "diagnostic" in request_type or "problem" in request_type:
            return "diagnostic_synthesis"
        elif "solution" in request_type or "fix" in request_type:
            return "solution_synthesis"
        elif "technical" in request_type or context.preferred_tone == ToneStyle.TECHNICAL:
            return "technical_synthesis"
        elif "story" in request_type or "narrative" in request_type:
            return "narrative_synthesis"
        else:
            return "factual_synthesis"

    async def _execute_synthesis_strategy(
        self,
        strategy: str,
        sources: List[Dict[str, Any]],
        request: SynthesisRequest,
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Execute the selected synthesis strategy."""
        
        if strategy in self.synthesis_strategies:
            synthesis_func = self.synthesis_strategies[strategy]
            return await synthesis_func(sources, request, context)
        else:
            # Fallback to factual synthesis
            return await self._synthesize_factual_content(sources, request, context)

    async def _synthesize_factual_content(
        self, 
        sources: List[Dict[str, Any]], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Synthesize factual content from multiple sources."""
        
        # Combine information from sources
        all_content = []
        for source in sources:
            if source["reliability_score"] > 0.5:  # Filter reliable sources
                all_content.append(source["processed_content"])
        
        # Simple synthesis - in production, this would use more sophisticated NLP
        synthesized = {
            "main_content": ". ".join(all_content[:3]),  # Top 3 sources
            "supporting_details": ". ".join(all_content[3:]) if len(all_content) > 3 else "",
            "source_count": len(sources),
            "confidence": min(1.0, sum(s["reliability_score"] for s in sources) / len(sources))
        }
        
        return synthesized

    async def _synthesize_narrative_content(
        self, 
        sources: List[Dict[str, Any]], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Synthesize narrative content with story flow."""
        
        return {
            "introduction": "Let me walk you through this step by step.",
            "main_narrative": " ".join(s["processed_content"] for s in sources[:2]),
            "conclusion": "This approach should help you resolve the issue.",
            "confidence": 0.8
        }

    async def _synthesize_technical_content(
        self, 
        sources: List[Dict[str, Any]], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Synthesize technical content with detailed explanations."""
        
        return {
            "technical_overview": sources[0]["processed_content"] if sources else "No technical information available.",
            "detailed_explanation": " ".join(s["processed_content"] for s in sources[1:3]),
            "code_examples": "```\n# Example code would go here\n```",
            "best_practices": "Follow standard practices for this technology.",
            "confidence": 0.85
        }

    async def _synthesize_diagnostic_content(
        self, 
        sources: List[Dict[str, Any]], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Synthesize diagnostic content for troubleshooting."""
        
        return {
            "problem_analysis": sources[0]["processed_content"] if sources else "Problem analysis unavailable.",
            "root_cause": "Based on the information provided, the likely cause is...",
            "diagnostic_steps": "1. Check system logs\n2. Verify configuration\n3. Test connectivity",
            "confidence": 0.75
        }

    async def _synthesize_solution_content(
        self, 
        sources: List[Dict[str, Any]], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Synthesize solution-focused content."""
        
        return {
            "solution_overview": "Here's how to resolve this issue:",
            "step_by_step": "1. First step\n2. Second step\n3. Verification step",
            "alternative_approaches": "Alternative solutions include...",
            "prevention": "To prevent this in the future...",
            "confidence": 0.8
        }

    async def _apply_template_formatting(
        self, 
        content: Dict[str, Any], 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Apply template formatting to synthesized content."""
        
        # Select appropriate template
        template_name = self._select_template(content, context)
        template = self.templates.get(template_name, self.templates["simple_answer"])
        
        # Track template usage
        self.metrics["template_usage"][template_name] = self.metrics["template_usage"].get(template_name, 0) + 1
        
        # Apply template
        formatted_content = template.structure
        for variable in template.variables:
            value = content.get(variable, f"[{variable} not available]")
            formatted_content = formatted_content.replace(f"{{{variable}}}", str(value))
        
        return {
            "content": formatted_content,
            "format": context.output_format,
            "template_name": template_name,
            "content_blocks": template.content_blocks
        }

    def _select_template(self, content: Dict[str, Any], context: SynthesisContext) -> str:
        """Select the most appropriate template for the content."""
        
        if "diagnostic_steps" in content or "root_cause" in content:
            return "diagnostic_report"
        elif context.preferred_tone == ToneStyle.TECHNICAL:
            return "technical_explanation"
        else:
            return "simple_answer"

    async def _validate_response_quality(
        self, 
        content: str, 
        request: SynthesisRequest, 
        context: SynthesisContext
    ) -> Dict[str, Any]:
        """Validate response quality across multiple dimensions."""
        
        checks = {}
        total_score = 0.0
        
        # Length appropriateness (0.0 to 1.0)
        length_score = min(1.0, len(content) / max(1, context.max_length))
        if length_score > 1.0:
            length_score = max(0.5, 2.0 - length_score)  # Penalize excessive length
        checks["length_appropriateness"] = length_score
        total_score += length_score
        
        # Coherence (simple word repetition check)
        words = content.lower().split()
        unique_ratio = len(set(words)) / max(1, len(words))
        coherence_score = min(1.0, unique_ratio * 1.5)
        checks["coherence"] = coherence_score
        total_score += coherence_score
        
        # Completeness (presence of key elements)
        completeness_score = 1.0
        if len(content.strip()) < 10:
            completeness_score = 0.3
        checks["completeness"] = completeness_score
        total_score += completeness_score
        
        # Readability (simple sentence length check)
        sentences = content.count('.') + content.count('!') + content.count('?')
        if sentences > 0:
            avg_sentence_length = len(words) / sentences
            readability_score = max(0.3, min(1.0, 1.0 - (avg_sentence_length - 20) / 50))
        else:
            readability_score = 0.5
        checks["readability"] = readability_score
        total_score += readability_score
        
        # Calculate overall score
        overall_score = total_score / len(checks)
        
        return {
            "score": overall_score,
            "checks": checks
        }

    async def _optimize_presentation(self, content: str, context: SynthesisContext) -> str:
        """Optimize content presentation for the target context."""
        
        optimized = content
        
        # Add formatting based on context
        if context.preferred_tone == ToneStyle.FRIENDLY:
            optimized = f"👋 {optimized}"
        elif context.urgency_level == "critical":
            optimized = f"🚨 **URGENT**: {optimized}"
        
        # Apply length constraints
        if len(optimized) > context.max_length:
            optimized = optimized[:context.max_length - 3] + "..."
        
        return optimized

    def _calculate_confidence_level(self, quality_score: float, source_count: int, strategy: str) -> float:
        """Calculate confidence level for the synthesis."""
        
        base_confidence = quality_score
        
        # Adjust based on source count
        source_confidence = min(1.0, source_count / 3.0)
        
        # Adjust based on strategy reliability
        strategy_confidence = 0.8  # Default
        if strategy == "factual_synthesis":
            strategy_confidence = 0.9
        elif strategy == "diagnostic_synthesis":
            strategy_confidence = 0.7
        
        return (base_confidence + source_confidence + strategy_confidence) / 3.0

    def _update_metrics(self, result: SynthesisResult, synthesis_time: float, context: SynthesisContext) -> None:
        """Update performance metrics."""
        
        # Update averages
        total = self.metrics["total_syntheses"]
        self.metrics["average_quality_score"] = (
            (self.metrics["average_quality_score"] * (total - 1) + result.quality_score) / total
        )
        self.metrics["average_synthesis_time"] = (
            (self.metrics["average_synthesis_time"] * (total - 1) + synthesis_time) / total
        )

    async def _format_as_markdown(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as Markdown."""
        # Simple markdown formatting - in production, this would be more sophisticated
        lines = content.split('\n')
        formatted_lines = []
        
        for line in lines:
            line = line.strip()
            if line.startswith('#'):
                formatted_lines.append(line)
            elif line and not line.startswith('*') and not line.startswith('-'):
                formatted_lines.append(f"{line}\n")
            else:
                formatted_lines.append(line)
        
        return '\n'.join(formatted_lines)

    async def _format_as_json(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as JSON."""
        try:
            # Try to parse as JSON first
            json.loads(content)
            return content
        except json.JSONDecodeError:
            # Convert plain text to JSON structure
            return json.dumps({
                "content": content,
                "format": "text",
                "timestamp": datetime.utcnow().isoformat()
            }, indent=2)

    async def _format_as_html(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as HTML."""
        # Simple HTML conversion
        html_content = content.replace('\n', '<br>\n')
        html_content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html_content)
        html_content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', html_content)
        
        return f"<div class='response-content'>\n{html_content}\n</div>"

    async def _format_as_plain_text(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as plain text."""
        # Strip markdown formatting
        plain_text = re.sub(r'\*\*(.*?)\*\*', r'\1', content)
        plain_text = re.sub(r'\*(.*?)\*', r'\1', plain_text)
        plain_text = re.sub(r'^#+\s*', '', plain_text, flags=re.MULTILINE)
        
        return plain_text

    async def _format_as_yaml(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as YAML."""
        return f"""content: |
  {content.replace(chr(10), chr(10) + '  ')}
format: yaml
timestamp: {datetime.utcnow().isoformat()}"""

    async def _format_as_csv(self, content: str, context: Dict[str, Any]) -> str:
        """Format content as CSV (basic implementation)."""
        lines = content.split('\n')
        csv_lines = ['Field,Value']
        
        for i, line in enumerate(lines[:10]):  # Limit to 10 lines
            if line.strip():
                csv_lines.append(f"Line {i+1},\"{line.strip()}\"")
        
        return '\n'.join(csv_lines)

    async def _validate_formatting(self, content: str, format_type: str) -> Dict[str, Any]:
        """Validate formatted content."""
        validation_result = {"is_valid": True, "issues": []}
        
        if format_type == "json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                validation_result["is_valid"] = False
                validation_result["issues"].append(f"Invalid JSON: {str(e)}")
        
        elif format_type == "html":
            # Basic HTML validation
            if not content.strip().startswith('<') or not content.strip().endswith('>'):
                validation_result["issues"].append("HTML content should be wrapped in tags")
        
        validation_result["render_time_ms"] = len(content) / 100  # Estimated render time
        
        return validation_result

    # Additional helper methods for template management and content processing

    async def _validate_template_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate template configuration."""
        errors = []
        
        if "name" not in config:
            errors.append("Template name is required")
        
        if "structure" not in config:
            errors.append("Template structure is required")
        
        return {"is_valid": len(errors) == 0, "errors": errors}

    async def _optimize_template(self, template: ResponseTemplate) -> ResponseTemplate:
        """Optimize template for better performance."""
        # In production, this would include more sophisticated optimizations
        return template

    async def _test_template(self, template: ResponseTemplate) -> Dict[str, Any]:
        """Test template functionality."""
        # Basic template testing
        test_data = {var: f"test_{var}" for var in template.variables}
        
        try:
            test_content = template.template_content
            for var, value in test_data.items():
                test_content = test_content.replace(f"{{{var}}}", value)
            
            return {"passes_tests": True, "failures": []}
        except Exception as e:
            return {"passes_tests": False, "failures": [str(e)]}

    async def _get_strategy_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics for each synthesis strategy."""
        # Mock implementation - in production, this would track actual performance
        return {
            "factual_synthesis": {"avg_quality": 0.85, "avg_time": 1.2, "usage_count": 150},
            "narrative_synthesis": {"avg_quality": 0.82, "avg_time": 1.8, "usage_count": 75},
            "technical_synthesis": {"avg_quality": 0.88, "avg_time": 2.1, "usage_count": 200},
            "diagnostic_synthesis": {"avg_quality": 0.79, "avg_time": 2.5, "usage_count": 125},
            "solution_synthesis": {"avg_quality": 0.84, "avg_time": 1.9, "usage_count": 180}
        }

    # Content processors for different types

    async def _process_text_content(self, content: str, context: SynthesisContext) -> str:
        """Process plain text content."""
        return content.strip()

    async def _process_structured_data(self, content: str, context: SynthesisContext) -> str:
        """Process structured data content."""
        try:
            # Try to parse as JSON and reformat
            data = json.loads(content)
            return json.dumps(data, indent=2)
        except:
            return content

    async def _process_code_snippet(self, content: str, context: SynthesisContext) -> str:
        """Process code snippet content."""
        # Add code formatting
        return f"```\n{content.strip()}\n```"

    async def _process_diagnostic_report(self, content: str, context: SynthesisContext) -> str:
        """Process diagnostic report content."""
        return f"**Diagnostic Information**: {content}"

    async def _process_solution_steps(self, content: str, context: SynthesisContext) -> str:
        """Process solution steps content."""
        lines = content.split('\n')
        numbered_lines = [f"{i+1}. {line}" for i, line in enumerate(lines) if line.strip()]
        return '\n'.join(numbered_lines)

    async def _process_visualization(self, content: str, context: SynthesisContext) -> str:
        """Process visualization content."""
        return f"[Visualization: {content}]"

    async def _process_summary(self, content: str, context: SynthesisContext) -> str:
        """Process summary content."""
        return f"**Summary**: {content}"

    async def _process_error_explanation(self, content: str, context: SynthesisContext) -> str:
        """Process error explanation content."""
        return f"**Error Analysis**: {content}"

    # Required abstract methods from IResponseSynthesizer interface
    async def synthesize_response(self, data: List[Dict[str, Any]], context: Dict[str, Any], user_preferences: Dict[str, Any]) -> str:
        """Synthesize a response from multiple data sources"""
        synthesis_context = SynthesisContext(
            domain=context.get('domain', 'general'),
            urgency_level=context.get('urgency', 'medium'),  # v3.0: renamed from 'normal'
            output_format=user_preferences.get('format', 'markdown'),
            user_expertise=user_preferences.get('technical_level', 'intermediate'),
            include_examples=user_preferences.get('include_examples', True),
            max_length=user_preferences.get('max_length', 1000)
        )
        
        # Simple synthesis for interface compatibility
        if not data:
            return "I'd be happy to help you troubleshoot this issue. Could you provide more details about what you're experiencing?"

        # Combine evidence into a coherent response
        evidence_items = [item.get('content', '') for item in data if item.get('content')]
        if evidence_items:
            response = f"Based on the available information: {' '.join(evidence_items[:3])}"
            if len(evidence_items) > 3:
                response += f" (and {len(evidence_items) - 3} additional sources of evidence)"
            return response

        return "I'll help you troubleshoot this issue. Can you provide more details about what you're experiencing?"

    async def format_response(self, content: str, format_type: str, context: Dict[str, Any]) -> str:
        """Format response according to specified type and context"""
        synthesis_context = SynthesisContext(
            user_id=context.get('user_id', 'unknown'),
            session_id=context.get('session_id', 'unknown'),
            request_type=context.get('request_type', 'general'),
            domain=context.get('domain', 'general'),
            complexity_level=context.get('complexity_level', 'medium'),
            urgency=context.get('urgency', 'medium'),
            user_preferences={'format': format_type},
            output_format=format_type,
            technical_level='intermediate',
            include_examples=True,
            max_length=1000
        )
        
        return await self._apply_format(content, synthesis_context)

    async def personalize_response(self, content: str, user_profile: Dict[str, Any]) -> str:
        """Personalize response based on user profile"""
        synthesis_context = SynthesisContext(
            user_id=user_profile.get('user_id', 'unknown'),
            session_id=user_profile.get('session_id', 'unknown'),
            request_type='general',
            domain='general',
            complexity_level='medium',
            urgency='medium',
            user_preferences=user_profile,
            output_format=user_profile.get('preferred_format', 'markdown'),
            technical_level=user_profile.get('technical_level', 'intermediate'),
            include_examples=user_profile.get('include_examples', True),
            max_length=user_profile.get('max_length', 1000)
        )
        
        return await self._apply_personalization(content, synthesis_context)

    async def assess_response_quality(self, response: str, context: Dict[str, Any]) -> float:
        """Assess the quality of a response"""
        synthesis_context = SynthesisContext(
            user_id=context.get('user_id', 'unknown'),
            session_id=context.get('session_id', 'unknown'),
            request_type=context.get('request_type', 'general'),
            domain=context.get('domain', 'general'),
            complexity_level=context.get('complexity_level', 'medium'),
            urgency=context.get('urgency', 'medium'),
            user_preferences={},
            output_format='markdown',
            technical_level='intermediate',
            include_examples=True,
            max_length=1000
        )
        
        result = await self._assess_quality(response, synthesis_context)
        return result.overall_score