"""log_processor.py

Purpose: Analyze log data to extract insights

Requirements:
--------------------------------------------------------------------------------
• Parse unstructured logs with Grok patterns
• Apply anomaly detection algorithms
• Return ProcessorResult with summary
• Context-aware processing using agent state

Key Components:
--------------------------------------------------------------------------------
  class LogProcessor(BaseProcessor): ...
  def _parse_logs_to_dataframe(content: str)

Technology Stack:
--------------------------------------------------------------------------------
pandas, PyOD, scikit-learn

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
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict, Any, Optional

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from ..models import DataInsightsResponse, DataType, AgentState


class LogProcessor:
    """Processes log files to extract insights and detect anomalies with context-aware analysis"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # Common log patterns for parsing
        self.log_patterns = {
            'timestamp': [
                r'(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)',
                r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})',
                r'(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})',
            ],
            'log_level': [
                r'\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b',
            ],
            'http_status': [
                r'\b(2\d{2}|3\d{2}|4\d{2}|5\d{2})\b',
            ],
            'ip_address': [
                r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
            ],
            'error_code': [
                r'\b[A-Z_]+_ERROR\b',
                r'\b[A-Z_]+_EXCEPTION\b',
            ],
            'duration': [
                r'(\d+(?:\.\d+)?)\s*(?:ms|s|seconds?)',
            ]
        }
        
        # Compile patterns
        self.compiled_patterns = {}
        for category, patterns in self.log_patterns.items():
            self.compiled_patterns[category] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]
    
    async def process(
        self, content: str, data_id: str, agent_state: AgentState
    ) -> DataInsightsResponse:
        """
        Process log content and extract insights with context-aware analysis
        
        Args:
            content: Raw log content
            data_id: Identifier for the data
            agent_state: Current agent state for context-aware processing
            
        Returns:
            DataInsightsResponse with extracted insights
        """
        start_time = datetime.utcnow()
        
        try:
            # Parse logs into structured format
            df = self._parse_logs_to_dataframe(content)
            
            if df.empty:
                return DataInsightsResponse(
                    data_id=data_id,
                    data_type=DataType.LOG_FILE,
                    insights={'error': 'No valid log entries found'},
                    confidence_score=0.0,
                    processing_time_ms=0,
                    anomalies_detected=[],
                    recommendations=[]
                )
            
            # Extract basic insights with context awareness
            insights = self._extract_basic_insights(df, agent_state)
            
            # Detect anomalies
            anomalies = self._detect_anomalies(df)
            
            # Generate context-aware recommendations
            recommendations = self._generate_recommendations(insights, anomalies, agent_state)
            
            # Calculate confidence score
            confidence = self._calculate_confidence(df, insights, anomalies)
            
            # Calculate processing time
            processing_time = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            return DataInsightsResponse(
                data_id=data_id,
                data_type=DataType.LOG_FILE,
                insights=insights,
                confidence_score=confidence,
                processing_time_ms=processing_time,
                anomalies_detected=anomalies,
                recommendations=recommendations
            )
            
        except Exception as e:
            self.logger.error(f"Log processing failed: {e}")
            return DataInsightsResponse(
                data_id=data_id,
                data_type=DataType.LOG_FILE,
                insights={'error': str(e)},
                confidence_score=0.0,
                processing_time_ms=0,
                anomalies_detected=[],
                recommendations=[]
            )
    
    def _parse_logs_to_dataframe(self, content: str) -> pd.DataFrame:
        """
        Parse unstructured log content into a structured DataFrame
        
        Args:
            content: Raw log content
            
        Returns:
            DataFrame with parsed log entries
        """
        lines = content.strip().split('\n')
        parsed_entries = []
        
        for line_num, line in enumerate(lines, 1):
            if not line.strip():
                continue
            
            entry = self._parse_log_line(line, line_num)
            if entry:
                parsed_entries.append(entry)
        
        return pd.DataFrame(parsed_entries)
    
    def _parse_log_line(self, line: str, line_num: int) -> Optional[Dict[str, Any]]:
        """
        Parse a single log line
        
        Args:
            line: Single log line
            line_num: Line number for reference
            
        Returns:
            Parsed log entry dictionary
        """
        entry = {
            'line_number': line_num,
            'raw_line': line,
            'timestamp': None,
            'log_level': None,
            'message': line,
            'http_status': None,
            'ip_address': None,
            'error_code': None,
            'duration_ms': None
        }
        
        # Extract timestamp
        for pattern in self.compiled_patterns['timestamp']:
            match = pattern.search(line)
            if match:
                entry['timestamp'] = match.group(1)
                break
        
        # Extract log level
        for pattern in self.compiled_patterns['log_level']:
            match = pattern.search(line)
            if match:
                entry['log_level'] = match.group(1).upper()
                break
        
        # Extract HTTP status codes
        for pattern in self.compiled_patterns['http_status']:
            match = pattern.search(line)
            if match:
                entry['http_status'] = int(match.group(1))
                break
        
        # Extract IP addresses
        for pattern in self.compiled_patterns['ip_address']:
            match = pattern.search(line)
            if match:
                entry['ip_address'] = match.group(0)
                break
        
        # Extract error codes
        for pattern in self.compiled_patterns['error_code']:
            match = pattern.search(line)
            if match:
                entry['error_code'] = match.group(0)
                break
        
        # Extract duration
        for pattern in self.compiled_patterns['duration']:
            match = pattern.search(line)
            if match:
                duration_str = match.group(1)
                try:
                    duration = float(duration_str)
                    if 'ms' in line.lower():
                        entry['duration_ms'] = duration
                    else:
                        entry['duration_ms'] = duration * 1000  # Convert to ms
                except ValueError:
                    pass
                break
        
        return entry
    
    def _extract_basic_insights(
        self, df: pd.DataFrame, agent_state: AgentState
    ) -> Dict[str, Any]:
        """
        Extract basic insights from parsed log data with context awareness
        
        Args:
            df: Parsed log DataFrame
            agent_state: Current agent state for context-aware processing
            
        Returns:
            Dictionary of insights
        """
        insights = {
            'total_entries': len(df),
            'time_range': None,
            'log_level_distribution': {},
            'error_summary': {},
            'performance_metrics': {},
            'top_errors': [],
            'unique_ips': 0,
            'contextual_analysis': {}
        }
        
        # Extract context keywords from agent state
        context_keywords = []
        investigation_context = agent_state.get('investigation_context', {})
        
        # Get keywords from various context sources
        if 'keywords' in investigation_context:
            context_keywords.extend(investigation_context['keywords'])
        if 'services' in investigation_context:
            context_keywords.extend(investigation_context['services'])
        if 'components' in investigation_context:
            context_keywords.extend(investigation_context['components'])
        
        # Extract keywords from user query
        user_query = agent_state.get('user_query', '')
        if user_query:
            # Simple keyword extraction from user query
            query_words = [
                word.lower() for word in user_query.split()
                if len(word) > 3 and word.lower() not in [
                    'the', 'and', 'for', 'with', 'that', 'this'
                ]
            ]
            context_keywords.extend(query_words)
        
        # Remove duplicates and empty strings
        context_keywords = list(set([kw for kw in context_keywords if kw]))
        
        # Time range analysis
        if 'timestamp' in df.columns and not df['timestamp'].isna().all():
            timestamps = []
            for ts in df['timestamp'].dropna():
                try:
                    # Try different timestamp formats
                    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%m/%d/%Y %H:%M:%S']:
                        try:
                            parsed_ts = datetime.strptime(str(ts), fmt)
                            timestamps.append(parsed_ts)
                            break
                        except ValueError:
                            continue
                except Exception:
                    continue
            
            if timestamps:
                insights['time_range'] = {
                    'start': min(timestamps).isoformat(),
                    'end': max(timestamps).isoformat(),
                    'duration_hours': (max(timestamps) - min(timestamps)).total_seconds() / 3600
                }
        
        # Log level distribution
        if 'log_level' in df.columns:
            level_counts = df['log_level'].value_counts().to_dict()
            insights['log_level_distribution'] = level_counts
        
        # Context-aware error analysis
        error_entries = df[df['log_level'].isin(['ERROR', 'FATAL', 'CRITICAL'])]
        insights['error_summary'] = {
            'total_errors': len(error_entries),
            'error_rate': len(error_entries) / len(df) if len(df) > 0 else 0
        }
        
        # Contextual analysis - prioritize logs matching investigation context
        if context_keywords and 'raw_line' in df.columns:
            # Create a case-insensitive regex pattern for context keywords
            context_pattern = '|'.join(re.escape(kw) for kw in context_keywords)
            context_mask = df['raw_line'].str.contains(context_pattern, case=False, na=False)
            contextual_logs = df[context_mask]
            
            insights['contextual_analysis'] = {
                'context_keywords': context_keywords,
                'contextual_entries': len(contextual_logs),
                'contextual_percentage': len(contextual_logs) / len(df) * 100 if len(df) > 0 else 0
            }
            
            # Prioritize contextual errors
            contextual_errors = contextual_logs[contextual_logs['log_level'].isin(['ERROR', 'FATAL', 'CRITICAL'])]
            if len(contextual_errors) > 0:
                insights['contextual_analysis']['contextual_errors'] = len(contextual_errors)
                insights['contextual_analysis']['contextual_error_rate'] = len(contextual_errors) / len(contextual_logs) * 100
                
                # Extract top contextual error messages
                insights['contextual_analysis']['top_contextual_errors'] = (
                    contextual_errors['raw_line'].head(5).tolist()
                )
            
            # Contextual performance analysis
            if 'duration_ms' in contextual_logs.columns:
                contextual_durations = contextual_logs['duration_ms'].dropna()
                if len(contextual_durations) > 0:
                    insights['contextual_analysis']['contextual_performance'] = {
                        'avg_response_time_ms': contextual_durations.mean(),
                        'max_response_time_ms': contextual_durations.max(),
                        'count': len(contextual_durations)
                    }
        
        # HTTP status analysis
        if 'http_status' in df.columns:
            status_counts = df['http_status'].value_counts().to_dict()
            insights['http_status_distribution'] = status_counts
        
        # Performance metrics
        if 'duration_ms' in df.columns:
            durations = df['duration_ms'].dropna()
            if len(durations) > 0:
                insights['performance_metrics'] = {
                    'avg_response_time_ms': durations.mean(),
                    'max_response_time_ms': durations.max(),
                    'min_response_time_ms': durations.min(),
                    'p95_response_time_ms': durations.quantile(0.95)
                }
        
        # Top errors
        if 'error_code' in df.columns:
            error_codes = df['error_code'].value_counts().head(5).to_dict()
            insights['top_errors'] = list(error_codes.keys())
        
        # Unique IPs
        if 'ip_address' in df.columns:
            insights['unique_ips'] = df['ip_address'].nunique()
        
        return insights
    
    def _detect_anomalies(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Detect anomalies in log data
        
        Args:
            df: Parsed log DataFrame
            
        Returns:
            List of detected anomalies
        """
        anomalies = []
        
        # 1. Error rate anomalies
        if len(df) > 10:
            error_entries = df[df['log_level'].isin(['ERROR', 'FATAL', 'CRITICAL'])]
            error_rate = len(error_entries) / len(df)
            
            if error_rate > 0.1:  # More than 10% errors
                anomalies.append({
                    'type': 'high_error_rate',
                    'severity': 'high' if error_rate > 0.2 else 'medium',
                    'description': f'Error rate is {error_rate:.2%}',
                    'value': error_rate,
                    'threshold': 0.1
                })
        
        # 2. Performance anomalies
        if 'duration_ms' in df.columns:
            durations = df['duration_ms'].dropna()
            if len(durations) > 5:
                # Use Isolation Forest for outlier detection
                try:
                    scaler = StandardScaler()
                    scaled_durations = scaler.fit_transform(durations.values.reshape(-1, 1))
                    
                    iso_forest = IsolationForest(contamination=0.1, random_state=42)
                    predictions = iso_forest.fit_predict(scaled_durations)
                    
                    outlier_indices = np.where(predictions == -1)[0]
                    if len(outlier_indices) > 0:
                        outlier_durations = durations.iloc[outlier_indices]
                        
                        for idx, duration in outlier_durations.items():
                            anomalies.append({
                                'type': 'performance_outlier',
                                'severity': 'high' if duration > durations.quantile(0.95) else 'medium',
                                'description': f'Unusually slow response: {duration:.2f}ms',
                                'value': duration,
                                'line_number': df.iloc[idx]['line_number'] if 'line_number' in df.columns else None
                            })
                except Exception as e:
                    self.logger.warning(f"Performance anomaly detection failed: {e}")
        
        # 3. HTTP status anomalies
        if 'http_status' in df.columns:
            status_counts = df['http_status'].value_counts()
            error_statuses = status_counts[status_counts.index >= 400]
            
            for status, count in error_statuses.items():
                if count > len(df) * 0.05:  # More than 5% of requests
                    anomalies.append({
                        'type': 'http_error_spike',
                        'severity': 'high' if status >= 500 else 'medium',
                        'description': f'High rate of HTTP {status} errors: {count} occurrences',
                        'value': count,
                        'status_code': status
                    })
        
        # 4. Temporal anomalies (if timestamps available)
        if 'timestamp' in df.columns and not df['timestamp'].isna().all():
            # Simple temporal clustering for now
            # In a real implementation, you might use more sophisticated time series analysis
            pass
        
        return anomalies
    
    def _generate_recommendations(self, insights: Dict[str, Any], anomalies: List[Dict[str, Any]], agent_state: AgentState) -> List[str]:
        """
        Generate context-aware recommendations based on insights, anomalies, and agent state
        
        Args:
            insights: Extracted insights
            anomalies: Detected anomalies
            agent_state: Current agent state for context-aware recommendations
            
        Returns:
            List of recommendations
        """
        recommendations = []
        current_phase = agent_state.get('current_phase', '')
        investigation_context = agent_state.get('investigation_context', {})
        
        # Phase-specific recommendations
        if current_phase == 'define_blast_radius':
            if insights.get('contextual_analysis', {}).get('contextual_entries', 0) > 0:
                contextual_pct = insights['contextual_analysis']['contextual_percentage']
                recommendations.append(
                    f"Blast radius analysis: {contextual_pct:.1f}% of log entries are related to your investigation context. "
                    f"Focus on these {insights['contextual_analysis']['contextual_entries']} entries."
                )
            
            # Time range for blast radius
            if insights.get('time_range'):
                recommendations.append(
                    f"Time range affected: {insights['time_range']['start']} to {insights['time_range']['end']} "
                    f"({insights['time_range']['duration_hours']:.1f} hours)"
                )
        
        elif current_phase == 'establish_timeline':
            if insights.get('time_range'):
                recommendations.append(
                    f"Timeline established: Logs span from {insights['time_range']['start']} to {insights['time_range']['end']}. "
                    "Correlate this with deployment events, configuration changes, or external incidents."
                )
            
            # Contextual timeline analysis
            contextual_analysis = insights.get('contextual_analysis', {})
            if contextual_analysis.get('contextual_entries', 0) > 0:
                recommendations.append(
                    f"Found {contextual_analysis['contextual_entries']} relevant log entries. "
                    "Review these for timeline correlation with the reported issue."
                )
        
        elif current_phase == 'formulate_hypothesis':
            # Context-aware hypothesis suggestions
            contextual_analysis = insights.get('contextual_analysis', {})
            if contextual_analysis.get('contextual_errors', 0) > 0:
                recommendations.append(
                    f"Hypothesis: {contextual_analysis['contextual_errors']} errors found in context-relevant logs. "
                    "This suggests the issue is related to the components you're investigating."
                )
            
            # Performance-based hypothesis
            if contextual_analysis.get('contextual_performance'):
                avg_time = contextual_analysis['contextual_performance']['avg_response_time_ms']
                max_time = contextual_analysis['contextual_performance']['max_response_time_ms']
                recommendations.append(
                    f"Hypothesis: Performance degradation detected (avg: {avg_time:.0f}ms, max: {max_time:.0f}ms). "
                    "Consider resource constraints or downstream dependencies."
                )
        
        elif current_phase == 'validate_hypothesis':
            # Provide validation guidance based on context
            contextual_analysis = insights.get('contextual_analysis', {})
            if contextual_analysis.get('top_contextual_errors'):
                recommendations.append(
                    "Validation: Review these specific error messages for hypothesis confirmation:"
                )
                for i, error in enumerate(contextual_analysis['top_contextual_errors'][:3], 1):
                    recommendations.append(f"  {i}. {error[:100]}...")
        
        elif current_phase == 'propose_solution':
            # Solution-oriented recommendations
            error_rate = insights.get('error_summary', {}).get('error_rate', 0)
            if error_rate > 0.1:
                recommendations.append(
                    f"Solution: Address the {error_rate:.1%} error rate. "
                    "Consider implementing circuit breakers, retry mechanisms, or scaling resources."
                )
            
            # Context-specific solutions
            contextual_analysis = insights.get('contextual_analysis', {})
            if contextual_analysis.get('contextual_errors', 0) > 0:
                recommendations.append(
                    "Solution: Focus remediation efforts on the context-relevant errors identified. "
                    "These are most likely related to the reported issue."
                )
        
        # General error rate recommendations
        if insights.get('error_summary', {}).get('error_rate', 0) > 0.1:
            recommendations.append(
                "High error rate detected. Review application logs for root causes."
            )
        
        # Performance recommendations
        perf_metrics = insights.get('performance_metrics', {})
        if perf_metrics.get('avg_response_time_ms', 0) > 1000:
            recommendations.append(
                "Average response time is high. Consider performance optimization."
            )
        
        # Anomaly-based recommendations
        for anomaly in anomalies:
            if anomaly['type'] == 'high_error_rate':
                recommendations.append(
                    "Investigate the high error rate immediately."
                )
            elif anomaly['type'] == 'performance_outlier':
                recommendations.append(
                    "Review the identified slow requests for optimization."
                )
            elif anomaly['type'] == 'http_error_spike':
                recommendations.append(
                    f"Investigate HTTP {anomaly.get('status_code')} errors."
                )
        
        # Context-aware general recommendations
        contextual_analysis = insights.get('contextual_analysis', {})
        if contextual_analysis.get('contextual_entries', 0) == 0 and contextual_analysis.get('context_keywords'):
            recommendations.append(
                f"No logs found matching your investigation context ({', '.join(contextual_analysis['context_keywords'])}). "
                "Consider expanding the search scope or reviewing log completeness."
            )
        
        # Default recommendation if no issues found
        if len(anomalies) == 0 and insights.get('error_summary', {}).get('error_rate', 0) < 0.05:
            recommendations.append("No significant anomalies detected in the logs.")
        
        return recommendations
    
    def _calculate_confidence(self, df: pd.DataFrame, insights: Dict[str, Any], anomalies: List[Dict[str, Any]]) -> float:
        """
        Calculate confidence score for the analysis
        
        Args:
            df: Parsed log DataFrame
            insights: Extracted insights
            anomalies: Detected anomalies
            
        Returns:
            Confidence score between 0.0 and 1.0
        """
        confidence = 0.5  # Base confidence
        
        # Increase confidence based on data quality
        if len(df) > 100:
            confidence += 0.2
        elif len(df) > 10:
            confidence += 0.1
        
        # Increase confidence if we have timestamps
        if insights.get('time_range'):
            confidence += 0.1
        
        # Increase confidence if we have log levels
        if insights.get('log_level_distribution'):
            confidence += 0.1
        
        # Increase confidence if we have performance metrics
        if insights.get('performance_metrics'):
            confidence += 0.1
        
        # Decrease confidence if we have many anomalies (might indicate parsing issues)
        if len(anomalies) > 10:
            confidence -= 0.1
        
        return min(1.0, max(0.0, confidence))

