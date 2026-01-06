"""Deployment Profile Pattern for FaultMaven.

This module implements the Deployment Profile Pattern to support different deployment
configurations (CORE, TEAM, ENTERPRISE) without conditional logic in business code.

Pattern: Provider Selection via Profile
- CORE: Community Edition (SQLite, in-memory, local files, no observability)
- TEAM: Team Edition (PostgreSQL, Redis, S3/MinIO, basic metrics)
- ENTERPRISE: Enterprise Edition (PostgreSQL, Redis, S3, Presidio, Opik, Prometheus)

Design Reference: Phase 3, Week 14-15 - Deployment Profile Pattern
Architecture Document: deployment-agnostic-architecture.md

Usage:
    # Auto-detect profile from environment
    profile = ProfileManager.get_current_profile()

    # Check if feature is enabled
    if ProfileManager.is_feature_enabled('pii_redaction'):
        # Use Presidio PII redaction
        ...

    # Get profile-specific configuration
    db_backend = ProfileManager.get_database_backend()
"""

import logging
import os
from enum import Enum
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class DeploymentProfile(str, Enum):
    """Deployment profile types for FaultMaven.

    Attributes:
        CORE: Community Edition - zero external dependencies, local development
        TEAM: Team Edition - PostgreSQL, Redis, basic cloud storage
        ENTERPRISE: Enterprise Edition - full infrastructure stack, compliance features
    """

    CORE = "core"
    TEAM = "team"
    ENTERPRISE = "enterprise"


# Feature Matrix - Defines which features are enabled in each profile
FEATURE_MATRIX: Dict[DeploymentProfile, Dict[str, Any]] = {
    DeploymentProfile.CORE: {
        # Database
        "database_backend": "sqlite",
        "database_url": "sqlite:///./data/faultmaven.db",

        # Session storage
        "session_storage": "inmemory",
        "redis_enabled": False,

        # File storage
        "storage_backend": "filesystem",
        "storage_root": "./data/evidence",

        # Vector storage
        "vector_storage": "inmemory",

        # Security & Compliance
        "pii_redaction": False,
        "presidio_enabled": False,

        # Observability
        "tracing_enabled": False,
        "opik_enabled": False,
        "metrics_enabled": False,
        "prometheus_enabled": False,

        # LLM Providers
        "llm_providers": ["openai", "anthropic", "local"],
        "max_llm_providers": 3,

        # Multi-tenancy
        "tenant_provider": "single",
        "multi_tenant_enabled": False,
    },

    DeploymentProfile.TEAM: {
        # Database
        "database_backend": "postgresql",
        "database_url": None,  # Must be provided via env

        # Session storage
        "session_storage": "redis",
        "redis_enabled": True,

        # File storage
        "storage_backend": "s3",  # Or MinIO
        "storage_root": None,  # Must be provided via env

        # Vector storage
        "vector_storage": "chromadb",

        # Security & Compliance
        "pii_redaction": False,
        "presidio_enabled": False,

        # Observability
        "tracing_enabled": False,
        "opik_enabled": False,
        "metrics_enabled": True,
        "prometheus_enabled": False,  # Basic metrics only

        # LLM Providers
        "llm_providers": ["openai", "anthropic", "fireworks", "gemini", "groq"],
        "max_llm_providers": 5,

        # Multi-tenancy
        "tenant_provider": "single",  # Team edition is single-tenant
        "multi_tenant_enabled": False,
    },

    DeploymentProfile.ENTERPRISE: {
        # Database
        "database_backend": "postgresql",
        "database_url": None,  # Must be provided via env

        # Session storage
        "session_storage": "redis",
        "redis_enabled": True,

        # File storage
        "storage_backend": "s3",
        "storage_root": None,  # Must be provided via env

        # Vector storage
        "vector_storage": "chromadb",

        # Security & Compliance
        "pii_redaction": True,
        "presidio_enabled": True,

        # Observability
        "tracing_enabled": True,
        "opik_enabled": True,
        "metrics_enabled": True,
        "prometheus_enabled": True,

        # LLM Providers
        "llm_providers": ["openai", "anthropic", "fireworks", "gemini", "groq", "huggingface", "local"],
        "max_llm_providers": 7,

        # Multi-tenancy
        "tenant_provider": "multi",
        "multi_tenant_enabled": True,
    },
}


class ProfileManager:
    """Manager for deployment profile detection and feature queries.

    This class provides centralized access to deployment profile configuration
    without introducing conditional logic in business services.
    """

    _current_profile: Optional[DeploymentProfile] = None
    _profile_override: Optional[DeploymentProfile] = None  # For testing

    @classmethod
    def get_current_profile(cls) -> DeploymentProfile:
        """Get the current deployment profile from environment.

        Reads DEPLOYMENT_PROFILE environment variable and validates it.
        Defaults to CORE if not specified or invalid.

        Returns:
            DeploymentProfile enum value

        Example:
            >>> os.environ['DEPLOYMENT_PROFILE'] = 'enterprise'
            >>> ProfileManager.get_current_profile()
            <DeploymentProfile.ENTERPRISE: 'enterprise'>
        """
        # Check for test override first
        if cls._profile_override is not None:
            return cls._profile_override

        # Check cache
        if cls._current_profile is not None:
            return cls._current_profile

        # Read from environment
        profile_str = os.getenv("DEPLOYMENT_PROFILE", "core").lower()

        try:
            profile = DeploymentProfile(profile_str)
            cls._current_profile = profile
            logger.info(f"Deployment profile detected: {profile.value.upper()}")
            return profile
        except ValueError:
            logger.warning(
                f"Invalid DEPLOYMENT_PROFILE='{profile_str}'. "
                f"Valid values: {[p.value for p in DeploymentProfile]}. "
                f"Defaulting to CORE."
            )
            cls._current_profile = DeploymentProfile.CORE
            return cls._current_profile

    @classmethod
    def set_profile(cls, profile: DeploymentProfile) -> None:
        """Set the deployment profile (for testing only).

        Args:
            profile: DeploymentProfile to use

        Warning:
            This method is for testing only. In production, use DEPLOYMENT_PROFILE
            environment variable.
        """
        cls._profile_override = profile
        cls._current_profile = profile
        logger.debug(f"Deployment profile set to: {profile.value.upper()} (override)")

    @classmethod
    def reset_profile(cls) -> None:
        """Reset profile cache and override (for testing)."""
        cls._current_profile = None
        cls._profile_override = None

    @classmethod
    def is_feature_enabled(cls, feature: str) -> bool:
        """Check if a feature is enabled in the current profile.

        Args:
            feature: Feature name to check (e.g., 'pii_redaction', 'tracing_enabled')

        Returns:
            True if feature is enabled, False otherwise

        Raises:
            ValueError: If feature name is not recognized

        Example:
            >>> ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
            >>> ProfileManager.is_feature_enabled('pii_redaction')
            True
            >>> ProfileManager.set_profile(DeploymentProfile.CORE)
            >>> ProfileManager.is_feature_enabled('pii_redaction')
            False
        """
        profile = cls.get_current_profile()
        feature_config = FEATURE_MATRIX[profile]

        if feature not in feature_config:
            raise ValueError(
                f"Unknown feature '{feature}'. "
                f"Valid features: {list(feature_config.keys())}"
            )

        return bool(feature_config[feature])

    @classmethod
    def get_feature_value(cls, feature: str) -> Any:
        """Get the value of a feature for the current profile.

        Args:
            feature: Feature name to retrieve

        Returns:
            Feature value (type depends on feature)

        Raises:
            ValueError: If feature name is not recognized

        Example:
            >>> ProfileManager.set_profile(DeploymentProfile.CORE)
            >>> ProfileManager.get_feature_value('database_backend')
            'sqlite'
            >>> ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
            >>> ProfileManager.get_feature_value('database_backend')
            'postgresql'
        """
        profile = cls.get_current_profile()
        feature_config = FEATURE_MATRIX[profile]

        if feature not in feature_config:
            raise ValueError(
                f"Unknown feature '{feature}'. "
                f"Valid features: {list(feature_config.keys())}"
            )

        return feature_config[feature]

    @classmethod
    def get_database_backend(cls) -> str:
        """Get the database backend for the current profile.

        Returns:
            'sqlite' for CORE, 'postgresql' for TEAM/ENTERPRISE
        """
        return cls.get_feature_value('database_backend')

    @classmethod
    def get_session_storage(cls) -> str:
        """Get the session storage backend for the current profile.

        Returns:
            'inmemory' for CORE, 'redis' for TEAM/ENTERPRISE
        """
        return cls.get_feature_value('session_storage')

    @classmethod
    def get_storage_backend(cls) -> str:
        """Get the file storage backend for the current profile.

        Returns:
            'filesystem' for CORE, 's3' for TEAM/ENTERPRISE
        """
        return cls.get_feature_value('storage_backend')

    @classmethod
    def get_vector_storage(cls) -> str:
        """Get the vector storage backend for the current profile.

        Returns:
            'inmemory' for CORE, 'chromadb' for TEAM/ENTERPRISE
        """
        return cls.get_feature_value('vector_storage')

    @classmethod
    def validate_profile_requirements(cls) -> tuple[bool, list[str]]:
        """Validate that required dependencies are available for current profile.

        Performs fail-fast validation at startup to ensure profile requirements
        are met before accepting requests.

        Returns:
            Tuple of (is_valid, list_of_errors)

        Example:
            >>> ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
            >>> is_valid, errors = ProfileManager.validate_profile_requirements()
            >>> if not is_valid:
            ...     for error in errors:
            ...         print(f"ERROR: {error}")
            ...     sys.exit(1)
        """
        profile = cls.get_current_profile()
        errors = []

        # TEAM and ENTERPRISE require DATABASE_URL
        if profile in [DeploymentProfile.TEAM, DeploymentProfile.ENTERPRISE]:
            if not os.getenv("DATABASE_URL"):
                errors.append(
                    f"{profile.value.upper()} profile requires DATABASE_URL environment variable"
                )

        # TEAM and ENTERPRISE require Redis configuration
        if profile in [DeploymentProfile.TEAM, DeploymentProfile.ENTERPRISE]:
            if not os.getenv("REDIS_HOST") and not os.getenv("REDIS_URL"):
                errors.append(
                    f"{profile.value.upper()} profile requires REDIS_HOST or REDIS_URL environment variable"
                )

        # ENTERPRISE requires Presidio configuration
        if profile == DeploymentProfile.ENTERPRISE:
            if not os.getenv("PRESIDIO_URL") and not os.getenv("SKIP_SERVICE_CHECKS"):
                errors.append(
                    "ENTERPRISE profile requires PRESIDIO_URL environment variable "
                    "(or set SKIP_SERVICE_CHECKS=true for testing)"
                )

        # ENTERPRISE requires Opik configuration
        if profile == DeploymentProfile.ENTERPRISE:
            if not os.getenv("OPIK_API_KEY") and not os.getenv("SKIP_SERVICE_CHECKS"):
                errors.append(
                    "ENTERPRISE profile requires OPIK_API_KEY environment variable "
                    "(or set SKIP_SERVICE_CHECKS=true for testing)"
                )

        is_valid = len(errors) == 0

        if not is_valid:
            logger.error(f"Profile validation failed for {profile.value.upper()}:")
            for error in errors:
                logger.error(f"  - {error}")
        else:
            logger.info(f"Profile validation passed for {profile.value.upper()}")

        return is_valid, errors

    @classmethod
    def get_profile_summary(cls) -> Dict[str, Any]:
        """Get a summary of the current profile configuration.

        Returns:
            Dictionary with profile name and enabled features

        Example:
            >>> summary = ProfileManager.get_profile_summary()
            >>> print(summary['profile'])
            'enterprise'
            >>> print(summary['features']['pii_redaction'])
            True
        """
        profile = cls.get_current_profile()
        features = FEATURE_MATRIX[profile]

        return {
            "profile": profile.value,
            "features": features.copy(),
            "description": cls._get_profile_description(profile),
        }

    @classmethod
    def _get_profile_description(cls, profile: DeploymentProfile) -> str:
        """Get human-readable description of a profile."""
        descriptions = {
            DeploymentProfile.CORE: (
                "Community Edition - SQLite, in-memory sessions, local files, "
                "no observability"
            ),
            DeploymentProfile.TEAM: (
                "Team Edition - PostgreSQL, Redis sessions, S3/MinIO storage, "
                "basic metrics"
            ),
            DeploymentProfile.ENTERPRISE: (
                "Enterprise Edition - PostgreSQL, Redis, S3, Presidio PII, "
                "Opik tracing, Prometheus metrics"
            ),
        }
        return descriptions.get(profile, "Unknown profile")
