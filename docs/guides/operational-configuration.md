# FaultMaven Operational Configuration Guide

**Version**: 1.0  
**Last Updated**: 2025-10-11  
**Status**: Operational Guide  
**Source**: Created from investigation framework operational patterns

---

## Overview

This guide provides configuration, deployment, and monitoring guidance for FaultMaven's investigation framework in production environments.

---

## Table of Contents

1. [System Requirements](#system-requirements)
2. [Configuration](#configuration)
3. [Monitoring & Observability](#monitoring--observability)
4. [Deployment Considerations](#deployment-considerations)

---

## System Requirements

### Runtime Requirements

```yaml
runtime:
  python_version: "3.10+"
  memory: "2GB minimum, 4GB recommended"
  cpu: "2 cores minimum, 4 cores recommended"
  
dependencies:
  # LLM Providers (at least one required)
  - openai >= 1.0.0
  - anthropic >= 0.5.0
  
  # Core Dependencies
  - pydantic >= 2.0.0
  - fastapi >= 0.100.0
  - redis >= 4.0.0
  
  # Optional but Recommended
  - prometheus-client  # For monitoring
  - sentry-sdk  # For error tracking
  - opik-sdk  # For LLM observability
```

### External Services

| Service | Purpose | Required |
|---------|---------|----------|
| Redis | Session storage, state persistence, memory cache | ✅ Yes |
| PostgreSQL | Investigation history, case records | ⚠️ Optional (fallback to Redis) |
| ChromaDB | Vector storage for knowledge base | ✅ Yes |
| Presidio | PII redaction | ✅ Yes |
| Opik | LLM observability | ⚠️ Optional |

---

## Configuration

### Investigation Framework Configuration

```python
@dataclass
class InvestigationConfig:
    """Configuration for investigation framework"""
    
    # OODA Settings
    max_ooda_iterations: int = 10
    """Maximum OODA iterations before escalation"""
    
    progress_stall_threshold: int = 3
    """Iterations without progress before stall detection"""
    
    anchoring_detection_threshold: int = 4
    """Tests of same category before forcing alternatives"""
    
    confidence_decay_factor: float = 0.85
    """Decay factor for hypothesis confidence (per iteration)"""
    
    # Memory Settings
    hot_memory_size: int = 2
    """Number of iterations kept in hot memory (full fidelity)"""
    
    warm_memory_size: int = 3
    """Number of iterations in warm memory (summarized)"""
    
    max_conversation_history: int = 50
    """Maximum conversation turns to retain"""
    
    memory_compression_interval: int = 3
    """Compress memory every N turns"""
    
    # Escalation Settings
    auto_escalate_after_iterations: int = 10
    """Auto-recommend escalation after N iterations"""
    
    auto_escalate_on_mitigation_failures: int = 3
    """Auto-recommend escalation after N failed mitigations"""
    
    # Phase Transition Settings
    phase_0_timeout_turns: int = 10
    """Max turns in Phase 0 (Intake) before timeout"""
    
    require_phase_confirmation: bool = True
    """Require user confirmation for phase transitions"""
    
    allow_phase_skipping: bool = True
    """Allow skipping phases for critical incidents"""
    
    # Evidence Settings
    max_evidence_requests_per_turn: int = 3
    """Maximum evidence requests per turn"""
    
    evidence_coverage_threshold: float = 0.7
    """Minimum coverage before phase advancement"""
    
    blocked_evidence_escalation_threshold: int = 3
    """Escalate after N critical evidence blocked"""
```

### LLM Configuration

```python
@dataclass
class LLMConfig:
    """LLM provider configuration"""
    
    # Provider Settings
    llm_provider: str = "openai"  # or "anthropic", "fireworks"
    llm_model: str = "gpt-4-turbo"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2000
    llm_timeout_seconds: int = 30
    
    # Retry Settings
    max_retries: int = 3
    retry_base_delay: float = 2.0  # seconds
    retry_exponential_base: float = 2.0
    
    # Fallback Settings
    enable_fallback: bool = True
    fallback_provider: str = "anthropic"
    fallback_model: str = "claude-3-sonnet"
```

### Persistence Configuration

```python
@dataclass
class PersistenceConfig:
    """Data persistence configuration"""
    
    # Redis Settings
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    redis_ssl: bool = False
    
    # Session TTL
    session_ttl_seconds: int = 3600  # 1 hour
    hot_memory_ttl_seconds: int = 3600  # 1 hour
    warm_memory_ttl_seconds: int = 7200  # 2 hours
    cold_memory_ttl_seconds: int = 86400  # 24 hours
    
    # State Persistence
    save_state_on_each_turn: bool = True
    async_state_save: bool = True
    compress_state_in_redis: bool = True
    
    # Backup Settings
    enable_postgres_backup: bool = False
    postgres_dsn: Optional[str] = None
```

### Performance Settings

```python
@dataclass
class PerformanceConfig:
    """Performance and optimization settings"""
    
    # Caching
    enable_caching: bool = True
    cache_ttl_seconds: int = 3600
    
    # Concurrency
    max_concurrent_investigations: int = 100
    max_concurrent_llm_requests: int = 10
    
    # Token Management
    enable_token_counting: bool = True
    token_budget_per_turn: int = 4000
    warn_at_token_percentage: float = 0.8  # 80%
    
    # Memory Management
    enable_automatic_compression: bool = True
    compression_target_tokens: int = 1600
    max_uncompressed_tokens: int = 3000
```

### Environment Variables

```bash
# LLM Configuration
export LLM_PROVIDER="openai"
export LLM_MODEL="gpt-4-turbo"
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# Investigation Settings
export MAX_OODA_ITERATIONS=10
export ANCHORING_THRESHOLD=4
export AUTO_ESCALATE_ITERATIONS=10

# Memory Settings
export HOT_MEMORY_SIZE=2
export WARM_MEMORY_SIZE=3
export MEMORY_COMPRESSION_INTERVAL=3

# Redis Configuration
export REDIS_HOST="localhost"
export REDIS_PORT=6379
export REDIS_PASSWORD=""
export SESSION_TTL_SECONDS=3600

# Performance Settings
export MAX_CONCURRENT_INVESTIGATIONS=100
export ENABLE_CACHING=true
export TOKEN_BUDGET_PER_TURN=4000

# Monitoring
export ENABLE_METRICS=true
export ENABLE_TRACING=true
export SENTRY_DSN="https://..."
export LOG_LEVEL="INFO"
```

---

## Monitoring & Observability

### Prometheus Metrics

```python
class InvestigationMetrics:
    """Prometheus metrics for investigation framework"""
    
    def __init__(self):
        # Investigation Counters
        self.investigation_counter = Counter(
            'faultmaven_investigations_total',
            'Total investigations started',
            ['engagement_mode', 'investigation_strategy']
        )
        
        self.investigation_completed = Counter(
            'faultmaven_investigations_completed_total',
            'Investigations completed',
            ['completion_type']  # resolved, escalated, abandoned
        )
        
        # Phase Metrics
        self.phase_duration = Histogram(
            'faultmaven_phase_duration_seconds',
            'Time spent in each investigation phase',
            ['phase_number', 'phase_name']
        )
        
        self.phase_transitions = Counter(
            'faultmaven_phase_transitions_total',
            'Phase transitions',
            ['from_phase', 'to_phase']
        )
        
        # OODA Metrics
        self.ooda_iterations = Histogram(
            'faultmaven_ooda_iterations',
            'Number of OODA iterations per investigation',
            ['investigation_strategy']
        )
        
        self.ooda_step_duration = Histogram(
            'faultmaven_ooda_step_duration_seconds',
            'Duration of OODA steps',
            ['step_name']
        )
        
        # Success Metrics
        self.root_cause_identified = Counter(
            'faultmaven_root_cause_identified_total',
            'Investigations where root cause was identified',
            ['confidence_level']  # high (>0.8), medium (0.6-0.8), low (<0.6)
        )
        
        self.escalation_rate = Counter(
            'faultmaven_escalations_total',
            'Investigations escalated',
            ['escalation_reason']
        )
        
        # Evidence Metrics
        self.evidence_requests = Histogram(
            'faultmaven_evidence_requests',
            'Evidence requests per investigation'
        )
        
        self.evidence_coverage = Histogram(
            'faultmaven_evidence_coverage',
            'Evidence coverage score (0.0-1.0)'
        )
        
        self.evidence_blocked = Counter(
            'faultmaven_evidence_blocked_total',
            'Evidence requests blocked',
            ['category']
        )
        
        # Hypothesis Metrics
        self.hypotheses_generated = Histogram(
            'faultmaven_hypotheses_generated',
            'Hypotheses generated per investigation'
        )
        
        self.anchoring_detected = Counter(
            'faultmaven_anchoring_detected_total',
            'Anchoring bias detected'
        )
        
        # LLM Metrics
        self.llm_latency = Histogram(
            'faultmaven_llm_latency_seconds',
            'LLM API call latency',
            ['provider', 'model']
        )
        
        self.llm_errors = Counter(
            'faultmaven_llm_errors_total',
            'LLM API errors',
            ['provider', 'error_type']
        )
        
        # Memory Metrics
        self.memory_compression_duration = Histogram(
            'faultmaven_memory_compression_duration_seconds',
            'Memory compression duration'
        )
        
        self.memory_token_count = Histogram(
            'faultmaven_memory_tokens',
            'Token count after compression',
            ['tier']  # hot, warm, cold
        )
    
    def record_investigation_start(self, engagement_mode: str, strategy: str):
        """Record new investigation"""
        self.investigation_counter.labels(
            engagement_mode=engagement_mode,
            investigation_strategy=strategy
        ).inc()
    
    def record_phase_duration(self, phase: int, phase_name: str, duration: float):
        """Record time spent in phase"""
        self.phase_duration.labels(
            phase_number=str(phase),
            phase_name=phase_name
        ).observe(duration)
    
    def record_root_cause_found(self, confidence: float):
        """Record root cause identification"""
        if confidence >= 0.8:
            level = "high"
        elif confidence >= 0.6:
            level = "medium"
        else:
            level = "low"
        
        self.root_cause_identified.labels(confidence_level=level).inc()
    
    def record_escalation(self, reason: str):
        """Record escalation"""
        self.escalation_rate.labels(escalation_reason=reason).inc()
```

### Health Checks

```python
async def health_check() -> Dict:
    """Comprehensive health check"""
    
    health = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {}
    }
    
    # Check Redis
    try:
        await redis_client.ping()
        health["components"]["redis"] = {"status": "healthy"}
    except Exception as e:
        health["components"]["redis"] = {"status": "unhealthy", "error": str(e)}
        health["status"] = "degraded"
    
    # Check LLM Provider
    try:
        await llm_client.health_check()
        health["components"]["llm"] = {"status": "healthy"}
    except Exception as e:
        health["components"]["llm"] = {"status": "unhealthy", "error": str(e)}
        health["status"] = "degraded"
    
    # Check Investigation Metrics
    metrics = get_investigation_metrics()
    health["components"]["investigations"] = {
        "status": "healthy" if metrics["active"] < 100 else "degraded",
        "active_count": metrics["active"],
        "avg_duration_minutes": metrics["avg_duration"]
    }
    
    return health
```

### Logging Configuration

```python
import logging
import structlog

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Log investigation events
logger.info(
    "investigation_started",
    investigation_id="inv-123",
    engagement_mode="investigator",
    strategy="post_mortem"
)

logger.info(
    "phase_transition",
    investigation_id="inv-123",
    from_phase=1,
    to_phase=2,
    duration_seconds=45.2
)
```

---

## Deployment Considerations

### Deployment Checklist

- [ ] LLM API keys configured and validated
- [ ] Redis instance provisioned and accessible
- [ ] ChromaDB initialized with schema
- [ ] Presidio service configured
- [ ] Environment variables set correctly
- [ ] Monitoring/metrics endpoint exposed
- [ ] Health check endpoint responding
- [ ] Logging configured and tested
- [ ] Error tracking (Sentry) initialized
- [ ] Resource limits configured
- [ ] Backup strategy implemented

### Resource Sizing

**Small Deployment** (< 50 concurrent investigations):
- 2 CPU cores, 4GB RAM
- Redis: 1GB memory
- Expected: 10-20 investigations/hour

**Medium Deployment** (50-200 concurrent investigations):
- 4 CPU cores, 8GB RAM
- Redis: 2GB memory
- Expected: 50-100 investigations/hour

**Large Deployment** (200+ concurrent investigations):
- 8+ CPU cores, 16GB+ RAM
- Redis: 4GB+ memory
- Expected: 100+ investigations/hour

### Performance Tuning

```python
# Optimize for throughput
config = InvestigationConfig(
    async_state_save=True,  # Non-blocking saves
    compress_state_in_redis=True,  # Reduce memory
    enable_automatic_compression=True,  # Token management
    max_concurrent_llm_requests=20  # Parallel LLM calls
)

# Optimize for latency
config = InvestigationConfig(
    enable_caching=True,  # Cache LLM responses
    memory_compression_interval=5,  # Less frequent compression
    save_state_on_each_turn=False  # Batch saves
)
```

---

**END OF DOCUMENT**


