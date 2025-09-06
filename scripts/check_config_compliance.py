#!/usr/bin/env python3
"""
Configuration Compliance Checker

Standalone script to check for configuration architecture violations.
Can be run as a pre-commit hook or CI check to prevent regressions.

Usage:
    python scripts/check_config_compliance.py
    
Exit codes:
    0 - No violations found
    1 - Violations detected
"""

import sys
import os
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.architecture.test_configuration_compliance import ConfigurationViolationScanner


def main():
    """Run configuration compliance checks"""
    print("🔍 FaultMaven Configuration Architecture Compliance Check")
    print("=" * 60)
    
    # Initialize scanner
    source_root = project_root / "faultmaven"
    scanner = ConfigurationViolationScanner(source_root)
    
    total_violations = 0
    
    # Check for direct environment variable access violations
    print("\n📋 Checking for direct environment variable access...")
    env_violations = scanner.scan_for_os_getenv_violations()
    
    if env_violations:
        print(f"❌ Found {len(env_violations)} direct environment access violations:")
        for violation in env_violations:
            print(f"   {violation['file']}:{violation['line']} - {violation['message']}")
        total_violations += len(env_violations)
    else:
        print("✅ No direct environment variable access violations found")
    
    # Check for legacy configuration imports
    print("\n📋 Checking for legacy configuration imports...")
    legacy_violations = scanner.scan_for_legacy_config_imports()
    
    if legacy_violations:
        print(f"❌ Found {len(legacy_violations)} legacy configuration import violations:")
        for violation in legacy_violations:
            print(f"   {violation['file']}:{violation['line']} - {violation['message']}")
        total_violations += len(legacy_violations)
    else:
        print("✅ No legacy configuration import violations found")
    
    # Check for proper settings usage
    print("\n📋 Checking settings system integrity...")
    try:
        from faultmaven.config.settings import get_settings, reset_settings
        
        # Test settings loading
        reset_settings()
        settings = get_settings()
        print("✅ Settings system loads successfully")
        
        # Test frontend compatibility
        compatibility = settings.validate_frontend_compatibility()
        if compatibility["compatible"]:
            print("✅ Settings are frontend-compatible")
        else:
            print("⚠️  Settings have potential frontend compatibility issues:")
            for issue in compatibility["issues"]:
                print(f"   - {issue}")
            for warning in compatibility["warnings"]:
                print(f"   - WARNING: {warning}")
        
        # Test CORS configuration
        cors_config = settings.get_cors_config()
        required_headers = ["X-RateLimit-Remaining", "X-Total-Count", "Location"]
        missing_headers = [h for h in required_headers if h not in cors_config.get("expose_headers", [])]
        
        if missing_headers:
            print(f"⚠️  Missing critical CORS headers: {missing_headers}")
        else:
            print("✅ CORS configuration includes all required headers")
            
    except Exception as e:
        print(f"❌ Settings system error: {e}")
        total_violations += 1
    
    # Summary
    print("\n" + "=" * 60)
    if total_violations == 0:
        print("🎉 CONFIGURATION COMPLIANCE CHECK: PASSED")
        print("✅ No architecture violations detected")
        print("✅ Unified settings system is properly enforced")
        return 0
    else:
        print("🚨 CONFIGURATION COMPLIANCE CHECK: FAILED")
        print(f"❌ Found {total_violations} architecture violations")
        print("\nViolations must be fixed before proceeding:")
        print("1. Replace os.getenv() calls with settings-based configuration")
        print("2. Remove legacy configuration imports")
        print("3. Ensure all services receive settings via dependency injection")
        print("\nOnly faultmaven/config/settings.py should access environment variables directly.")
        return 1


if __name__ == "__main__":
    sys.exit(main())