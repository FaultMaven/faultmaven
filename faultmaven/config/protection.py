"""
Protection configuration for FaultMaven

Loads rate limiting, deduplication, and timeout settings from environment
variables with sensible defaults.
"""

import os
from typing import Dict, Any, Optional
from datetime import timedelta

from ..models.protection import (
    ProtectionSettings,
    RateLimitConfig,
    DeduplicationConfig,
    TimeoutConfig,
    LimitType
)


def load_protection_settings(settings=None) -> ProtectionSettings:
    """
    Load protection settings from unified settings or environment variables (fallback)
    
    Args:
        settings: FaultMavenSettings instance (if None, loads from environment)
    
    Environment Variables (used only as fallback when settings unavailable):
        # General
        PROTECTION_ENABLED: Enable all protection mechanisms (default: true)
        PROTECTION_FAIL_OPEN: Fail open on Redis errors (default: true)
        PROTECTION_BYPASS_HEADERS: Comma-separated list of bypass headers
        
        # Rate Limiting
        RATE_LIMITING_ENABLED: Enable rate limiting (default: true)
        REDIS_URL: Redis connection URL (default: redis://192.168.0.111:30379)
        REDIS_KEY_PREFIX: Redis key prefix (default: faultmaven)
        
        # Rate Limits (requests:window_seconds)
        RATE_LIMIT_GLOBAL: Global rate limit (default: 1000:60)
        RATE_LIMIT_PER_SESSION: Per-session rate limit (default: 10:60)
        RATE_LIMIT_PER_SESSION_HOURLY: Per-session hourly (default: 100:3600)
        RATE_LIMIT_TITLE_GENERATION: Title generation (default: 1:300)
        
        # Deduplication
        DEDUPLICATION_ENABLED: Enable request deduplication (default: true)
        DEDUP_DEFAULT_TTL: Default deduplication TTL seconds (default: 300)
        DEDUP_AGENT_QUERY_TTL: Agent query TTL seconds (default: 60)
        
        # Timeouts
        TIMEOUTS_ENABLED: Enable timeout protection (default: true)
        TIMEOUT_AGENT_TOTAL: Total agent timeout seconds (default: 300)
        TIMEOUT_AGENT_PHASE: Agent phase timeout seconds (default: 120)
        TIMEOUT_LLM_CALL: LLM call timeout seconds (default: 30)
        TIMEOUT_EMERGENCY_SHUTDOWN: Emergency shutdown timeout (default: 600)
    
    Returns:
        ProtectionSettings instance with loaded configuration
    """
    
    # If settings provided, use them
    if settings is not None:
        # Get settings if not provided
        try:
            from faultmaven.config.settings import get_settings
            if settings is None:
                settings = get_settings()
        except Exception:
            settings = None
    
    if settings is not None:
        # Use settings-based configuration
        return _load_from_settings(settings)
    else:
        # Fallback to environment variables
        return _load_from_environment()


def _load_from_settings(settings) -> ProtectionSettings:
    """Load protection settings from unified settings"""
    # Basic protection settings are available in the settings
    return ProtectionSettings(
        # General - use security and database settings
        enabled=settings.security.protection_enabled,
        fail_open_on_redis_error=True,  # Safe default
        protection_bypass_headers=[],   # No bypasses from settings
        
        # Redis
        redis_url=settings.database.redis_url,
        redis_key_prefix='faultmaven',
        
        # Rate limiting - use defaults since not in basic settings
        rate_limiting_enabled=True,
        rate_limits={
            'global': RateLimitConfig(enabled=True, requests=1000, window=60),
            'per_session': RateLimitConfig(enabled=True, requests=10, window=60),
            'per_session_hourly': RateLimitConfig(enabled=True, requests=100, window=3600),
            'title_generation': RateLimitConfig(enabled=True, requests=1, window=300)
        },
        
        # Deduplication - use defaults
        deduplication_enabled=True,
        deduplication={
            'default': DeduplicationConfig(enabled=True, ttl=300),
            'agent_query': DeduplicationConfig(enabled=True, ttl=60)
        },
        
        # Timeouts - use defaults
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=300,
            agent_phase=120,
            llm_call=30,
            emergency_shutdown=600
        )
    )


def _load_from_environment() -> ProtectionSettings:
    """Load protection settings from environment variables (fallback)"""
    
    # Helper function to parse rate limit string
    def parse_rate_limit(value: str, default_requests: int, default_window: int) -> RateLimitConfig:
        if not value:
            return RateLimitConfig(
                enabled=True,
                requests=default_requests,
                window=default_window
            )
        
        try:
            requests_str, window_str = value.split(':')
            return RateLimitConfig(
                enabled=True,
                requests=int(requests_str),
                window=int(window_str)
            )
        except (ValueError, IndexError):
            return RateLimitConfig(
                enabled=True,
                requests=default_requests,
                window=default_window
            )
    
    # General settings
    protection_enabled = os.getenv('PROTECTION_ENABLED', 'true').lower() == 'true'
    fail_open = os.getenv('PROTECTION_FAIL_OPEN', 'true').lower() == 'true'
    bypass_headers = [
        header.strip() 
        for header in os.getenv('PROTECTION_BYPASS_HEADERS', '').split(',')
        if header.strip()
    ]
    
    # Redis settings
    redis_url = os.getenv('REDIS_URL', 'redis://192.168.0.111:30379')
    redis_key_prefix = os.getenv('REDIS_KEY_PREFIX', 'faultmaven')
    
    # Rate limiting settings
    rate_limiting_enabled = os.getenv('RATE_LIMITING_ENABLED', 'true').lower() == 'true'
    
    rate_limits = {
        'global': parse_rate_limit(
            os.getenv('RATE_LIMIT_GLOBAL', '1000:60'), 
            1000, 60
        ),
        'per_session': parse_rate_limit(
            os.getenv('RATE_LIMIT_PER_SESSION', '10:60'),
            10, 60
        ),
        'per_session_hourly': parse_rate_limit(
            os.getenv('RATE_LIMIT_PER_SESSION_HOURLY', '100:3600'),
            100, 3600
        ),
        'title_generation': parse_rate_limit(
            os.getenv('RATE_LIMIT_TITLE_GENERATION', '1:300'),
            1, 300
        )
    }
    
    # Deduplication settings
    deduplication_enabled = os.getenv('DEDUPLICATION_ENABLED', 'true').lower() == 'true'
    
    deduplication = {
        'default': DeduplicationConfig(
            enabled=True,
            ttl=int(os.getenv('DEDUP_DEFAULT_TTL', '300'))
        ),
        'agent_query': DeduplicationConfig(
            enabled=True,
            ttl=int(os.getenv('DEDUP_AGENT_QUERY_TTL', '60'))
        )
    }
    
    # Timeout settings
    timeouts_enabled = os.getenv('TIMEOUTS_ENABLED', 'true').lower() == 'true'
    
    timeouts = TimeoutConfig(
        enabled=timeouts_enabled,
        agent_total=int(os.getenv('TIMEOUT_AGENT_TOTAL', '300')),
        agent_phase=int(os.getenv('TIMEOUT_AGENT_PHASE', '120')),
        llm_call=int(os.getenv('TIMEOUT_LLM_CALL', '30')),
        emergency_shutdown=int(os.getenv('TIMEOUT_EMERGENCY_SHUTDOWN', '600'))
    )
    
    return ProtectionSettings(
        # General
        enabled=protection_enabled,
        fail_open_on_redis_error=fail_open,
        protection_bypass_headers=bypass_headers,
        
        # Redis
        redis_url=redis_url,
        redis_key_prefix=redis_key_prefix,
        
        # Rate limiting
        rate_limiting_enabled=rate_limiting_enabled,
        rate_limits=rate_limits,
        
        # Deduplication
        deduplication_enabled=deduplication_enabled,
        deduplication=deduplication,
        
        # Timeouts
        timeouts=timeouts
    )


def get_development_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for development
    
    - More lenient rate limits
    - Shorter timeouts for faster feedback
    - Bypass headers enabled
    - Fail open on errors
    """
    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=True,
        protection_bypass_headers=['X-Dev-Bypass', 'X-Test-Bypass'],
        
        # Redis
        redis_url='redis://192.168.0.111:30379',
        redis_key_prefix='faultmaven_dev',
        
        # Rate limiting (more lenient for development)
        rate_limiting_enabled=True,
        rate_limits={
            'global': RateLimitConfig(enabled=True, requests=5000, window=60),
            'per_session': RateLimitConfig(enabled=True, requests=50, window=60),
            'per_session_hourly': RateLimitConfig(enabled=True, requests=500, window=3600),
            'title_generation': RateLimitConfig(enabled=True, requests=5, window=300)
        },
        
        # Deduplication (shorter TTLs for faster iteration)
        deduplication_enabled=True,
        deduplication={
            'default': DeduplicationConfig(enabled=True, ttl=60),
            'agent_query': DeduplicationConfig(enabled=True, ttl=30)
        },
        
        # Timeouts (shorter for faster feedback)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=120,  # 2 minutes
            agent_phase=60,   # 1 minute
            llm_call=20,      # 20 seconds
            emergency_shutdown=180  # 3 minutes
        )
    )


def get_production_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for production
    
    - Strict rate limits
    - Long timeouts for reliability
    - No bypass headers
    - Fail closed on critical errors
    """
    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=False,  # Fail closed in production
        protection_bypass_headers=[],    # No bypasses in production
        
        # Redis
        redis_url='redis://redis.faultmaven.local:6379',
        redis_key_prefix='faultmaven_prod',
        
        # Rate limiting (strict for production)
        rate_limiting_enabled=True,
        rate_limits={
            'global': RateLimitConfig(enabled=True, requests=500, window=60),
            'per_session': RateLimitConfig(enabled=True, requests=5, window=60),
            'per_session_hourly': RateLimitConfig(enabled=True, requests=50, window=3600),
            'title_generation': RateLimitConfig(enabled=True, requests=1, window=600)  # Once per 10 minutes
        },
        
        # Deduplication (longer TTLs for better protection)
        deduplication_enabled=True,
        deduplication={
            'default': DeduplicationConfig(enabled=True, ttl=600),   # 10 minutes
            'agent_query': DeduplicationConfig(enabled=True, ttl=180)  # 3 minutes
        },
        
        # Timeouts (longer for reliability)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=600,  # 10 minutes
            agent_phase=300,  # 5 minutes
            llm_call=60,      # 1 minute
            emergency_shutdown=1200  # 20 minutes
        )
    )


def validate_protection_settings(settings: ProtectionSettings) -> Dict[str, Any]:
    """
    Validate protection settings and return validation report
    
    Returns:
        Dictionary with validation status and any issues found
    """
    validation = {
        'valid': True,
        'warnings': [],
        'errors': [],
        'recommendations': []
    }
    
    # Check Redis URL
    if not settings.redis_url:
        validation['errors'].append('Redis URL is required')
        validation['valid'] = False
    
    # Check rate limits
    for limit_name, limit_config in settings.rate_limits.items():
        if limit_config.enabled:
            if limit_config.requests <= 0:
                validation['errors'].append(f'Rate limit {limit_name} must have positive request count')
                validation['valid'] = False
            
            if limit_config.window <= 0:
                validation['errors'].append(f'Rate limit {limit_name} must have positive window')
                validation['valid'] = False
            
            # Warn about very permissive limits
            if limit_config.requests > 10000:
                validation['warnings'].append(f'Rate limit {limit_name} is very high: {limit_config.requests}')
            
            # Warn about very restrictive limits
            if limit_config.requests < 5 and limit_name != 'title_generation':
                validation['warnings'].append(f'Rate limit {limit_name} is very restrictive: {limit_config.requests}')
    
    # Check deduplication settings
    for dedup_name, dedup_config in settings.deduplication.items():
        if dedup_config.enabled:
            if dedup_config.ttl <= 0:
                validation['errors'].append(f'Deduplication {dedup_name} must have positive TTL')
                validation['valid'] = False
            
            # Warn about very long TTLs
            if dedup_config.ttl > 3600:
                validation['warnings'].append(f'Deduplication {dedup_name} TTL is very long: {dedup_config.ttl}s')
    
    # Check timeout settings
    if settings.timeouts.enabled:
        if settings.timeouts.agent_total <= 0:
            validation['errors'].append('Agent total timeout must be positive')
            validation['valid'] = False
        
        if settings.timeouts.agent_phase <= 0:
            validation['errors'].append('Agent phase timeout must be positive')
            validation['valid'] = False
        
        if settings.timeouts.llm_call <= 0:
            validation['errors'].append('LLM call timeout must be positive')
            validation['valid'] = False
        
        # Check timeout hierarchy
        if settings.timeouts.agent_phase >= settings.timeouts.agent_total:
            validation['warnings'].append('Agent phase timeout should be less than total timeout')
        
        if settings.timeouts.llm_call >= settings.timeouts.agent_phase:
            validation['warnings'].append('LLM call timeout should be less than phase timeout')
        
        # Warn about very short timeouts
        if settings.timeouts.llm_call < 10:
            validation['warnings'].append('LLM call timeout is very short, may cause premature failures')
    
    # Production recommendations
    if not settings.fail_open_on_redis_error:
        if not settings.protection_bypass_headers:
            validation['recommendations'].append('Consider adding emergency bypass headers for production debugging')
    
    return validation