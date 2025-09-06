"""
Configuration Architecture Compliance Tests

Ensures architectural compliance and prevents regression to scattered 
environment variable usage patterns.

These tests enforce the clean architecture principle that only the 
settings module should access environment variables directly.
"""

import pytest
import ast
import os
from pathlib import Path
from typing import List, Dict, Any, Set
from unittest.mock import patch

from faultmaven.config.settings import get_settings, reset_settings, FaultMavenSettings


class ConfigurationViolationScanner:
    """Scans codebase for configuration architecture violations"""
    
    def __init__(self, source_root: Path):
        self.source_root = source_root
        self.allowed_files = {
            "faultmaven/config/settings.py",  # Only file allowed to access env vars
            "tests/architecture/test_configuration_compliance.py"  # This test file
        }
        
    def scan_for_os_getenv_violations(self) -> List[Dict[str, Any]]:
        """Find all os.getenv() calls outside of allowed files"""
        violations = []
        
        for py_file in self.source_root.rglob("*.py"):
            # Skip test files, __pycache__, and allowed files
            if (
                "__pycache__" in str(py_file) or
                "test_" in py_file.name or
                any(allowed in str(py_file) for allowed in self.allowed_files)
            ):
                continue
                
            violations.extend(self._scan_file_for_env_access(py_file))
            
        return violations
        
    def _scan_file_for_env_access(self, file_path: Path) -> List[Dict[str, Any]]:
        """Scan single file for environment variable access"""
        violations = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            tree = ast.parse(content, filename=str(file_path))
            visitor = EnvAccessVisitor(file_path)
            visitor.visit(tree)
            
            violations.extend(visitor.violations)
            
        except (SyntaxError, UnicodeDecodeError):
            # Skip files that can't be parsed
            pass
            
        return violations
    
    def scan_for_legacy_config_imports(self) -> List[Dict[str, Any]]:
        """Find imports of legacy configuration modules"""
        violations = []
        
        for py_file in self.source_root.rglob("*.py"):
            if "__pycache__" in str(py_file) or "test_" in py_file.name:
                continue
                
            violations.extend(self._scan_file_for_legacy_imports(py_file))
            
        return violations
    
    def _scan_file_for_legacy_imports(self, file_path: Path) -> List[Dict[str, Any]]:
        """Scan single file for legacy configuration imports"""
        violations = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            # Look for legacy import patterns
            legacy_patterns = [
                "from faultmaven.config.config import",
                "from faultmaven.config.configuration_manager import",
                "import faultmaven.config.config",
                "import faultmaven.config.configuration_manager"
            ]
            
            lines = content.split('\n')
            for line_no, line in enumerate(lines, 1):
                for pattern in legacy_patterns:
                    if pattern in line and not line.strip().startswith("#"):
                        violations.append({
                            "file": str(file_path),
                            "line": line_no,
                            "content": line.strip(),
                            "violation_type": "legacy_config_import",
                            "message": f"Legacy configuration import found: {pattern}"
                        })
                        
        except (UnicodeDecodeError, IOError):
            pass
            
        return violations


class EnvAccessVisitor(ast.NodeVisitor):
    """AST visitor to detect direct environment variable access"""
    
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.violations = []
        
    def visit_Call(self, node):
        """Check function calls for os.getenv() usage"""
        # Check for os.getenv() calls
        if (
            hasattr(node.func, 'attr') and
            hasattr(node.func, 'value') and
            hasattr(node.func.value, 'id') and
            node.func.value.id == 'os' and
            node.func.attr == 'getenv'
        ):
            env_var = self._extract_env_var_name(node)
            
            self.violations.append({
                "file": str(self.file_path),
                "line": node.lineno,
                "env_var": env_var,
                "violation_type": "direct_env_access",
                "message": f"Direct os.getenv() call for '{env_var}' - use settings instead"
            })
        
        # Check for environ dict access
        elif (
            hasattr(node.func, 'value') and
            hasattr(node.func.value, 'attr') and
            hasattr(node.func.value.value, 'id') and
            node.func.value.value.id == 'os' and
            node.func.value.attr == 'environ' and
            hasattr(node.func, 'attr') and
            node.func.attr == 'get'
        ):
            env_var = self._extract_env_var_name(node)
            
            self.violations.append({
                "file": str(self.file_path),
                "line": node.lineno,
                "env_var": env_var,
                "violation_type": "environ_dict_access",
                "message": f"Direct os.environ.get() call for '{env_var}' - use settings instead"
            })
            
        self.generic_visit(node)
        
    def visit_Subscript(self, node):
        """Check subscript access for os.environ['VAR']"""
        if (
            hasattr(node.value, 'attr') and
            hasattr(node.value, 'value') and
            hasattr(node.value.value, 'id') and
            node.value.value.id == 'os' and
            node.value.attr == 'environ'
        ):
            env_var = self._extract_subscript_var_name(node)
            
            self.violations.append({
                "file": str(self.file_path),
                "line": node.lineno,
                "env_var": env_var,
                "violation_type": "environ_subscript_access",
                "message": f"Direct os.environ['{env_var}'] access - use settings instead"
            })
            
        self.generic_visit(node)
        
    def _extract_env_var_name(self, node) -> str:
        """Extract environment variable name from function call"""
        if node.args:
            arg = node.args[0]
            if hasattr(arg, 'value'):  # String literal
                return str(arg.value)
            elif hasattr(arg, 's'):  # Python < 3.8 string literal
                return str(arg.s)
        return "UNKNOWN"
        
    def _extract_subscript_var_name(self, node) -> str:
        """Extract environment variable name from subscript"""
        if hasattr(node.slice, 'value'):  # String literal
            return str(node.slice.value)
        elif hasattr(node.slice, 's'):  # Python < 3.8 string literal
            return str(node.slice.s)
        return "UNKNOWN"


class TestConfigurationArchitectureCompliance:
    """Test suite ensuring configuration architecture compliance"""
    
    def setup_method(self):
        """Reset configuration state before each test"""
        reset_settings()
        
    @pytest.fixture(scope="class")
    def scanner(self):
        """Create violation scanner for the FaultMaven codebase"""
        source_root = Path(__file__).parent.parent.parent / "faultmaven"
        return ConfigurationViolationScanner(source_root)
        
    def test_no_direct_environment_variable_access(self, scanner):
        """
        Ensure no modules access environment variables directly.
        
        Only faultmaven/config/settings.py is allowed to use os.getenv().
        All other modules must receive configuration via dependency injection.
        """
        violations = scanner.scan_for_os_getenv_violations()
        
        if violations:
            violation_summary = []
            for violation in violations:
                violation_summary.append(
                    f"  {violation['file']}:{violation['line']} - {violation['message']}"
                )
            
            pytest.fail(
                f"Found {len(violations)} direct environment variable access violations:\n" +
                "\n".join(violation_summary) + 
                "\n\nOnly faultmaven/config/settings.py should access environment variables directly."
            )
    
    def test_no_legacy_configuration_imports(self, scanner):
        """
        Ensure no modules import legacy configuration systems.
        
        Legacy config.py and configuration_manager.py should not be imported
        anywhere in the codebase.
        """
        violations = scanner.scan_for_legacy_config_imports()
        
        if violations:
            violation_summary = []
            for violation in violations:
                violation_summary.append(
                    f"  {violation['file']}:{violation['line']} - {violation['content']}"
                )
            
            pytest.fail(
                f"Found {len(violations)} legacy configuration import violations:\n" +
                "\n".join(violation_summary) + 
                "\n\nAll modules should use faultmaven.config.settings instead."
            )
    
    def test_settings_singleton_behavior(self):
        """Test that settings follow singleton pattern correctly"""
        # Should return same instance
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2, "Settings should be singleton"
        
        # Reset should create new instance
        reset_settings()
        settings3 = get_settings()
        assert settings1 is not settings3, "Reset should create new instance"
        
    def test_settings_validation_with_environment_variables(self):
        """Test that settings properly validate environment variables"""
        # Test with valid environment variables
        test_env = {
            "ENVIRONMENT": "development",
            "HOST": "0.0.0.0",
            "PORT": "8001",
            "CHAT_PROVIDER": "openai",
            "LOG_LEVEL": "DEBUG",
            "SESSION_TIMEOUT_MINUTES": "45",
            "REDIS_HOST": "test-redis"
        }
        
        with patch.dict(os.environ, test_env, clear=False):
            reset_settings()
            settings = get_settings()
            
            # Verify settings picked up environment variables
            assert settings.server.environment.value == "development"
            assert settings.server.host == "0.0.0.0"
            assert settings.server.port == 8001
            assert settings.llm.provider.value == "openai"
            assert settings.logging.level.value == "DEBUG"
            assert settings.session.timeout_minutes == 45
            assert settings.database.redis_host == "test-redis"
    
    def test_settings_validation_with_invalid_values(self):
        """Test that pydantic validation catches invalid configuration values"""
        # Test invalid port
        with patch.dict(os.environ, {"PORT": "invalid"}, clear=False):
            reset_settings()
            with pytest.raises(ValueError, match="validation error"):
                get_settings()
        
        # Test invalid enum value
        with patch.dict(os.environ, {"LOG_LEVEL": "INVALID_LEVEL"}, clear=False):
            reset_settings()
            with pytest.raises(ValueError, match="validation error"):
                get_settings()
        
        # Test invalid timeout (too small)
        with patch.dict(os.environ, {"SESSION_TIMEOUT_MINUTES": "0"}, clear=False):
            reset_settings()
            with pytest.raises(ValueError, match="validation error"):
                get_settings()
    
    def test_frontend_compatibility_validation(self):
        """Test frontend compatibility validation"""
        reset_settings()
        settings = get_settings()
        
        # Test with default settings
        compatibility = settings.validate_frontend_compatibility()
        assert isinstance(compatibility, dict)
        assert "compatible" in compatibility
        assert "issues" in compatibility
        assert "warnings" in compatibility
        
        # Test with problematic configuration
        with patch.dict(os.environ, {"SESSION_TIMEOUT_MINUTES": "2"}, clear=False):
            reset_settings()
            settings = get_settings()
            compatibility = settings.validate_frontend_compatibility()
            
            assert not compatibility["compatible"]
            assert len(compatibility["issues"]) > 0
            assert any("timeout too short" in issue.lower() for issue in compatibility["issues"])
    
    def test_cors_configuration_method(self):
        """Test CORS configuration generation for frontend compatibility"""
        settings = get_settings()
        cors_config = settings.get_cors_config()
        
        # Should have required structure
        required_keys = ["allow_origins", "allow_credentials", "allow_methods", 
                        "allow_headers", "expose_headers"]
        for key in required_keys:
            assert key in cors_config, f"Missing CORS config key: {key}"
        
        # Should include critical headers for frontend
        exposed_headers = cors_config["expose_headers"]
        critical_headers = ["X-RateLimit-Remaining", "X-Total-Count", "Location"]
        for header in critical_headers:
            assert header in exposed_headers, f"Missing critical header: {header}"
        
        # Should include browser extension origins
        origins = cors_config["allow_origins"]
        assert any("chrome-extension://" in str(origin) for origin in origins), \
            "Missing chrome-extension origin"
        assert any("localhost" in str(origin) for origin in origins), \
            "Missing localhost origin"
    
    def test_redis_url_generation(self):
        """Test Redis URL generation from settings"""
        # Test without password
        with patch.dict(os.environ, {
            "REDIS_HOST": "test-redis",
            "REDIS_PORT": "6380",
            "REDIS_DB": "1"
        }, clear=False):
            reset_settings()
            settings = get_settings()
            redis_url = settings.get_redis_url()
            
            assert redis_url == "redis://test-redis:6380/1"
        
        # Test with direct REDIS_URL
        with patch.dict(os.environ, {
            "REDIS_URL": "redis://custom-redis:6379/2"
        }, clear=False):
            reset_settings()
            settings = get_settings()
            redis_url = settings.get_redis_url()
            
            assert redis_url == "redis://custom-redis:6379/2"
    
    def test_llm_provider_api_key_access(self):
        """Test LLM provider API key access and security"""
        with patch.dict(os.environ, {
            "CHAT_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-key-123"
        }, clear=False):
            reset_settings()
            settings = get_settings()
            
            # Should get correct API key for provider
            api_key = settings.llm.get_api_key()
            assert api_key == "test-key-123"
            
            # Should get correct model for provider
            model = settings.llm.get_model()
            assert model == settings.llm.openai_model
            
            # API key should be secret (not visible in string representation)
            assert "test-key-123" not in str(settings.llm.openai_api_key)
    
    def test_environment_detection_methods(self):
        """Test environment detection helper methods"""
        # Test development environment
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=False):
            reset_settings()
            settings = get_settings()
            
            assert settings.is_development()
            assert not settings.is_production()
        
        # Test production environment
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=False):
            reset_settings()
            settings = get_settings()
            
            assert not settings.is_development()
            assert settings.is_production()
    
    def test_nested_settings_structure_integrity(self):
        """Test that nested settings structure is maintained"""
        settings = get_settings()
        
        # Test all nested sections exist
        expected_sections = [
            'server', 'llm', 'database', 'session', 'security', 
            'protection', 'observability', 'logging', 'upload', 
            'knowledge', 'features'
        ]
        
        for section in expected_sections:
            assert hasattr(settings, section), f"Missing settings section: {section}"
        
        # Test that each section has expected attributes
        assert hasattr(settings.server, 'environment')
        assert hasattr(settings.llm, 'provider')
        assert hasattr(settings.database, 'redis_host')
        assert hasattr(settings.session, 'timeout_minutes')
        assert hasattr(settings.security, 'cors_allow_origins')
        assert hasattr(settings.protection, 'protection_enabled')
        assert hasattr(settings.observability, 'tracing_enabled')
        assert hasattr(settings.logging, 'level')
        assert hasattr(settings.upload, 'max_file_size_mb')
        assert hasattr(settings.knowledge, 'enable_web_search')
        assert hasattr(settings.features, 'use_di_container')


class TestConfigurationBridge:
    """Test the legacy compatibility bridge during migration"""
    
    def test_configuration_bridge_access(self):
        """Test that configuration bridge provides legacy access patterns"""
        from faultmaven.config.settings import config_bridge
        
        # Should be able to access nested configuration
        provider = config_bridge.get("llm.provider", "default")
        assert provider is not None
        
        host = config_bridge.get("server.host", "default")
        assert host is not None
        
        # Should return default for non-existent keys
        unknown = config_bridge.get("non.existent.key", "default_value")
        assert unknown == "default_value"
    
    def test_bridge_with_environment_variables(self):
        """Test bridge works with environment variable overrides"""
        from faultmaven.config.settings import config_bridge
        
        with patch.dict(os.environ, {
            "CHAT_PROVIDER": "anthropic",
            "REDIS_HOST": "bridge-test-redis"
        }, clear=False):
            reset_settings()
            
            # Bridge should pick up new environment variables
            provider = config_bridge.get("llm.provider")
            assert provider == "anthropic"
            
            redis_host = config_bridge.get("database.redis_host")
            assert redis_host == "bridge-test-redis"


if __name__ == "__main__":
    # Allow running this test file directly for development
    pytest.main([__file__, "-v"])