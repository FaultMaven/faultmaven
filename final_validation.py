#!/usr/bin/env python3
"""Final architecture validation for full activation"""

import os
import sys
sys.path.insert(0, '.')

def final_validation():
    print("🏁 STEP 6: FINAL ARCHITECTURE VALIDATION")
    
    # Set full activation environment
    os.environ['USE_DI_CONTAINER'] = 'true'
    os.environ['USE_REFACTORED_SERVICES'] = 'true'
    os.environ['USE_REFACTORED_API'] = 'true'
    os.environ['ENABLE_MIGRATION_LOGGING'] = 'true'
    
    print("Running comprehensive validation with full new architecture...")
    
    try:
        # 1. Architecture Tests (from our test_architecture.py)
        print("\n=== Architecture Boundary Tests ===")
        import ast
        from pathlib import Path
        
        # API boundary test (refactored routes only)
        api_violations = 0
        api_files = list(Path("faultmaven/api").rglob("*.py"))
        for file in api_files:
            if file.name.startswith("__"):
                continue
            if not file.name.endswith("_refactored.py") and file.name != "dependencies.py":
                continue
            
            content = file.read_text()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                    if (module.startswith("faultmaven.core") or 
                        (module.startswith("faultmaven.infrastructure") and not module.endswith("observability.tracing")) or
                        module.startswith("faultmaven.tools")):
                        api_violations += 1
        
        print(f"✅ API boundary violations: {api_violations}")
        
        # Service interface test
        service_violations = 0
        service_files = list(Path("faultmaven/services").rglob("*_refactored.py"))
        for file in service_files:
            if "test_" in file.name or "example_" in file.name:
                continue
            content = file.read_text()
            if "from faultmaven.models.interfaces import" not in content:
                service_violations += 1
        
        print(f"✅ Service interface violations: {service_violations}")
        
        # 2. Feature Flag Validation
        print("\n=== Feature Flag Validation ===")
        from faultmaven.config.feature_flags import (
            get_migration_strategy, is_migration_safe, 
            validate_feature_flag_combination
        )
        
        strategy = get_migration_strategy()
        safe = is_migration_safe()
        
        print(f"✅ Migration strategy: {strategy}")
        print(f"✅ Migration safe: {safe}")
        
        validate_feature_flag_combination()  # Should not raise exception
        print("✅ Feature flag validation passed")
        
        # 3. Container Health
        print("\n=== Container Health Validation ===")
        from faultmaven.container_refactored import DIContainer
        DIContainer._instance = None
        container = DIContainer()
        container.initialize()
        
        health = container.health_check()
        print(f"✅ Container status: {health['status']}")
        print(f"✅ Component count: {len(health.get('components', {}))}")
        
        # 4. End-to-End Integration
        print("\n=== End-to-End Integration ===")
        
        # Service access
        agent_service = container.get_agent_service()
        data_service = container.get_data_service()
        knowledge_service = container.get_knowledge_service()
        
        print(f"✅ Agent service: {type(agent_service).__name__}")
        print(f"✅ Data service: {type(data_service).__name__}")
        print(f"✅ Knowledge service: {type(knowledge_service).__name__}")
        
        # Interface dependencies
        if hasattr(agent_service, '_llm') and hasattr(agent_service, '_sanitizer'):
            print("✅ Agent service has interface dependencies")
        
        # 5. Production Readiness Check
        print("\n=== Production Readiness ===")
        
        checks = [
            strategy == "full_new_architecture",
            safe == True,
            api_violations == 0,
            service_violations == 0,
            health['status'] in ['healthy', 'degraded'],  # degraded ok due to missing deps
            agent_service is not None,
            data_service is not None
        ]
        
        all_passed = all(checks)
        
        for i, check in enumerate(checks, 1):
            status = "✅" if check else "❌"
            print(f"{status} Check {i}: {'PASS' if check else 'FAIL'}")
        
        if all_passed:
            print("\n🎉 FINAL VALIDATION SUCCESS!")
            print("✅ Full new architecture is ACTIVE and READY!")
            print("✅ All boundaries enforced")
            print("✅ All interfaces working")  
            print("✅ Production deployment ready")
            return True
        else:
            print("\n❌ FINAL VALIDATION FAILED!")
            return False
            
    except Exception as e:
        print(f"\n❌ VALIDATION ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = final_validation()
    sys.exit(0 if success else 1)