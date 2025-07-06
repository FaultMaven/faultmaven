"""classifier.py

Purpose: Classify user-submitted data

Requirements:
--------------------------------------------------------------------------------
• Heuristic-based checks for common formats
• LLM-based classification fallback
• Output DataType enum values

Key Components:
--------------------------------------------------------------------------------
  class DataClassifier: classify(content: str) -> DataType
  def _heuristic_classify(content: str)

Technology Stack:
--------------------------------------------------------------------------------
PyYAML, Regex, LLMRouter

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import re
import logging
import yaml
import json

from ..models import DataType
from ..llm.router import LLMRouter


class DataClassifier:
    """Classifies data content into appropriate DataType categories"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.llm_router = LLMRouter()
        
        # Heuristic patterns for classification
        self.patterns = {
            DataType.LOG_FILE: [
                # Timestamp patterns
                r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}',
                r'\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}',
                # Log level indicators
                r'\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b',
                # Common log prefixes
                r'^\d{4}-\d{2}-\d{2}',
                r'^\[\d{4}-\d{2}-\d{2}',
                # Log file extensions
                r'\.(log|txt)$',
            ],
            DataType.ERROR_MESSAGE: [
                # Error keywords
                r'\b(error|exception|failed|failure|crash|abort)\b',
                # Exception patterns
                r'Exception:',
                r'Error:',
                r'Traceback \(most recent call last\):',
                # HTTP error codes
                r'\b(4\d{2}|5\d{2})\b',
            ],
            DataType.STACK_TRACE: [
                # Stack trace patterns
                r'at\s+[\w\.$<>]+\([^)]*\)',
                r'Traceback \(most recent call last\):',
                r'File "[^"]+", line \d+',
                r'^\s+File\s+"[^"]+"',
                r'^\s+at\s+',
            ],
            DataType.METRICS_DATA: [
                # JSON metrics
                r'\{[^{}]*"metric[s]?":',
                r'\{[^{}]*"value":\s*\d+',
                # Prometheus format
                r'^\w+{[^}]*}\s+\d+\.?\d*',
                # Graphite format
                r'^\w+\.\w+\s+\d+\.?\d*\s+\d+',
                # CSV with numeric data
                r'^\d+\.?\d*,\d+\.?\d*,\d+\.?\d*',
            ],
            DataType.CONFIG_FILE: [
                # YAML indicators
                r'^---\s*$',
                r'^\w+:\s*$',
                r'^\s+-\s+\w+:',
                # JSON config
                r'\{[^{}]*"config":',
                r'\{[^{}]*"settings":',
                # INI format
                r'^\[[^\]]+\]',
                r'^\w+\s*=\s*[^=]+$',
                # Environment variables
                r'^\w+=\w+$',
            ],
            DataType.DOCUMENTATION: [
                # Markdown
                r'^#\s+\w+',
                r'^\*\*[^*]+\*\*',
                r'^\[[^\]]+\]\([^)]+\)',
                # Documentation keywords
                r'\b(guide|manual|documentation|tutorial|help)\b',
                # HTML tags
                r'<[^>]+>',
                # Code blocks
                r'```\w*',
            ]
        }
        
        # Compile patterns for efficiency
        self.compiled_patterns = {}
        for data_type, pattern_list in self.patterns.items():
            self.compiled_patterns[data_type] = [
                re.compile(pattern, re.IGNORECASE | re.MULTILINE)
                for pattern in pattern_list
            ]
    
    async def classify(self, content: str) -> DataType:
        """
        Classify data content into appropriate DataType
        
        Args:
            content: Raw content to classify
            
        Returns:
            DataType enum value
        """
        if not content or not isinstance(content, str):
            return DataType.UNKNOWN
        
        # Try heuristic classification first
        heuristic_result = self._heuristic_classify(content)
        if heuristic_result != DataType.UNKNOWN:
            self.logger.info(f"Heuristic classification: {heuristic_result}")
            return heuristic_result
        
        # Fallback to LLM-based classification
        try:
            llm_result = await self._llm_classify(content)
            self.logger.info(f"LLM classification: {llm_result}")
            return llm_result
        except Exception as e:
            self.logger.warning(f"LLM classification failed: {e}")
            return DataType.UNKNOWN
    
    def _heuristic_classify(self, content: str) -> DataType:
        """
        Perform heuristic-based classification using regex patterns
        
        Args:
            content: Content to classify
            
        Returns:
            DataType enum value
        """
        # Calculate confidence scores for each data type
        scores = {}
        
        for data_type, patterns in self.compiled_patterns.items():
            score = 0
            for pattern in patterns:
                matches = pattern.findall(content)
                score += len(matches)
            
            # Normalize score by content length
            if len(content) > 0:
                score = score / len(content) * 1000  # Scale up for readability
            
            scores[data_type] = score
        
        # Find the data type with highest score
        if scores:
            best_type = max(scores.items(), key=lambda x: x[1])
            if best_type[1] > 0.1:  # Threshold for confidence
                return best_type[0]
        
        # Additional heuristics for specific formats
        if self._is_json(content):
            return DataType.CONFIG_FILE
        elif self._is_yaml(content):
            return DataType.CONFIG_FILE
        elif self._is_csv_with_metrics(content):
            return DataType.METRICS_DATA
        
        return DataType.UNKNOWN
    
    def _is_json(self, content: str) -> bool:
        """Check if content is valid JSON"""
        try:
            json.loads(content)
            return True
        except (json.JSONDecodeError, ValueError):
            return False
    
    def _is_yaml(self, content: str) -> bool:
        """Check if content is valid YAML"""
        try:
            yaml.safe_load(content)
            return True
        except (yaml.YAMLError, ValueError):
            return False
    
    def _is_csv_with_metrics(self, content: str) -> bool:
        """Check if content is CSV with numeric metrics"""
        lines = content.strip().split('\n')
        if len(lines) < 2:
            return False
        
        # Check if first few lines contain numeric data
        numeric_lines = 0
        for line in lines[:5]:
            if re.match(r'^[^,]*,\d+\.?\d*', line):
                numeric_lines += 1
        
        return numeric_lines >= 2
    
    async def _llm_classify(self, content: str) -> DataType:
        """
        Use LLM to classify content when heuristics fail
        
        Args:
            content: Content to classify
            
        Returns:
            DataType enum value
        """
        # Create classification prompt
        prompt = f"""
        Classify the following data content into one of these categories:
        - log_file: System or application logs with timestamps
        - error_message: Error messages or exception details
        - stack_trace: Stack traces from programming languages
        - metrics_data: Performance metrics, monitoring data
        - config_file: Configuration files (YAML, JSON, INI, etc.)
        - documentation: Documentation, guides, manuals
        
        Content to classify:
        {content[:1000]}  # Limit content length
        
        Respond with only the category name (e.g., "log_file").
        """
        
        try:
            response = await self.llm_router.route(
                prompt=prompt,
                max_tokens=50,
                temperature=0.1  # Low temperature for classification
            )
            
            # Parse response
            category = response.content.strip().lower()
            
            # Map response to DataType
            category_mapping = {
                'log_file': DataType.LOG_FILE,
                'error_message': DataType.ERROR_MESSAGE,
                'stack_trace': DataType.STACK_TRACE,
                'metrics_data': DataType.METRICS_DATA,
                'config_file': DataType.CONFIG_FILE,
                'documentation': DataType.DOCUMENTATION,
            }
            
            return category_mapping.get(category, DataType.UNKNOWN)
            
        except Exception as e:
            self.logger.error(f"LLM classification failed: {e}")
            return DataType.UNKNOWN
    
    def get_classification_confidence(self, content: str, data_type: DataType) -> float:
        """
        Get confidence score for a specific classification
        
        Args:
            content: Content that was classified
            data_type: The DataType to check confidence for
            
        Returns:
            Confidence score between 0.0 and 1.0
        """
        if data_type not in self.compiled_patterns:
            return 0.0
        
        patterns = self.compiled_patterns[data_type]
        total_matches = 0
        
        for pattern in patterns:
            matches = pattern.findall(content)
            total_matches += len(matches)
        
        # Normalize by content length and pattern count
        if len(content) > 0 and patterns:
            confidence = min(1.0, total_matches / (len(content) / 1000))
            return confidence
        
        return 0.0

