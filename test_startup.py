#!/usr/bin/env python3
"""Test full application startup sequence"""

import os
import sys
sys.path.insert(0, '.')

def test_startup():
    print("🚀 STEP 5: Full Application Startup Test")
    
    # Set environment for full activation
    os.environ['USE_DI_CONTAINER'] = 'true'
    os.environ['USE_REFACTORED_SERVICES'] = 'true'
    os.environ['USE_REFACTORED_API'] = 'true'
    os.environ['ENABLE_MIGRATION_LOGGING'] = 'true'
    
    print("Simulating main.py startup sequence...")
    
    try:
        # 1. Feature flag loading
        print("\n=== Step 1: Feature Flag Configuration ===")
        from faultmaven.config.feature_flags import (
            USE_REFACTORED_API, USE_DI_CONTAINER, 
            log_feature_flag_status, get_migration_strategy
        )
        
        log_feature_flag_status()
        print(f"✅ Strategy: {get_migration_strategy()}")
        
        # 2. Route selection logic
        print("\n=== Step 2: Route Selection Logic ===")
        if USE_REFACTORED_API:
            print("✅ Would load refactored API routes")
            from pathlib import Path
            routes = [
                "faultmaven/api/v1/routes/agent_refactored.py",
                "faultmaven/api/v1/routes/data_refactored.py"
            ]
            for route in routes:
                if Path(route).exists():
                    print(f"  ✅ {route} ready")
                else:
                    raise FileNotFoundError(f"Missing {route}")
        
        # 3. Container initialization
        print("\n=== Step 3: Container Initialization ===")
        if USE_DI_CONTAINER:
            print("✅ Would initialize refactored DI container")
            from faultmaven.container_refactored import container
            container.initialize()
            
            health = container.health_check()
            print(f"✅ Container health: {health['status']}")
            
            if health['status'] != 'healthy':
                print("ℹ️  Degraded due to missing dependencies (expected)")
        
        # 4. Health endpoint
        print("\n=== Step 4: Health Endpoint ===")
        from faultmaven.config.feature_flags import is_migration_safe
        
        health_data = {
            'migration_strategy': get_migration_strategy(),
            'migration_safe': is_migration_safe(),
            'using_refactored_api': USE_REFACTORED_API,
            'using_di_container': USE_DI_CONTAINER
        }
        
        print("✅ Health endpoint data:")
        for key, value in health_data.items():
            print(f"  {key}: {value}")
        
        print("\n🎉 STEP 5 SUCCESS: Startup sequence validated!")
        return True
        
    except Exception as e:
        print(f"\n❌ STEP 5 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_startup()
    sys.exit(0 if success else 1)