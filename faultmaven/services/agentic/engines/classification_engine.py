"""Query Classification Engine Component

This component serves as the intelligent gateway for query processing and routing,
implementing multi-dimensional classification to determine the optimal processing
strategy for each user query in the agentic framework.

Key responsibilities:
- Parse and normalize user queries
- Classify query intent and complexity 
- Route queries to appropriate workflow paths
- Extract key entities and context
- Assess complexity for sync vs async processing
- Domain identification for specialized handling

The Classification Engine is the first step in the agentic loop, providing the
intelligence needed to route queries to the appropriate processing workflows.
"""

import re
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum

from faultmaven.models.agentic import IQueryClassificationEngine, QueryIntent, QueryClassification
from faultmaven.models.interfaces import ILLMProvider, ITracer
from faultmaven.infrastructure.observability.tracing import trace

logger = logging.getLogger(__name__)


class ConfidenceMetrics:
    """Track confidence-based decisions for optimization and monitoring"""

    def __init__(self):
        """Initialize metrics tracking"""
        self.tier_distribution = {"high": 0, "medium": 0, "low": 0}
        self.llm_classification_calls = 0
        self.llm_classification_skips = 0
        self.confidence_overrides = 0
        self.self_corrections = 0

    def record_classification(self, confidence: float, llm_called: bool):
        """Record classification metrics

        Args:
            confidence: Classification confidence (0.0-1.0)
            llm_called: Whether LLM was called for classification
        """
        # Tier distribution (HIGH ≥0.7, MEDIUM 0.4-0.7, LOW <0.4)
        if confidence >= 0.7:
            self.tier_distribution["high"] += 1
        elif confidence >= 0.4:
            self.tier_distribution["medium"] += 1
        else:
            self.tier_distribution["low"] += 1

        # LLM call tracking
        if llm_called:
            self.llm_classification_calls += 1
        else:
            self.llm_classification_skips += 1

    def record_override(self):
        """Record when confidence-based override occurs (e.g., forced clarification)"""
        self.confidence_overrides += 1

    def record_self_correction(self):
        """Record when LLM performs self-correction in medium confidence tier"""
        self.self_corrections += 1

    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics

        Returns:
            Dict containing tier distribution, LLM call stats, and override counts
        """
        total = sum(self.tier_distribution.values())
        total_llm = self.llm_classification_calls + self.llm_classification_skips

        return {
            "tier_distribution": self.tier_distribution.copy(),
            "tier_percentages": {
                tier: (count / total * 100) if total > 0 else 0
                for tier, count in self.tier_distribution.items()
            },
            "llm_classification": {
                "calls": self.llm_classification_calls,
                "skips": self.llm_classification_skips,
                "skip_rate": (
                    self.llm_classification_skips / total_llm * 100
                    if total_llm > 0
                    else 0
                )
            },
            "confidence_overrides": self.confidence_overrides,
            "self_corrections": self.self_corrections,
            "total_classifications": total
        }

    def reset(self):
        """Reset all metrics to zero"""
        self.tier_distribution = {"high": 0, "medium": 0, "low": 0}
        self.llm_classification_calls = 0
        self.llm_classification_skips = 0
        self.confidence_overrides = 0
        self.self_corrections = 0




class ComplexityLevel(str, Enum):
    """Query complexity levels"""
    SIMPLE = "simple"      # Single-step, well-defined issues
    MODERATE = "moderate"  # Multi-step with clear dependencies
    COMPLEX = "complex"    # Requires investigation and analysis
    EXPERT = "expert"      # Multi-system, high expertise required


class UrgencyLevel(str, Enum):
    """Query urgency levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TechnicalDomain(str, Enum):
    """Technical domains for specialized handling"""
    DATABASE = "database"
    NETWORKING = "networking"
    APPLICATION = "application"
    INFRASTRUCTURE = "infrastructure"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MONITORING = "monitoring"
    DEPLOYMENT = "deployment"
    GENERAL = "general"


class LLMClassificationMode(str, Enum):
    """LLM classification modes for enhanced confidence framework"""
    DISABLED = "disabled"        # Never call LLM, pattern-only always
    FALLBACK = "fallback"        # Call LLM only when patterns fail (confidence=0)
    ENHANCEMENT = "enhancement"  # Call LLM when pattern confidence < threshold (RECOMMENDED)
    ALWAYS = "always"           # Always call LLM (backward compatibility)


class QueryClassificationEngine(IQueryClassificationEngine):
    """Production implementation of query classification using ML and pattern matching"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        tracer: ITracer,
        llm_classification_mode: str = "enhancement",  # Changed from bool to mode
        pattern_confidence_threshold: float = 0.7,  # Threshold for triggering LLM call
        fallback_to_patterns: bool = True
    ):
        """Initialize the query classification engine

        Args:
            llm_provider: LLM provider for semantic classification
            tracer: Observability tracer
            llm_classification_mode: LLM classification mode (disabled/fallback/enhancement/always)
            pattern_confidence_threshold: Confidence threshold for LLM enhancement (0.7 recommended)
            fallback_to_patterns: Whether to fall back to pattern matching
        """
        self.llm_provider = llm_provider
        self.tracer = tracer
        self.llm_classification_mode = LLMClassificationMode(llm_classification_mode)
        self.pattern_confidence_threshold = pattern_confidence_threshold
        self.fallback_to_patterns = fallback_to_patterns

        # Store provider name for same-provider optimization
        self.llm_provider_name = getattr(llm_provider, 'provider_name', None) or getattr(llm_provider, 'name', 'unknown')

        # Initialize pattern matchers (with compiled regex for performance)
        self._init_pattern_matchers()
        self._compile_patterns()

        # Initialize confidence metrics tracking
        self.metrics = ConfidenceMetrics()

        logger.info(
            f"QueryClassificationEngine initialized: mode={self.llm_classification_mode.value}, "
            f"threshold={self.pattern_confidence_threshold}, provider={self.llm_provider_name}"
        )
    
    def _init_pattern_matchers(self):
        """Initialize pattern matching rules for fast classification with weighted patterns"""

        # Intent patterns with weights (0.5 = generic, 1.0 = typical, 2.0 = specific)
        # Format: (pattern, weight)
        self.intent_patterns = {
            QueryIntent.TROUBLESHOOTING: [
                (r'\b(my|our|the) .* (is|are) (broken|crashing|failing|not working)\b', 2.0),  # Specific active problem
                (r'\b(i\'?m|we\'?re) (getting|seeing|experiencing) (error|issue|problem)\b', 2.0),  # Active incident
                (r'\b(troubleshoot|debug|diagnose)\b', 1.5),  # Troubleshooting verbs
                (r'\b(fix|resolve|solve)\b', 1.0),  # Solution-seeking
                (r'\b(issue|problem|error|bug)\b', 0.8),  # Generic problem words
                (r'\b(broken|fails?|failing)\b', 1.0),  # Failure states
                (r'\b(why.*not|what.*wrong|how.*fix)\b', 1.2),  # Problem questions
            ],
            QueryIntent.VALIDATION: [
                # Hypothetical/confirmation questions about specific approaches
                (r'\bthis (won\'?t|will not|can\'?t|cannot) work,? (right|correct)\?\s*$', 2.0),  # Direct hypothesis
                (r'\bso this (won\'?t|will not|should not|shouldn\'?t) work\b', 1.8),  # Hypothesis statement
                (r'\b(would|will|should) this (work|be correct|be valid)\b', 1.5),  # Hypothetical question
                (r'\b(is it (true|correct) that|am i right that)\b', 1.5),  # Seeking validation
                (r'\b(if i|if we).*(will it|would it|should it)\b', 1.3),  # Conditional hypothesis
                (r'\bjust (confirming|checking|verifying) (that|if)\b', 1.5),  # Explicit confirmation
            ],
            QueryIntent.PROCEDURAL: [
                # How-to and capability questions (not about problems, just procedures)
                (r'\bhow (do|can|should) (i|we|you) (do|perform|execute|run|use|create|setup|configure)\b', 2.0),  # Direct how-to
                (r'\b(can|could) (i|we|you) (use|run|execute|do|perform|create)\b', 1.8),  # Capability questions
                (r'\b(is it possible|are we able|am i able) to\b', 1.5),  # Possibility inquiry
                (r'\b(what (is|are) the (way|method|steps|procedure) to)\b', 1.3),  # Method inquiry
                (r'\bhow to (do|perform|execute|run|use|create|setup|configure)\b', 1.8),  # How-to format
                (r'\b(can.*at the same time|can.*simultaneously|can.*together)\b', 1.6),  # Compound actions
            ],
            QueryIntent.STATUS_CHECK: [
                # Merged: MONITORING patterns added here (v3.0)
                (r'\b(status|health|state|running|up|down|available)\b', 1.5),
                (r'\b(check|verify|confirm|validate)\b', 1.0),
                (r'\bis.*working|are.*running\b', 1.8),
                (r'\b(monitor|track|watch|observe|alert)\b', 1.5),  # From MONITORING
                (r'\b(metrics|logs|dashboard|stats)\b', 1.3),  # From MONITORING
                (r'\b(performance|usage|utilization)\b', 1.2),  # From MONITORING
            ],
            QueryIntent.INFORMATION: [
                # Merged: EXPLANATION, DOCUMENTATION patterns added here (v3.0)
                # Meta-queries about methodology/doctrine/processes (not actual problems)
                (r'\b(what (are|is) the (steps|phases|process|methodology|doctrine))\b', 2.0),
                (r'\b(how does .* work)\b', 1.3),
                (r'\b(explain (the|how|what))\b', 1.0),
                (r'\b(tell me about (the|your))\b', 1.5),
                (r'\b(describe (the|how))\b', 1.3),
                (r'\b(what (should|do) (i|we) (do|follow))\b', 1.5),
                # Questions about SRE practices/principles
                (r'\b(sre (doctrine|principles|methodology|practices))\b', 2.0),
                (r'\b(troubleshooting (approach|methodology|steps|process))\b', 1.8),
                # From EXPLANATION
                (r'\b(how does|what is|tell me about)\b', 1.5),
                (r'\b(meaning|definition|purpose|reason)\b', 1.2),
                (r'\bwhy.*\?\s*$', 1.0),
                # From DOCUMENTATION
                (r'\b(documentation|docs|manual|guide|reference)\b', 1.5),
                (r'\b(where (can|do) (i|we) find|where is the (doc|documentation))\b', 1.8),
            ],
            QueryIntent.CONFIGURATION: [
                (r'\b(configure|config|setup|install|deploy)\b', 1.5),
                (r'\b(settings|parameters|options)\b', 1.2),
                (r'\bhow to (set|configure|install)\b', 1.8),
            ],
            QueryIntent.BEST_PRACTICES: [
                # Best practices and recommendations (not current problems)
                (r'\b(best practice|recommended (approach|way|method))\b', 2.0),  # Explicit best practices
                (r'\b(should (i|we) (use|do|follow)|what (is|are) the best)\b', 1.8),  # Seeking recommendations
                (r'\b(proper (way|method|approach)|right (way|method))\b', 1.8),  # Proper methodology
                (r'\b(industry standard|standard practice|common practice)\b', 1.8),  # Industry patterns
                (r'\b(recommendation|guideline|convention)\b', 1.5),  # General guidance
                (r'\b(do\'?s and don\'?ts|pitfall|gotcha|tip)\b', 1.5),  # Practical advice
            ],
            QueryIntent.OPTIMIZATION: [
                # Performance optimization and improvement (not fixing broken systems)
                (r'\b(optimize|optimiz|improve|enhance|speed up|make.*faster)\b', 2.0),  # Optimization verbs
                (r'\b(performance (tuning|improvement|optimization))\b', 2.0),  # Performance focus
                (r'\b(reduce (latency|memory|cpu|load|time))\b', 1.8),  # Reduction goals
                (r'\b(scale|scaling|scalability)\b', 1.5),  # Scalability concerns
                (r'\b(efficiency|efficient|resource usage)\b', 1.3),  # Efficiency focus
                (r'\b(slow|sluggish|lag).*(improve|better|faster)\b', 1.8),  # Slow but seeking improvement
            ],
            QueryIntent.DEPLOYMENT: [
                # Deployment planning and execution (NEW v3.0)
                (r'\b(deploy|deployment|rollout|roll out|release)\b', 2.0),  # Deployment terms
                (r'\b(deploy.*to (production|staging|dev))\b', 2.0),  # Environment-specific deployment
                (r'\b(ci/cd|pipeline|continuous (integration|deployment))\b', 1.8),  # CI/CD context
                (r'\b(kubernetes|k8s|docker|container).*(deploy|run|start)\b', 1.8),  # Container deployment
                (r'\b(helm|terraform|ansible).*(deploy|provision)\b', 1.8),  # Infrastructure as code
                (r'\b(blue.green|canary|rolling).*(deployment|update)\b', 2.0),  # Deployment strategies
                (r'\b(migrate|migration|upgrade).*(to|from)\b', 1.5),  # Migration/upgrade context
            ],
            QueryIntent.VISUALIZATION: [
                # Diagram and visualization requests (NEW v3.0)
                (r'\b(show me|draw|diagram|visualize|chart)\b', 2.0),  # Visualization verbs
                (r'\b(architecture (diagram|overview|map))\b', 2.0),  # Architecture diagrams
                (r'\b(flowchart|flow chart|workflow diagram)\b', 2.0),  # Flowcharts
                (r'\b(data flow|request flow|process flow)\b', 1.8),  # Flow diagrams
                (r'\b(system (design|architecture|layout|structure))\b', 1.8),  # System structure
                (r'\b(how does.*flow|what.*the flow)\b', 1.5),  # Flow questions
                (r'\b(mermaid|graphviz|plantuml)\b', 2.0),  # Explicit diagram tools
            ],
            QueryIntent.COMPARISON: [
                # Feature comparisons and analysis (NEW v3.0)
                (r'\b(compare|comparison|versus|vs\.?|difference between)\b', 2.0),  # Comparison terms
                (r'\b(pros? and cons?|advantages? and disadvantages?)\b', 2.0),  # Pros/cons analysis
                (r'\b(which (is|are) better|what.*better)\b', 1.8),  # Comparative evaluation
                (r'\b(similarities? and differences?)\b', 1.8),  # Similarity/difference analysis
                (r'\b(option a|option b|alternative|choice)\b', 1.5),  # Options/alternatives
                (r'\b(trade.?off|tradeoff)\b', 1.8),  # Trade-off analysis
                (r'\b(feature comparison|capability comparison)\b', 2.0),  # Feature comparison
            ],
            # Conversation intelligence intents
            QueryIntent.OFF_TOPIC: [
                (r'\b(recipe|cooking|food|restaurant|eat|dinner|breakfast|lunch)\b', 1.5),
                (r'\b(weather|forecast|temperature|climate|rain|snow|sunny)\b', 1.5),
                (r'\b(movie|film|tv show|series|actor|actress|cinema)\b', 1.5),
                (r'\b(sports|football|basketball|soccer|baseball|game|match)\b', 1.5),
                (r'\b(music|song|album|artist|band|concert)\b', 1.5),
                (r'\b(politics|election|president|government|vote)\b', 1.5),
                (r'\b(travel|vacation|hotel|flight|trip|destination)\b', 1.5),
                (r'\b(shopping|buy|purchase|store|price|deal)\b', 1.5),
                (r'\b(health|medical|doctor|hospital|medicine|symptom)\b', 1.5),
                (r'\b(personal|family|relationship|dating|friend)\b', 1.5),
            ],
            QueryIntent.META_FAULTMAVEN: [
                (r'\b(what (are|is) you|who (are|is) you|what can you do)\b', 2.0),
                (r'\b(faultmaven|your capabilities|your features|your purpose)\b', 1.8),
                (r'\b(how (do|does) you work|what.*your function)\b', 1.8),
                (r'\b(your limitations|what.*you (can\'?t|cannot))\b', 1.5),
                (r'\b(about (you|faultmaven)|tell me about (yourself|faultmaven))\b', 1.8),
            ],
            QueryIntent.CONVERSATION_CONTROL: [
                (r'\b(start over|reset|clear|new (topic|conversation|session))\b', 2.0),
                (r'\b(go back|previous|undo|revert)\b', 1.8),
                (r'\b(skip|next|move on|continue)\b', 1.5),
                (r'\b(stop|quit|exit|end|cancel)\b', 1.5),
                (r'\b(pause|wait|hold on)\b', 1.3),
            ],
            QueryIntent.GREETING: [
                (r'^(hi|hello|hey|greetings|good (morning|afternoon|evening))\b', 2.0),
                (r'\b(how are you|how\'s it going|what\'s up)\b', 1.8),
                (r'^(yo|sup|howdy)\b', 1.8),
            ],
            QueryIntent.GRATITUDE: [
                (r'\b(thank(s| you)|thx|ty|appreciate|grateful)\b', 2.0),
                (r'\b(great (job|work|help)|well done|awesome|excellent)\b', 1.5),
                (r'\b(that (helped|worked)|perfect|exactly)\b', 1.8),
            ]
        }

        # Exclusion rules - patterns that immediately disqualify an intent
        self.exclusion_rules = {
            QueryIntent.TROUBLESHOOTING: [
                # Exclude hypothetical/confirmation questions
                r'\bthis (won\'?t|will not) work,? (right|correct)\?\s*$',
                r'\b(would|will|should) this (work|be correct)\b',
                r'\bjust (confirming|checking|verifying)\b',
                # Exclude capability questions
                r'\b(can|could) (i|we) (use|run|do)\b',
            ],
            QueryIntent.PROCEDURAL: [
                # Exclude active problems/errors
                r'\b(broken|error|fail|not working|crashing)\b',
                r'\b(my|our|the) .* (is|are) (broken|failing)\b',
                r'\b(i\'?m|we\'?re) (getting|seeing) (error|issue)\b',
            ],
            QueryIntent.VALIDATION: [
                # Exclude actual active problems
                r'\b(my|our|the) .* (is|are) (broken|crashing|failing)\b',
                r'\b(i\'?m|we\'?re) (getting|seeing) (error|issue)\b',
                # Exclude general capability questions (VALIDATION is for specific hypothesis)
                r'\b(can|could) (i|we) (use|run|do)\b',
            ],
            QueryIntent.INFORMATION: [
                # Exclude actual troubleshooting
                r'\b(my|our|the) .* (is|are) (broken|crashing|failing|not working)\b',
                r'\b(i\'?m|we\'?re) (getting|seeing|experiencing) (error|issue|problem)\b',
            ],
            QueryIntent.BEST_PRACTICES: [
                # Exclude active problems requiring immediate fixes
                r'\b(my|our|the) .* (is|are) (broken|crashing|failing)\b',
                r'\b(error|issue|problem).*(right now|currently|at the moment)\b',
            ],
            QueryIntent.OPTIMIZATION: [
                # Exclude completely broken systems (optimization requires working baseline)
                r'\b(broken|crashed|down|not working|completely failed)\b',
                r'\b(error|exception|stack trace)\b',
            ],
            QueryIntent.DEPLOYMENT: [
                # Exclude troubleshooting existing deployments
                r'\b(deployment.*failed|deployment.*broken|rollback)\b',
                r'\b(my deployment.*(is|are) (failing|broken))\b',
            ],
            QueryIntent.VISUALIZATION: [
                # Exclude troubleshooting (user wants diagram, not problem solving)
                r'\b(fix|debug|solve|troubleshoot)\b',
                r'\b(error|exception|broken|failing)\b',
            ],
            QueryIntent.COMPARISON: [
                # Exclude active problems (user wants analysis, not fixes)
                r'\b(broken|error|failing|crashed)\b',
                r'\b(fix|debug|solve|troubleshoot)\b',
            ]
        }
        
        # Complexity patterns  
        self.complexity_patterns = {
            ComplexityLevel.SIMPLE: [
                r'^(yes|no|true|false)\s*$',
                r'\b(status|version|help|info)\b',
                r'^[^?]*\?\s*$'  # Simple questions
            ],
            ComplexityLevel.COMPLEX: [
                r'\b(analyze|investigate|research|deep dive)\b',
                r'\bmultiple.*systems?\b',
                r'\bcomplex.*environment\b',
                r'\broot cause analysis\b'
            ],
            ComplexityLevel.EXPERT: [
                r'\b(architecture|scalability|performance optimization)\b',
                r'\bmigration.*strategy\b',
                r'\bdisaster recovery\b',
                r'\bsecurity audit\b'
            ]
        }
        
        # Domain patterns
        self.domain_patterns = {
            TechnicalDomain.DATABASE: [
                r'\b(database|db|sql|mysql|postgres|mongodb|redis|query)\b',
                r'\b(table|schema|index|connection)\b'
            ],
            TechnicalDomain.NETWORKING: [
                r'\b(network|connection|ip|dns|firewall|port|protocol)\b',
                r'\b(tcp|udp|http|https|ssl|tls)\b'
            ],
            TechnicalDomain.APPLICATION: [
                r'\b(app|application|service|api|endpoint|code)\b',
                r'\b(java|python|node|javascript|php)\b'
            ],
            TechnicalDomain.INFRASTRUCTURE: [
                r'\b(server|infrastructure|cloud|aws|azure|kubernetes|docker)\b',
                r'\b(deployment|container|vm|instance)\b'
            ],
            TechnicalDomain.SECURITY: [
                r'\b(security|auth|authentication|authorization|vulnerability)\b',
                r'\b(ssl|certificate|encryption|firewall|access)\b'
            ],
            TechnicalDomain.PERFORMANCE: [
                r'\b(performance|slow|latency|throughput|bottleneck)\b',
                r'\b(memory|cpu|disk|load|optimization)\b'
            ]
        }
        
        # Urgency patterns
        self.urgency_patterns = {
            UrgencyLevel.CRITICAL: [
                r'\b(urgent|critical|emergency|production down|outage)\b',
                r'\b(asap|immediately|right now|critical issue)\b'
            ],
            UrgencyLevel.HIGH: [
                r'\b(important|high priority|blocking|stuck)\b',
                r'\b(customers affected|users complaining)\b'
            ]
        }

        # Sentiment patterns for user emotional state
        self.sentiment_patterns = {
            "frustration": [
                r'\b(frustrated|annoying|annoyed|sick of|fed up)\b',
                r'\b(still (not|doesn\'t)|again|keep (failing|breaking))\b',
                r'\b(this is (ridiculous|stupid)|what the hell|wtf)\b',
                r'!{2,}',  # Multiple exclamation marks
                r'\b(why (does|is) this|how is this|seriously)\b'
            ],
            "confusion": [
                r'\b(confused|confusing|don\'t understand|unclear)\b',
                r'\b(what does.*mean|what\'s happening|i\'m lost)\b',
                r'\b(makes no sense|doesn\'t make sense)\b',
                r'\?{2,}'  # Multiple question marks
            ],
            "urgency": [
                r'\b(urgent|asap|immediately|now|quick|quickly)\b',
                r'\b(critical|emergency|production|down)\b',
                r'\b(need.*now|help.*asap)\b'
            ],
            "satisfaction": [
                r'\b(thanks?|thank you|appreciate|great|perfect|excellent)\b',
                r'\b(worked|fixed|solved|resolved|good)\b',
                r'\b(helpful|exactly|that\'s it)\b'
            ]
        }

        # Information completeness indicators
        self.info_completeness_patterns = {
            "has_context": [
                r'\b(after|since|when|started|began)\b',
                r'\b(version|environment|setup|configuration)\b',
                r'\b(error (message|code|log)|stacktrace)\b'
            ],
            "missing_context": [
                r'^(it|this|that).*broken\b',  # Vague references
                r'\b(doesn\'t work|not working)\s*$',  # No details
                r'^(help|issue|problem)\s*$'  # Too brief
            ]
        }

        # Data submission detection patterns (Task 3: Data Submission Integration)
        # These patterns detect when user pastes logs/data without questions
        self.data_submission_patterns = {
            # High confidence indicators (2.0 weight)
            "timestamps": [
                (r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}', 2.0),  # ISO timestamp
                (r'\[\d{4}-\d{2}-\d{2}.*?\]', 2.0),                  # [2024-10-03 ...]
                (r'\d{2}:\d{2}:\d{2}\.\d{3}', 2.0),                  # HH:MM:SS.mmm
            ],
            "log_levels": [
                # Multiple log level indicators suggest log dump
                (r'(ERROR|WARN|INFO|DEBUG|TRACE).*\n.*\1', 2.0),    # Repeated log levels
                (r'\b(ERROR|WARNING|INFO|DEBUG)\b.*\n.*\b(ERROR|WARNING|INFO|DEBUG)\b', 1.8),
            ],
            "stack_traces": [
                (r'at\s+[\w.$]+\(.*?:\d+\)', 2.0),                  # Java stack trace
                (r'File ".*?", line \d+', 2.0),                      # Python stack trace
                (r'^\s+at\s+.*\(.*:\d+:\d+\)$', 2.0),               # JavaScript stack trace
                (r'Traceback \(most recent call last\)', 2.5),      # Python traceback
                (r'Exception in thread', 2.0),                       # Java exception
            ],
            "structured_data": [
                (r'^\s*\{[\s\S]*"[\w]+":\s*[\[\{"][\s\S]*\}\s*$', 1.8),  # JSON dump
                (r'^<\?xml', 1.8),                                   # XML dump
                (r'^\w+:\s*\S+\s*\n\w+:\s*\S+', 1.5),               # YAML-like key:value
            ],
            "repetitive_logs": [
                (r'(.*\n)\1{4,}', 1.8),                             # 5+ repeated lines
            ],
            "server_logs": [
                (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b.*\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', 1.5),  # Multiple IPs
                (r'(GET|POST|PUT|DELETE|PATCH)\s+/\S+\s+HTTP/\d\.\d', 1.8),  # HTTP requests
                (r'\b(nginx|apache|tomcat|iis)\b.*\[\d+/\w+/\d{4}', 1.5),  # Web server logs
            ],
            "metrics_data": [
                # Time-series metrics and monitoring data
                (r'(\d+\.\d+|\d+)\s+(cpu|memory|disk|network|latency|throughput)', 1.8),  # Metrics with values
                (r'\b(metric|gauge|counter|histogram)\s*:\s*\d+', 1.8),  # Prometheus-style metrics
                (r'(\d+\s*%|GB|MB|KB|ms|µs|ns)\s*.*\n.*(\d+\s*%|GB|MB|KB|ms|µs|ns)', 1.5),  # Repeated units
            ]
        }

        # Question patterns for data submission exclusion
        self.question_patterns = [
            r'\?$',                                                  # Ends with ?
            r'^(what|how|why|when|where|who|which|can|is|are|does|do|should|would|could)\s',
            r'\b(please|help|could you|can you|would you)\s',
            r'\b(explain|tell me|show me|describe)\s',
        ]

    def _compile_patterns(self):
        """Compile regex patterns for performance optimization"""
        # Compile weighted intent patterns (pattern, weight)
        self._intent_patterns_compiled = {}
        for intent, patterns in self.intent_patterns.items():
            self._intent_patterns_compiled[intent] = [
                (re.compile(pattern, re.IGNORECASE), weight)
                for pattern, weight in patterns
            ]

        # Compile exclusion rules
        self._exclusion_rules_compiled = {}
        for intent, patterns in self.exclusion_rules.items():
            self._exclusion_rules_compiled[intent] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        # Compile sentiment patterns
        self._sentiment_patterns_compiled = {}
        for sentiment, patterns in self.sentiment_patterns.items():
            self._sentiment_patterns_compiled[sentiment] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        # Compile completeness patterns
        self._completeness_patterns_compiled = {}
        for key, patterns in self.info_completeness_patterns.items():
            self._completeness_patterns_compiled[key] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        logger.debug("Compiled weighted patterns with exclusion rules for optimized matching")

    @trace("classification_engine_classify_query")
    async def classify_query(
        self,
        query: str,
        context: Optional["QueryContext"] = None
    ) -> QueryClassification:
        """Classify a user query and determine processing strategy

        Args:
            query: User query to classify
            context: Optional QueryContext with session, case, and conversation info

        Returns:
            QueryClassification with intent, confidence, and metadata
        """
        try:
            # Import and handle context
            from faultmaven.models.agentic import QueryContext
            if context is None:
                context = QueryContext()  # Use empty context with defaults

            # Stage 0: Data submission detection (BEFORE normalization to preserve length/format)
            data_submission_info = self._detect_data_submission(query)

            # Normalize query
            normalized_query = self._normalize_query(query)

            # Multi-stage classification
            results = {
                "query": query,
                "normalized_query": normalized_query,
                "classification_timestamp": datetime.utcnow().isoformat(),
                "context": {
                    "session_id": context.session_id,
                    "case_id": context.case_id,
                },
                "data_submission": data_submission_info  # Add data submission metadata
            }

            # Stage 1: Pattern-based classification (fast path)
            pattern_results = await self._pattern_classify(normalized_query)
            results.update(pattern_results)

            # Stage 2: Conditional LLM-based semantic classification
            # Determine whether to call LLM based on mode and pattern confidence
            pattern_confidence = results.get("confidence", 0.0)

            # Check if same provider optimization applies
            same_provider_for_response = context.same_provider_for_response

            should_call_llm = self._should_call_llm(pattern_confidence)

            # Optimization: Skip LLM classification if same provider will be used for response
            if should_call_llm and same_provider_for_response:
                logger.info(
                    f"Skipping LLM classification - same provider ({self.llm_provider_name}) will handle "
                    f"both classification and response generation. Pattern confidence: {pattern_confidence:.2f}"
                )
                results["classification_method"] = "pattern_only_same_provider_optimization"
                results["llm_called"] = False
                results["llm_skip_reason"] = (
                    f"same_provider_optimization (provider={self.llm_provider_name}, "
                    f"confidence={pattern_confidence:.2f})"
                )
                should_call_llm = False

            if should_call_llm:
                try:
                    llm_results = await self._llm_classify(query, context)
                    results.update(llm_results)
                    results["classification_method"] = "llm_enhanced"
                    results["llm_called"] = True
                    results["llm_trigger_reason"] = self._get_llm_trigger_reason(pattern_confidence)
                    logger.debug(
                        f"LLM classification called: pattern_confidence={pattern_confidence:.2f}, "
                        f"mode={self.llm_classification_mode.value}"
                    )
                except Exception as e:
                    logger.warning(f"LLM classification failed, using patterns only: {e}")
                    results["classification_method"] = "pattern_only"
                    results["llm_called"] = False
                    results["llm_error"] = str(e)
            else:
                results["classification_method"] = "pattern_only"
                results["llm_called"] = False
                results["llm_skip_reason"] = self._get_llm_skip_reason(pattern_confidence)
                logger.debug(
                    f"LLM classification skipped: pattern_confidence={pattern_confidence:.2f}, "
                    f"mode={self.llm_classification_mode.value}"
                )
            
            # Stage 3: Post-processing and validation
            results = self._validate_and_enhance_classification(results)

            # Record metrics for Phase 0 tracking
            final_confidence = results.get("confidence", 0.5)
            llm_called = results.get("llm_called", False)
            self.metrics.record_classification(final_confidence, llm_called)

            # Check for confidence override (forced clarification)
            if final_confidence < 0.4:
                self.metrics.record_override()

            logger.debug(f"Classified query: intent={results.get('intent')}, complexity={results.get('complexity')}")

            # Build metadata with routing information
            metadata = results.get("metadata", {})

            # Add data submission routing metadata
            data_info = results.get("data_submission", {})
            if data_info.get("should_route_to_upload", False):
                metadata["suggested_route"] = "data_upload"
                metadata["route_reason"] = data_info.get("reason", "Data submission detected")
                logger.info(f"Query classified as data submission - suggesting data upload route: {data_info.get('reason')}")

            # Add data indicators even if not routing (helps LLM understand context)
            if data_info.get("detected_patterns"):
                metadata["data_indicators"] = data_info["detected_patterns"]
                metadata["data_confidence"] = data_info.get("confidence", 0.0)

            # Convert dictionary to QueryClassification object
            classification = QueryClassification(
                query=results.get("query", query),
                normalized_query=results.get("normalized_query", query.lower()),
                intent=QueryIntent(results.get("intent", QueryIntent.UNKNOWN)),
                confidence=results.get("confidence", 0.5),
                complexity=results.get("complexity", "moderate"),
                domain=results.get("domain", "general"),
                urgency=results.get("urgency", "medium"),
                entities=results.get("entities", []),
                context=results.get("context", {}),
                classification_timestamp=results.get("classification_timestamp", datetime.utcnow().isoformat()),
                classification_method=results.get("classification_method", "pattern_based"),
                processing_recommendations=results.get("processing_recommendations", {}),
                metadata=metadata
            )

            return classification
            
        except Exception as e:
            logger.error(f"Query classification failed: {e}")
            # Return minimal fallback classification
            return QueryClassification(
                query=query,
                normalized_query=query.lower(),
                intent=QueryIntent.UNKNOWN,
                confidence=0.1,
                complexity="moderate",
                domain="general",
                urgency="medium",
                entities=[],
                context={},
                classification_method="fallback",
                processing_recommendations={},
                metadata={"error": str(e)}
            )
    
    @trace("classification_engine_extract_intent")
    async def extract_intent(self, query: str) -> Dict[str, Any]:
        """Extract user intent from query using weighted pattern matching with exclusion rules"""
        try:
            normalized_query = self._normalize_query(query)

            # Check patterns for each intent with weighted scoring
            intent_scores = {}
            for intent, compiled_patterns in self._intent_patterns_compiled.items():
                # First check exclusion rules
                excluded = False
                if intent in self._exclusion_rules_compiled:
                    for exclusion_pattern in self._exclusion_rules_compiled[intent]:
                        if exclusion_pattern.search(normalized_query):
                            excluded = True
                            logger.debug(f"Intent {intent.value} excluded by pattern: {exclusion_pattern.pattern}")
                            break

                if excluded:
                    continue  # Skip this intent entirely

                # Calculate weighted score
                weighted_score = 0.0
                max_possible_weight = sum(weight for _, weight in compiled_patterns)
                matches = []

                for compiled_pattern, weight in compiled_patterns:
                    if compiled_pattern.search(normalized_query):
                        weighted_score += weight
                        matches.append(compiled_pattern.pattern)

                if weighted_score > 0:
                    # Confidence = weighted_score / max_possible_weight
                    confidence = weighted_score / max_possible_weight if max_possible_weight > 0 else 0.0
                    intent_scores[intent.value] = {
                        "weighted_score": weighted_score,
                        "max_possible_weight": max_possible_weight,
                        "matches": matches,
                        "confidence": confidence
                    }

            # Determine primary intent (highest weighted score)
            if intent_scores:
                primary_intent = max(intent_scores.keys(), key=lambda k: intent_scores[k]["weighted_score"])
                confidence = intent_scores[primary_intent]["confidence"]
            else:
                primary_intent = QueryIntent.UNKNOWN.value
                confidence = 0.0

            return {
                "primary_intent": primary_intent,
                "confidence": confidence,
                "all_intents": intent_scores,
                "method": "weighted_pattern_matching"
            }

        except Exception as e:
            logger.error(f"Intent extraction failed: {e}")
            return {
                "primary_intent": QueryIntent.UNKNOWN.value,
                "confidence": 0.0,
                "error": str(e)
            }
    
    @trace("classification_engine_assess_complexity")
    async def assess_complexity(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Assess query complexity to determine sync vs async processing"""
        try:
            normalized_query = self._normalize_query(query)
            
            # Base complexity assessment
            complexity_indicators = {
                "query_length": len(query),
                "word_count": len(query.split()),
                "question_complexity": self._assess_question_complexity(query),
                "technical_depth": self._assess_technical_depth(query),
                "scope_breadth": self._assess_scope_breadth(query)
            }
            
            # Pattern-based complexity matching
            for complexity, patterns in self.complexity_patterns.items():
                for pattern in patterns:
                    if re.search(pattern, normalized_query, re.IGNORECASE):
                        complexity_indicators["pattern_match"] = complexity.value
                        break
            
            # Calculate overall complexity
            complexity_score = self._calculate_complexity_score(complexity_indicators)
            
            # Determine processing mode
            processing_mode = self._determine_processing_mode(complexity_score, context)
            
            return {
                "complexity_level": self._score_to_complexity_level(complexity_score),
                "complexity_score": complexity_score,
                "processing_mode": processing_mode,
                "indicators": complexity_indicators,
                "estimated_processing_time": self._estimate_processing_time(complexity_score)
            }
            
        except Exception as e:
            logger.error(f"Complexity assessment failed: {e}")
            return {
                "complexity_level": ComplexityLevel.MODERATE.value,
                "complexity_score": 0.5,
                "processing_mode": "sync",
                "error": str(e)
            }
    
    @trace("classification_engine_identify_domain")
    async def identify_domain(self, query: str) -> Optional[str]:
        """Identify the domain/category of the query"""
        try:
            normalized_query = self._normalize_query(query)
            
            # Check domain patterns
            domain_scores = {}
            for domain, patterns in self.domain_patterns.items():
                score = 0
                for pattern in patterns:
                    if re.search(pattern, normalized_query, re.IGNORECASE):
                        score += 1
                
                if score > 0:
                    domain_scores[domain.value] = score
            
            # Return domain with highest score
            if domain_scores:
                primary_domain = max(domain_scores.keys(), key=lambda k: domain_scores[k])
                return primary_domain
            
            return TechnicalDomain.GENERAL.value
            
        except Exception as e:
            logger.error(f"Domain identification failed: {e}")
            return TechnicalDomain.GENERAL.value
    
    @trace("classification_engine_extract_entities")
    async def extract_entities(self, query: str) -> List[Dict[str, Any]]:
        """Extract entities from the query"""
        try:
            entities = []
            
            # Extract common technical entities
            entity_patterns = {
                "ip_address": r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
                "port": r'\b(?:port\s+)?(\d{1,5})\b',
                "url": r'https?://[^\s]+',
                "file_path": r'[/\\][^\s]+\.[a-zA-Z0-9]+',
                "service_name": r'\b[a-zA-Z0-9_-]+(?:\.service|\.exe|\.jar)\b',
                "error_code": r'\b(?:error|code)\s+(\d+)\b',
                "version": r'\bv?(\d+(?:\.\d+)*)\b'
            }
            
            for entity_type, pattern in entity_patterns.items():
                matches = re.finditer(pattern, query, re.IGNORECASE)
                for match in matches:
                    entities.append({
                        "type": entity_type,
                        "value": match.group(),
                        "start": match.start(),
                        "end": match.end(),
                        "confidence": 0.8
                    })
            
            return entities
            
        except Exception as e:
            logger.error(f"Entity extraction failed: {e}")
            return []
    
    # Private helper methods

    def _should_call_llm(self, pattern_confidence: float) -> bool:
        """Determine whether to call LLM based on mode and pattern confidence

        Args:
            pattern_confidence: Confidence from pattern matching (0.0-1.0)

        Returns:
            bool: True if LLM should be called
        """
        if self.llm_classification_mode == LLMClassificationMode.DISABLED:
            return False

        if self.llm_classification_mode == LLMClassificationMode.ALWAYS:
            return True

        if self.llm_classification_mode == LLMClassificationMode.FALLBACK:
            # Call LLM only if patterns completely failed (confidence = 0)
            return pattern_confidence == 0.0

        if self.llm_classification_mode == LLMClassificationMode.ENHANCEMENT:
            # Call LLM if pattern confidence below threshold (RECOMMENDED)
            return pattern_confidence < self.pattern_confidence_threshold

        # Default: don't call LLM
        return False

    def _get_llm_trigger_reason(self, pattern_confidence: float) -> str:
        """Get human-readable reason for calling LLM

        Args:
            pattern_confidence: Confidence from pattern matching

        Returns:
            str: Reason for calling LLM
        """
        if self.llm_classification_mode == LLMClassificationMode.ALWAYS:
            return "mode=ALWAYS"

        if self.llm_classification_mode == LLMClassificationMode.FALLBACK:
            return f"mode=FALLBACK, pattern_confidence={pattern_confidence:.2f} (0.0)"

        if self.llm_classification_mode == LLMClassificationMode.ENHANCEMENT:
            return (
                f"mode=ENHANCEMENT, pattern_confidence={pattern_confidence:.2f} "
                f"< threshold={self.pattern_confidence_threshold}"
            )

        return "unknown"

    def _get_llm_skip_reason(self, pattern_confidence: float) -> str:
        """Get human-readable reason for skipping LLM

        Args:
            pattern_confidence: Confidence from pattern matching

        Returns:
            str: Reason for skipping LLM
        """
        if self.llm_classification_mode == LLMClassificationMode.DISABLED:
            return "mode=DISABLED"

        if self.llm_classification_mode == LLMClassificationMode.FALLBACK:
            return f"mode=FALLBACK, pattern_confidence={pattern_confidence:.2f} > 0.0"

        if self.llm_classification_mode == LLMClassificationMode.ENHANCEMENT:
            return (
                f"mode=ENHANCEMENT, pattern_confidence={pattern_confidence:.2f} "
                f">= threshold={self.pattern_confidence_threshold}"
            )

        return "unknown"

    def _normalize_query(self, query: str) -> str:
        """Normalize query for pattern matching"""
        if not query:
            return ""

        # Basic normalization
        normalized = query.lower().strip()

        # Remove extra whitespace
        normalized = re.sub(r'\s+', ' ', normalized)

        return normalized

    def _detect_data_submission(self, query: str) -> Dict[str, Any]:
        """Detect if query is a data submission that should be routed to data upload endpoint

        Strategy:
        - Messages >10K chars: Automatically route to data upload (too large for LLM context)
        - Messages <10K chars: Let LLM process, but add metadata hints about data patterns

        Returns:
            Dict with keys: should_route_to_upload (bool), confidence (float), detected_patterns (list)
        """
        # Hard limit: messages > 10K chars are definitely data dumps
        HARD_LIMIT = 10000

        query_length = len(query)

        if query_length > HARD_LIMIT:
            # Automatically route to data upload - too large for normal query processing
            return {
                "should_route_to_upload": True,
                "confidence": 1.0,
                "detected_patterns": ["length_threshold"],
                "reason": f"Message length {query_length} exceeds hard limit {HARD_LIMIT}",
                "length": query_length
            }

        # For messages under 10K, detect data patterns to help LLM classification
        # but don't force routing - let LLM decide
        detected_patterns = []
        data_score = 0.0

        for category, patterns in self.data_submission_patterns.items():
            for pattern_str, weight in patterns:
                try:
                    if re.search(pattern_str, query, re.MULTILINE | re.DOTALL):
                        data_score += weight
                        detected_patterns.append(category)
                        break  # One match per category is enough
                except re.error as e:
                    logger.warning(f"Regex error in data submission pattern {pattern_str}: {e}")

        # Normalize confidence (max score ~10-12)
        confidence = min(data_score / 10.0, 0.9)  # Cap at 0.9 for under-limit messages

        return {
            "should_route_to_upload": False,  # Under limit - let LLM process
            "confidence": confidence,
            "detected_patterns": list(set(detected_patterns)),  # Unique categories
            "data_indicators": len(detected_patterns),
            "length": query_length,
            "reason": "Under hard limit - LLM will process"
        }

    async def _pattern_classify(self, query: str) -> Dict[str, Any]:
        """Perform pattern-based classification"""
        try:
            results = {}
            
            # Extract intent
            intent_result = await self.extract_intent(query)
            results["intent"] = intent_result["primary_intent"]
            results["intent_confidence"] = intent_result["confidence"]
            
            # Assess complexity
            complexity_result = await self.assess_complexity(query)
            results["complexity"] = complexity_result["complexity_level"]
            results["complexity_score"] = complexity_result["complexity_score"]
            results["processing_mode"] = complexity_result["processing_mode"]
            
            # Identify domain
            domain = await self.identify_domain(query)
            results["domain"] = domain
            
            # Assess urgency
            urgency = self._assess_urgency_patterns(query)
            results["urgency"] = urgency
            
            # Extract entities
            entities = await self.extract_entities(query)
            results["entities"] = entities
            
            # Calculate overall confidence
            results["confidence"] = self._calculate_overall_confidence(results)
            
            return results
            
        except Exception as e:
            logger.error(f"Pattern classification failed: {e}")
            return {}
    
    async def _llm_classify(self, query: str, context: "QueryContext") -> Dict[str, Any]:
        """Perform LLM-based semantic classification

        Args:
            query: User query to classify
            context: QueryContext with conversation history and session info

        Returns:
            Dictionary with LLM classification results
        """
        try:
            if not self.llm_provider:
                return {}

            # Construct classification prompt
            prompt = self._build_classification_prompt(query, context)
            
            # Get LLM response
            response = await self.llm_provider.generate(
                prompt=prompt,
                max_tokens=500,
                temperature=0.1,  # Low temperature for consistent classification
            )
            
            # Parse LLM response
            if response and response.strip():
                llm_results = self._parse_llm_classification_response(response)
                return llm_results
            else:
                logger.warning("Empty or whitespace-only response from LLM classification")
                return {}
            
        except Exception as e:
            logger.error(f"LLM classification failed: {e}")
            return {}
    
    def _build_classification_prompt(self, query: str, context: "QueryContext") -> str:
        """Build enhanced LLM classification prompt with definitions and examples

        Args:
            query: User query to classify
            context: QueryContext with conversation history and session info

        Returns:
            Complete classification prompt for LLM
        """
        # Extract conversation history if available
        conversation_section = ""
        if context.has_conversation_context():
            conversation_section = f"""
## Previous Conversation

{context.conversation_history}

IMPORTANT: Use the conversation history to understand the full context. The user's current query may reference previous messages, continue an ongoing topic, or relate to earlier issues discussed.
"""

        # Build session context (if available)
        session_context = ""
        if context.session_id or context.case_id:
            session_context = f"\nSession: {context.session_id}, Case: {context.case_id}"

        prompt = f"""You are a query classifier for an SRE troubleshooting AI system.

Classify the following user query by analyzing its TRUE INTENT based on the ENTIRE CONTEXT, not just the current question.
{conversation_section}

## Current User Query

"{query}"{session_context}

=== INTENT CATEGORIES ===

Choose the PRIMARY intent from the following categories:

1. **TROUBLESHOOTING** - Active problems requiring diagnosis
   - User has a current, ongoing problem
   - Examples: "my pod is crashing", "getting 502 errors", "database connection failing"

2. **PROCEDURAL** - How-to or capability questions
   - Asking IF something is possible or HOW to do something
   - No active problem, just seeking guidance
   - Examples: "can I use X to do Y?", "how do I configure X?", "is it possible to run X and Y together?"

3. **VALIDATION** - Confirming if a specific approach will work
   - User has a hypothesis and wants confirmation
   - Examples: "will this work?", "is this the right approach?", "this won't work, right?"

4. **INFORMATION** - Questions about concepts or methodology
   - Asking ABOUT principles, best practices, or system design
   - Examples: "what is the 5-phase doctrine?", "explain SRE principles", "what are the troubleshooting steps?"

5. **EXPLANATION** - Understanding how/why something works
   - Seeking conceptual understanding
   - Examples: "why does X happen?", "explain how Y works", "what causes Z?"

6. **CONFIGURATION** - System setup and configuration tasks
   - Configuring systems or troubleshooting configuration issues
   - Examples: "how do I configure nginx?", "my config isn't loading"

7. **STATUS_CHECK** - Checking system status or health
   - Examples: "is the service running?", "check cluster health"

8. **UNKNOWN** - Query doesn't fit clear categories
   - Use only if truly ambiguous

=== CRITICAL DISTINCTIONS ===

**PROCEDURAL vs TROUBLESHOOTING:**
- PROCEDURAL: "can I run git push to create a repo?" (asking about capability)
- TROUBLESHOOTING: "git push is failing with error X" (active problem)

**PROCEDURAL vs VALIDATION:**
- PROCEDURAL: "can I do X?" (general capability question)
- VALIDATION: "will doing X this specific way work?" (confirming hypothesis)

**INFORMATION vs PROCEDURAL:**
- INFORMATION: "what is the process for X?" (about methodology)
- PROCEDURAL: "how do I do X?" (seeking step-by-step instructions)

**PROCEDURAL vs CONFIGURATION:**
- PROCEDURAL: General how-to questions about any task
- CONFIGURATION: Specifically about system setup/config

=== CLASSIFICATION RULES ===

1. If query mentions active problems (error, broken, failing, not working) → likely TROUBLESHOOTING
2. If query asks "can I", "how do I", "is it possible" → likely PROCEDURAL
3. If query asks for confirmation ("will this work?", "right?") → likely VALIDATION
4. If query asks ABOUT methodology/principles → INFORMATION
5. Default to PROCEDURAL for how-to questions, not TROUBLESHOOTING

=== OUTPUT FORMAT ===

Return ONLY valid JSON with this structure:
{{
  "intent": "one of: troubleshooting, procedural, validation, information, explanation, configuration, status_check, unknown",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation of why you chose this intent (1-2 sentences)",
  "complexity": "simple|moderate|complex",
  "domain": "application|infrastructure|database|networking|kubernetes|storage|security|ci_cd|monitoring|deployment|general",
  "urgency": "low|medium|high|critical"
}}

Analyze the query carefully and return your classification:"""

        return prompt
    
    def _parse_llm_classification_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM classification response"""
        try:
            import json

            # Try to extract JSON from response
            response = response.strip()

            # Check if response is empty after stripping
            if not response:
                logger.warning("Empty response after stripping in classification parsing")
                return {}

            if response.startswith('```json'):
                response = response[7:]
            elif response.startswith('```'):
                response = response[3:]
            if response.endswith('```'):
                response = response[:-3]

            # Final check after removing code blocks
            response = response.strip()
            if not response:
                logger.warning("Empty response after removing code blocks in classification parsing")
                return {}

            # Parse JSON
            parsed = json.loads(response)
            
            # Validate and normalize fields
            result = {}
            if "intent" in parsed:
                result["intent"] = parsed["intent"]
            if "complexity" in parsed:
                result["complexity"] = parsed["complexity"]
            if "domain" in parsed:
                result["domain"] = parsed["domain"]
            if "urgency" in parsed:
                result["urgency"] = parsed["urgency"]
            if "confidence" in parsed:
                result["llm_confidence"] = float(parsed["confidence"])
            if "reasoning" in parsed:
                result["reasoning"] = parsed["reasoning"]
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to parse LLM classification response: {e}")
            return {}
    
    def _validate_and_enhance_classification(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and enhance classification results"""

        # Meta-query override: If query is asking ABOUT methodology/processes (not actual problems),
        # override to INFORMATION intent
        query = results.get("query", "").lower()
        is_meta_query = self._is_meta_query(query)
        if is_meta_query:
            results["intent"] = QueryIntent.INFORMATION.value
            results["metadata"] = results.get("metadata", {})
            results["metadata"]["is_meta_query"] = True
            results["metadata"]["meta_query_override"] = "Query about methodology, not actual troubleshooting"
            logger.debug(f"Meta-query detected - overriding to INFORMATION intent: {query[:50]}...")

        # Ensure all required fields have values
        if "intent" not in results or not results["intent"]:
            results["intent"] = QueryIntent.UNKNOWN.value

        if "complexity" not in results or not results["complexity"]:
            results["complexity"] = ComplexityLevel.MODERATE.value

        if "domain" not in results or not results["domain"]:
            results["domain"] = TechnicalDomain.GENERAL.value

        if "urgency" not in results or not results["urgency"]:
            results["urgency"] = UrgencyLevel.MEDIUM.value

        # Ensure confidence is set
        if "confidence" not in results:
            results["confidence"] = 0.5

        # Add processing recommendations
        results["processing_recommendations"] = self._generate_processing_recommendations(results)

        return results

    def _is_meta_query(self, query: str) -> bool:
        """Detect if query is asking ABOUT the system/methodology vs asking FOR help

        Meta-queries are information requests about processes/methodology:
        - "what are the steps to troubleshoot..."
        - "how does the doctrine work..."
        - "explain the methodology..."

        NOT meta-queries (actual problems):
        - "my pod is crashing..."
        - "I'm getting errors..."
        - "how do I fix..."

        Returns:
            bool: True if this is a meta-query about methodology
        """
        # Meta-query patterns (asking ABOUT methodology/processes)
        meta_patterns = [
            r'\b(what (are|is) the (steps|phases|process|methodology|doctrine))\b',
            r'\b(how does .* (work|function))\b',
            r'\b(explain (the|your) (approach|methodology|process|doctrine))\b',
            r'\b(tell me about (the|your) (process|methodology|approach))\b',
            r'\b(describe (the|your) (steps|process|methodology))\b',
            r'\b(what (should|do) (i|we) (do|follow)) (to|when|for) (troubleshoot|diagnose)\b',
            r'\b(sre (doctrine|principles|methodology|practices))\b',
            r'\b(troubleshooting (approach|methodology|steps|process))\b',
        ]

        # Anti-patterns (actual troubleshooting queries - NOT meta)
        actual_problem_patterns = [
            r'\b(my|our|the) .* (is|are) (broken|crashing|failing|not working)\b',
            r'\b(i\'?m|we\'?re) (getting|seeing|experiencing) (error|issue|problem)\b',
            r'\b(how (do i|can i|to)) (fix|solve|resolve|debug)\b',
            r'\b(help.*troubleshoot|help.*fix|help.*debug)\b',
        ]

        # First check anti-patterns (actual problems override)
        for pattern in actual_problem_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return False  # This is an actual problem, not a meta-query

        # Then check meta-patterns
        for pattern in meta_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return True  # This is asking about methodology

        return False  # Not a meta-query
    
    def _assess_urgency_patterns(self, query: str) -> str:
        """Assess urgency using pattern matching"""
        for urgency, patterns in self.urgency_patterns.items():
            for pattern in patterns:
                if re.search(pattern, query, re.IGNORECASE):
                    return urgency.value
        
        return UrgencyLevel.MEDIUM.value
    
    def _assess_question_complexity(self, query: str) -> float:
        """Assess complexity of the question structure"""
        complexity_score = 0.0
        
        # Multiple questions
        question_marks = query.count('?')
        if question_marks > 1:
            complexity_score += 0.2
        
        # Conditional language
        conditional_words = ['if', 'when', 'unless', 'provided', 'assuming']
        for word in conditional_words:
            if word in query.lower():
                complexity_score += 0.1
        
        # Multiple clauses
        clause_indicators = ['and', 'but', 'however', 'also', 'additionally']
        for indicator in clause_indicators:
            if indicator in query.lower():
                complexity_score += 0.1
        
        return min(complexity_score, 1.0)
    
    def _assess_technical_depth(self, query: str) -> float:
        """Assess technical depth of the query"""
        depth_score = 0.0
        
        # Technical terminology
        technical_terms = ['architecture', 'configuration', 'implementation', 'integration', 'optimization']
        for term in technical_terms:
            if term in query.lower():
                depth_score += 0.2
        
        # Specific technologies mentioned
        tech_count = len(re.findall(r'\b[A-Z]{2,}\b', query))  # Acronyms
        depth_score += min(tech_count * 0.1, 0.3)
        
        return min(depth_score, 1.0)
    
    def _assess_scope_breadth(self, query: str) -> float:
        """Assess how broad the scope of the query is"""
        scope_score = 0.0
        
        # Multiple systems/components mentioned
        system_indicators = ['system', 'service', 'application', 'component', 'module']
        system_count = sum(1 for indicator in system_indicators if indicator in query.lower())
        scope_score += min(system_count * 0.15, 0.4)
        
        # Environment indicators
        env_indicators = ['production', 'staging', 'development', 'test']
        for env in env_indicators:
            if env in query.lower():
                scope_score += 0.1
        
        return min(scope_score, 1.0)
    
    def _calculate_complexity_score(self, indicators: Dict[str, Any]) -> float:
        """Calculate overall complexity score from indicators"""
        score = 0.0
        
        # Query length factor
        length_factor = min(indicators.get("query_length", 0) / 200, 0.3)
        score += length_factor
        
        # Word count factor
        word_factor = min(indicators.get("word_count", 0) / 50, 0.2)
        score += word_factor
        
        # Question complexity
        score += indicators.get("question_complexity", 0) * 0.2
        
        # Technical depth
        score += indicators.get("technical_depth", 0) * 0.2
        
        # Scope breadth
        score += indicators.get("scope_breadth", 0) * 0.1
        
        return min(score, 1.0)
    
    def _score_to_complexity_level(self, score: float) -> str:
        """Convert complexity score to complexity level"""
        if score < 0.3:
            return ComplexityLevel.SIMPLE.value
        elif score < 0.6:
            return ComplexityLevel.MODERATE.value
        elif score < 0.8:
            return ComplexityLevel.COMPLEX.value
        else:
            return ComplexityLevel.EXPERT.value
    
    def _determine_processing_mode(self, complexity_score: float, context: Optional[Dict[str, Any]]) -> str:
        """Determine whether to process sync or async"""
        # Default to sync for better UX
        if complexity_score < 0.7:
            return "sync"
        
        # Check context for urgency indicators
        if context and context.get("urgency") == "critical":
            return "sync"  # Critical issues need immediate response
        
        # High complexity suggests async processing
        if complexity_score > 0.8:
            return "async"
        
        return "sync"
    
    def _estimate_processing_time(self, complexity_score: float) -> float:
        """Estimate processing time in seconds"""
        base_time = 2.0  # Base 2 seconds
        complexity_multiplier = 1 + (complexity_score * 3)  # 1x to 4x multiplier
        
        return base_time * complexity_multiplier
    
    def _calculate_overall_confidence(self, results: Dict[str, Any]) -> float:
        """Calculate overall classification confidence with multi-dimensional enhancement

        Confidence formula:
        base_confidence = pattern_confidence (weighted pattern matching)
        enhanced_confidence = base_confidence + multi_dimensional_boost
        final_confidence = min(enhanced_confidence, 1.0)

        Multi-dimensional factors (up to +0.5/-0.3 adjustment):
        1. Query structure analysis (-0.2 to +0.2)
        2. Linguistic markers (0 to +0.15)
        3. Entity presence (0 to +0.15)
        4. Conversation context (-0.1 to +0.1)
        5. Cross-intent disambiguation (-0.3 to 0)
        """
        # Base confidence from pattern matching
        base_confidence = results.get("intent_confidence", 0.5)

        # Calculate multi-dimensional boost
        query = results.get("normalized_query", "")
        boost = 0.0

        # Factor 1: Query structure analysis (-0.2 to +0.2)
        structure_boost = self._assess_query_structure(query)
        boost += structure_boost

        # Factor 2: Linguistic markers (0 to +0.15)
        linguistic_boost = self._assess_linguistic_markers(query)
        boost += linguistic_boost

        # Factor 3: Entity presence (0 to +0.15)
        entities = results.get("entities", [])
        entity_boost = min(len(entities) * 0.05, 0.15)
        boost += entity_boost

        # Factor 4: Conversation context (-0.1 to +0.1)
        context = results.get("context", {})
        context_boost = self._assess_conversation_context(context)
        boost += context_boost

        # Factor 5: Cross-intent disambiguation (-0.3 to 0)
        all_intents = results.get("all_intents", {})
        disambiguation_penalty = self._assess_cross_intent_ambiguity(all_intents)
        boost += disambiguation_penalty

        # Apply boost
        enhanced_confidence = base_confidence + boost

        # Store breakdown for debugging
        results["confidence_breakdown"] = {
            "base_confidence": base_confidence,
            "structure_boost": structure_boost,
            "linguistic_boost": linguistic_boost,
            "entity_boost": entity_boost,
            "context_boost": context_boost,
            "disambiguation_penalty": disambiguation_penalty,
            "total_boost": boost,
            "final_confidence": min(max(enhanced_confidence, 0.0), 1.0)
        }

        # Final confidence (capped at 0.0-1.0)
        return min(max(enhanced_confidence, 0.0), 1.0)

    def _assess_query_structure(self, query: str) -> float:
        """Assess query structure quality (-0.2 to +0.2)

        Well-structured queries boost confidence.
        Fragmented/unclear queries reduce confidence.
        """
        boost = 0.0

        # Positive indicators (complete sentences, proper questions)
        if re.search(r'\?\s*$', query):  # Ends with question mark
            boost += 0.1
        if len(query.split()) >= 5:  # Reasonable length
            boost += 0.05
        if re.search(r'\b(what|how|why|when|where|who)\b', query):  # Question word
            boost += 0.05

        # Negative indicators (fragments, unclear structure)
        if len(query.split()) <= 2:  # Too short/fragmented
            boost -= 0.1
        if re.match(r'^(it|this|that)\b', query):  # Vague reference
            boost -= 0.1

        return boost

    def _assess_linguistic_markers(self, query: str) -> float:
        """Assess linguistic clarity markers (0 to +0.15)

        Clear linguistic markers increase confidence.
        """
        boost = 0.0

        # Action verbs (clear intent)
        action_verbs = [
            r'\b(troubleshoot|fix|solve|resolve|diagnose|analyze)\b',
            r'\b(configure|setup|install|deploy|update)\b',
            r'\b(check|verify|validate|confirm|test)\b'
        ]
        for pattern in action_verbs:
            if re.search(pattern, query, re.IGNORECASE):
                boost += 0.05
                break

        # Technical specificity (mentions specific components)
        if re.search(r'\b(pod|service|deployment|container|node|cluster)\b', query, re.IGNORECASE):
            boost += 0.05

        # Temporal markers (when it started, how long, etc.)
        if re.search(r'\b(since|after|when|started|began|yesterday|today)\b', query, re.IGNORECASE):
            boost += 0.05

        return min(boost, 0.15)

    def _assess_conversation_context(self, context: Dict[str, Any]) -> float:
        """Assess conversation context relevance (-0.1 to +0.1)

        Previous conversation context can increase or decrease confidence.
        """
        boost = 0.0

        # If there's relevant conversation history, boost confidence
        if context.get("has_previous_context", False):
            boost += 0.05

        # If user is following up on previous topic, boost confidence
        if context.get("is_followup", False):
            boost += 0.05

        # If there's conflicting context, reduce confidence
        if context.get("context_conflict", False):
            boost -= 0.1

        return boost

    def _assess_cross_intent_ambiguity(self, all_intents: Dict[str, Any]) -> float:
        """Assess cross-intent ambiguity (-0.3 to 0)

        If multiple intents have similar scores, reduce confidence (ambiguous).
        If one intent clearly dominates, no penalty.
        """
        if not all_intents or len(all_intents) <= 1:
            return 0.0  # No ambiguity

        # Get top 2 intent scores
        sorted_intents = sorted(
            all_intents.items(),
            key=lambda x: x[1].get("weighted_score", 0),
            reverse=True
        )

        if len(sorted_intents) < 2:
            return 0.0

        top_score = sorted_intents[0][1].get("weighted_score", 0)
        second_score = sorted_intents[1][1].get("weighted_score", 0)

        # Calculate ambiguity ratio
        if top_score == 0:
            return -0.3  # Maximum penalty if no clear winner

        ratio = second_score / top_score

        # High ambiguity (scores are close)
        if ratio > 0.8:
            return -0.3
        elif ratio > 0.6:
            return -0.2
        elif ratio > 0.4:
            return -0.1

        return 0.0  # Clear winner, no penalty
    
    def _generate_processing_recommendations(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Generate processing recommendations based on classification"""
        recommendations = {
            "workflow_type": "general_troubleshooting",
            "tools_suggested": [],
            "escalation_needed": False,
            "estimated_duration": 30.0
        }
        
        # Intent-based recommendations
        intent = results.get("intent", "unknown")
        if intent == QueryIntent.TROUBLESHOOTING.value:
            recommendations["workflow_type"] = "diagnostic_workflow"
            recommendations["tools_suggested"] = ["diagnostic_analyzer", "log_parser"]
        elif intent == QueryIntent.STATUS_CHECK.value:
            # v3.0: MONITORING merged into STATUS_CHECK
            recommendations["workflow_type"] = "monitoring_workflow"
            recommendations["tools_suggested"] = ["metrics_collector", "dashboard_builder"]
        
        # Complexity-based recommendations
        complexity = results.get("complexity", "moderate")
        if complexity == ComplexityLevel.EXPERT.value:
            recommendations["escalation_needed"] = True
            recommendations["estimated_duration"] = 120.0
        elif complexity == ComplexityLevel.SIMPLE.value:
            recommendations["estimated_duration"] = 10.0
        
        # Domain-based tool suggestions
        domain = results.get("domain", "general")
        if domain == TechnicalDomain.DATABASE.value:
            recommendations["tools_suggested"].extend(["db_analyzer", "query_optimizer"])
        elif domain == TechnicalDomain.NETWORKING.value:
            recommendations["tools_suggested"].extend(["network_scanner", "connectivity_tester"])

        return recommendations

    def detect_sentiment(self, query: str) -> Dict[str, Any]:
        """Detect user sentiment and emotional state

        Args:
            query: User query text

        Returns:
            Dictionary with sentiment scores and primary sentiment
        """
        try:
            normalized_query = self._normalize_query(query)
            sentiment_scores = {}

            # Check each sentiment pattern (using compiled regex)
            for sentiment_type, compiled_patterns in self._sentiment_patterns_compiled.items():
                score = 0
                matches = []
                for pattern in compiled_patterns:
                    if pattern.search(normalized_query):
                        score += 1
                        matches.append(pattern.pattern)

                if score > 0:
                    sentiment_scores[sentiment_type] = {
                        "score": score,
                        "matches": matches,
                        "intensity": min(score / len(compiled_patterns), 1.0)
                    }

            # Determine primary sentiment
            primary_sentiment = "neutral"
            max_score = 0

            if sentiment_scores:
                for sentiment, data in sentiment_scores.items():
                    if data["score"] > max_score:
                        max_score = data["score"]
                        primary_sentiment = sentiment

            return {
                "primary_sentiment": primary_sentiment,
                "all_sentiments": sentiment_scores,
                "sentiment_detected": bool(sentiment_scores)
            }

        except Exception as e:
            logger.error(f"Sentiment detection failed: {e}")
            return {
                "primary_sentiment": "neutral",
                "all_sentiments": {},
                "sentiment_detected": False,
                "error": str(e)
            }

    def get_confidence_statistics(self) -> Dict[str, Any]:
        """Get confidence and LLM call statistics

        Returns:
            Dict containing tier distribution, LLM skip rate, and override counts
        """
        return self.metrics.get_statistics()

    def reset_metrics(self):
        """Reset all metrics tracking (useful for testing)"""
        self.metrics.reset()

    def assess_information_completeness(self, query: str) -> Dict[str, Any]:
        """Assess how much information the user has provided

        Args:
            query: User query text

        Returns:
            Dictionary with completeness assessment
        """
        try:
            normalized_query = self._normalize_query(query)

            # Check for contextual information (using compiled regex)
            has_context_score = 0
            context_indicators = []

            for pattern in self._completeness_patterns_compiled.get("has_context", []):
                if pattern.search(normalized_query):
                    has_context_score += 1
                    context_indicators.append(pattern.pattern)

            # Check for missing information (using compiled regex)
            missing_context_score = 0
            missing_indicators = []

            for pattern in self._completeness_patterns_compiled.get("missing_context", []):
                if pattern.search(normalized_query):
                    missing_context_score += 1
                    missing_indicators.append(pattern.pattern)

            # Calculate completeness score (0-1)
            if has_context_score + missing_context_score == 0:
                completeness_score = 0.5  # Neutral/moderate
            else:
                completeness_score = has_context_score / (has_context_score + missing_context_score)

            # Determine completeness level
            if completeness_score >= 0.7:
                completeness_level = "high"
                needs_clarification = False
            elif completeness_score >= 0.4:
                completeness_level = "moderate"
                needs_clarification = False
            else:
                completeness_level = "low"
                needs_clarification = True

            return {
                "completeness_score": completeness_score,
                "completeness_level": completeness_level,
                "needs_clarification": needs_clarification,
                "has_context_indicators": context_indicators,
                "missing_context_indicators": missing_indicators,
                "word_count": len(query.split()),
                "query_length": len(query)
            }

        except Exception as e:
            logger.error(f"Information completeness assessment failed: {e}")
            return {
                "completeness_score": 0.5,
                "completeness_level": "moderate",
                "needs_clarification": False,
                "error": str(e)
            }