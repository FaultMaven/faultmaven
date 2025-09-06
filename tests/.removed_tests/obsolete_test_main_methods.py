"""
OBSOLETE TEST METHODS REMOVED FROM tests/test_main.py

These methods were removed because they reference deleted modules:
- faultmaven.config.configuration_manager.ConfigurationManager
- faultmaven.config.configuration_manager.reset_config

These modules were deleted during the configuration system refactoring.

Removal Date: 2025-08-28
Reason: References non-existent modules after refactoring
"""

import os
import pytest
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from faultmaven.main import app


def test_configuration_validation_in_startup():
    """Test that application startup validates configuration."""
    # Mock configuration manager to test validation during startup
    with patch('faultmaven.config.configuration_manager.ConfigurationManager') as mock_config_class:
        mock_config = Mock()
        mock_config.validate.return_value = True
        mock_config.get_database_config.return_value = {
            "host": "test.redis.com",
            "port": 6379,
            "password": None,
            "db": 0,
            "ssl": False,
            "timeout": 30
        }
        mock_config.get_session_config.return_value = {
            "timeout_minutes": 30,
            "cleanup_interval_minutes": 15,
            "max_memory_mb": 100,
            "cleanup_batch_size": 50,
            "encryption_key": None
        }
        mock_config_class.return_value = mock_config
        
        # Application should handle configuration validation
        assert isinstance(app, FastAPI)
        
        # Verify configuration would be validated if implemented
        if hasattr(app, 'state') and hasattr(app.state, 'config_validated'):
            assert app.state.config_validated is True


def test_configuration_hot_reload_support():
    """Test that application supports configuration changes."""
    # Initial configuration
    initial_config = {
        'CHAT_PROVIDER': 'openai',
        'REDIS_HOST': 'initial.redis.com',
        'SESSION_TIMEOUT_MINUTES': '30'
    }
    
    with patch.dict(os.environ, initial_config, clear=True):
        with TestClient(app) as client:
            # Initial health check
            response1 = client.get("/health")
            assert response1.status_code == 200
    
    # Updated configuration
    updated_config = {
        'CHAT_PROVIDER': 'openai',
        'REDIS_HOST': 'updated.redis.com',
        'SESSION_TIMEOUT_MINUTES': '45'
    }
    
    with patch.dict(os.environ, updated_config, clear=True):
        # Reset configuration manager singleton if needed
        from faultmaven.config.configuration_manager import reset_config
        reset_config()
        
        with TestClient(app) as client:
            # Health check should work with updated config
            response2 = client.get("/health")
            assert response2.status_code == 200