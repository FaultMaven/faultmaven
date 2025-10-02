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


class QueryClassificationEngine(IQueryClassificationEngine):
    """Production implementation of query classification using ML and pattern matching"""
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tracer: ITracer,
        enable_llm_classification: bool = True,
        fallback_to_patterns: bool = True
    ):
        """Initialize the query classification engine

        Args:
            llm_provider: LLM provider for semantic classification
            tracer: Observability tracer
            enable_llm_classification: Whether to use LLM for classification
            fallback_to_patterns: Whether to fall back to pattern matching
        """
        self.llm_provider = llm_provider
        self.tracer = tracer
        self.enable_llm_classification = enable_llm_classification
        self.fallback_to_patterns = fallback_to_patterns

        # Initialize pattern matchers (with compiled regex for performance)
        self._init_pattern_matchers()
        self._compile_patterns()

        logger.info("QueryClassificationEngine initialized with LLM and pattern matching")
    
    def _init_pattern_matchers(self):
        """Initialize pattern matching rules for fast classification"""
        
        # Intent patterns
        self.intent_patterns = {
            QueryIntent.TROUBLESHOOTING: [
                r'\b(issue|problem|error|bug|broken|not working|fails?|failing)\b',
                r'\b(troubleshoot|debug|diagnose|fix|resolve|solve)\b',
                r'\b(why.*not|what.*wrong|how.*fix)\b'
            ],
            QueryIntent.STATUS_CHECK: [
                r'\b(status|health|state|running|up|down|available)\b',
                r'\b(check|verify|confirm|validate)\b',
                r'\bis.*working|are.*running\b'
            ],
            QueryIntent.EXPLANATION: [
                r'\b(how does|what is|explain|describe|tell me about)\b',
                r'\b(meaning|definition|purpose|reason)\b',
                r'\bwhy.*\?\s*$'
            ],
            QueryIntent.INFORMATION: [
                # Meta-queries about methodology/doctrine/processes (not actual problems)
                r'\b(what (are|is) the (steps|phases|process|methodology|doctrine))\b',
                r'\b(how does .* work)\b',
                r'\b(explain (the|how|what))\b',
                r'\b(tell me about (the|your))\b',
                r'\b(describe (the|how))\b',
                r'\b(what (should|do) (i|we) (do|follow))\b',
                # Questions about SRE practices/principles
                r'\b(sre (doctrine|principles|methodology|practices))\b',
                r'\b(troubleshooting (approach|methodology|steps|process))\b',
                r'\b(best practices?)\b',
            ],
            QueryIntent.CONFIGURATION: [
                r'\b(configure|config|setup|install|deploy)\b',
                r'\b(settings|parameters|options)\b',
                r'\bhow to (set|configure|install)\b'
            ],
            QueryIntent.MONITORING: [
                r'\b(monitor|track|watch|observe|alert)\b',
                r'\b(metrics|logs|dashboard|stats)\b',
                r'\b(performance|usage|utilization)\b'
            ],
            # Conversation intelligence intents
            QueryIntent.OFF_TOPIC: [
                r'\b(recipe|cooking|food|restaurant|eat|dinner|breakfast|lunch)\b',
                r'\b(weather|forecast|temperature|climate|rain|snow|sunny)\b',
                r'\b(movie|film|tv show|series|actor|actress|cinema)\b',
                r'\b(sports|football|basketball|soccer|baseball|game|match)\b',
                r'\b(music|song|album|artist|band|concert)\b',
                r'\b(politics|election|president|government|vote)\b',
                r'\b(travel|vacation|hotel|flight|trip|destination)\b',
                r'\b(shopping|buy|purchase|store|price|deal)\b',
                r'\b(health|medical|doctor|hospital|medicine|symptom)\b',
                r'\b(personal|family|relationship|dating|friend)\b'
            ],
            QueryIntent.META_FAULTMAVEN: [
                r'\b(what (are|is) you|who (are|is) you|what can you do)\b',
                r'\b(faultmaven|your capabilities|your features|your purpose)\b',
                r'\b(how (do|does) you work|what.*your function)\b',
                r'\b(your limitations|what.*you (can\'?t|cannot))\b',
                r'\b(about (you|faultmaven)|tell me about (yourself|faultmaven))\b'
            ],
            QueryIntent.CONVERSATION_CONTROL: [
                r'\b(start over|reset|clear|new (topic|conversation|session))\b',
                r'\b(go back|previous|undo|revert)\b',
                r'\b(skip|next|move on|continue)\b',
                r'\b(stop|quit|exit|end|cancel)\b',
                r'\b(pause|wait|hold on)\b'
            ],
            QueryIntent.GREETING: [
                r'^(hi|hello|hey|greetings|good (morning|afternoon|evening))\b',
                r'\b(how are you|how\'s it going|what\'s up)\b',
                r'^(yo|sup|howdy)\b'
            ],
            QueryIntent.GRATITUDE: [
                r'\b(thank(s| you)|thx|ty|appreciate|grateful)\b',
                r'\b(great (job|work|help)|well done|awesome|excellent)\b',
                r'\b(that (helped|worked)|perfect|exactly)\b'
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

    def _compile_patterns(self):
        """Compile regex patterns for performance optimization"""
        # Compile intent patterns
        self._intent_patterns_compiled = {}
        for intent, patterns in self.intent_patterns.items():
            self._intent_patterns_compiled[intent] = [
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

        logger.debug("Compiled regex patterns for optimized matching")

    @trace("classification_engine_classify_query")
    async def classify_query(self, query: str, context: Optional[Dict[str, Any]] = None) -> QueryClassification:
        """Classify a user query and determine processing strategy"""
        try:
            # Normalize query
            normalized_query = self._normalize_query(query)
            
            # Multi-stage classification
            results = {
                "query": query,
                "normalized_query": normalized_query,
                "classification_timestamp": datetime.utcnow().isoformat(),
                "context": context or {}
            }
            
            # Stage 1: Pattern-based classification (fast path)
            pattern_results = await self._pattern_classify(normalized_query)
            results.update(pattern_results)
            
            # Stage 2: LLM-based semantic classification (if enabled)
            if self.enable_llm_classification:
                try:
                    llm_results = await self._llm_classify(query, context)
                    results.update(llm_results)
                    results["classification_method"] = "llm_enhanced"
                except Exception as e:
                    logger.warning(f"LLM classification failed, using patterns only: {e}")
                    results["classification_method"] = "pattern_only"
            else:
                results["classification_method"] = "pattern_only"
            
            # Stage 3: Post-processing and validation
            results = self._validate_and_enhance_classification(results)

            logger.debug(f"Classified query: intent={results.get('intent')}, complexity={results.get('complexity')}")

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
                metadata=results.get("metadata", {})
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
        """Extract user intent from query"""
        try:
            normalized_query = self._normalize_query(query)
            
            # Check patterns for each intent
            intent_scores = {}
            for intent, patterns in self.intent_patterns.items():
                score = 0
                matches = []
                for pattern in patterns:
                    if re.search(pattern, normalized_query, re.IGNORECASE):
                        score += 1
                        matches.append(pattern)
                
                if score > 0:
                    intent_scores[intent.value] = {
                        "score": score,
                        "matches": matches,
                        "confidence": min(score / len(patterns), 1.0)
                    }
            
            # Determine primary intent
            if intent_scores:
                primary_intent = max(intent_scores.keys(), key=lambda k: intent_scores[k]["score"])
                confidence = intent_scores[primary_intent]["confidence"]
            else:
                primary_intent = QueryIntent.UNKNOWN.value
                confidence = 0.0
            
            return {
                "primary_intent": primary_intent,
                "confidence": confidence,
                "all_intents": intent_scores,
                "method": "pattern_matching"
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
    
    def _normalize_query(self, query: str) -> str:
        """Normalize query for pattern matching"""
        if not query:
            return ""
        
        # Basic normalization
        normalized = query.lower().strip()
        
        # Remove extra whitespace
        normalized = re.sub(r'\s+', ' ', normalized)
        
        return normalized
    
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
    
    async def _llm_classify(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform LLM-based semantic classification"""
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
    
    def _build_classification_prompt(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Build prompt for LLM classification"""
        
        context_str = ""
        if context:
            context_str = f"\nContext: {context}"
        
        prompt = f"""Classify this user query for a troubleshooting AI system:

Query: "{query}"{context_str}

Analyze and return JSON with:
1. intent: (choose one from: {', '.join(intent.value for intent in QueryIntent)})
2. complexity: (choose one from: {', '.join(level.value for level in ComplexityLevel)})
3. domain: (choose one from: {', '.join(domain.value for domain in TechnicalDomain)})
4. urgency: (choose one from: {', '.join(urgency.value for urgency in UrgencyLevel)})
5. confidence: 0.0-1.0
6. reasoning: brief explanation

Return only valid JSON."""
        
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
        """Calculate overall classification confidence"""
        confidences = []
        
        if "intent_confidence" in results:
            confidences.append(results["intent_confidence"])
        
        if "complexity_score" in results:
            # Convert complexity score to confidence (inverse relationship)
            confidences.append(1.0 - abs(results["complexity_score"] - 0.5))
        
        if "llm_confidence" in results:
            confidences.append(results["llm_confidence"])
        
        if confidences:
            return sum(confidences) / len(confidences)
        
        return 0.5  # Default moderate confidence
    
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
        elif intent == QueryIntent.MONITORING.value:
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