#!/usr/bin/env python3
"""
Frontend Verification Smoke Test

Purpose: E2E smoke test for frontend-backend contract compliance
Flow: dev-login → create case → submit query → list sessions/cases → verify responses
Target: No client workarounds needed; strict parsing per spec

This script simulates the critical user flows that the frontend team
reported issues with, verifying that all contract violations are resolved.
"""

import asyncio
import json
import time
import uuid
from typing import Dict, Any, List, Optional

import aiohttp
import argparse

class FrontendVerificationTest:
    """Frontend verification smoke test runner"""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip('/')
        self.session_id: Optional[str] = None
        self.case_id: Optional[str] = None
        self.query_id: Optional[str] = None
        self.user_id = f"test-user-{uuid.uuid4().hex[:8]}"
        
    async def run_smoke_test(self) -> Dict[str, Any]:
        """Run complete frontend verification smoke test"""
        
        print(f"🚀 Starting Frontend Verification Smoke Test")
        print(f"📡 Backend: {self.base_url}")
        print(f"👤 User: {self.user_id}")
        print("=" * 60)
        
        results = {
            "timestamp": time.time(),
            "backend_url": self.base_url,
            "user_id": self.user_id,
            "tests": {},
            "overall_status": "UNKNOWN"
        }
        
        async with aiohttp.ClientSession() as session:
            self.session = session
            
            try:
                # Step 1: Dev-login simulation
                await self._test_dev_login(results)
                
                # Step 2: Create case
                await self._test_create_case(results)
                
                # Step 3: Submit query (observe 201/202 path)
                await self._test_submit_query(results)
                
                # Step 4: List session cases
                await self._test_list_session_cases(results)
                
                # Step 5: List all cases
                await self._test_list_cases(results)
                
                # Step 6: Pre-auth probe verification
                await self._test_pre_auth_probe(results)
                
                # Calculate overall result
                failed_tests = [name for name, result in results["tests"].items() if result["status"] != "PASS"]
                
                if failed_tests:
                    results["overall_status"] = "FAIL"
                    results["failed_tests"] = failed_tests
                else:
                    results["overall_status"] = "PASS"
                    
            except Exception as e:
                results["overall_status"] = "ERROR"
                results["error"] = str(e)
                print(f"❌ Smoke test failed with error: {e}")
        
        return results
    
    async def _test_dev_login(self, results: Dict[str, Any]):
        """Test dev-login simulation (create session)"""
        print("\n1️⃣  Testing dev-login flow...")
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            # Create a new session (simulates dev-login)
            payload = {
                "session_id": f"session-{uuid.uuid4().hex[:8]}",
                "usage_type": "troubleshooting"
            }
            
            async with self.session.post(
                f"{self.base_url}/api/v1/sessions",
                json=payload,
                headers={"X-User-Id": self.user_id}
            ) as response:
                
                data = await response.json()
                
                # Verify response
                if response.status == 201:
                    self.session_id = data.get("session_id", payload["session_id"])
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "session_id": self.session_id,
                        "status_code": response.status
                    }
                    print(f"   ✅ Session created: {self.session_id}")
                else:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "status_code": response.status,
                        "response": data
                    }
                    print(f"   ❌ Session creation failed: {response.status}")
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Dev-login test error: {e}")
            
        results["tests"]["dev_login"] = test_result
    
    async def _test_create_case(self, results: Dict[str, Any]):
        """Test case creation with Location header verification"""
        print("\n2️⃣  Testing case creation...")
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            payload = {
                "title": f"Smoke Test Case {time.time()}",
                "description": "Frontend verification smoke test case",
                "session_id": self.session_id
            }
            
            async with self.session.post(
                f"{self.base_url}/api/v1/cases",
                json=payload,
                headers={"X-User-Id": self.user_id}
            ) as response:
                
                data = await response.json()
                location_header = response.headers.get("Location")
                
                # Verify contract compliance
                issues = []
                
                if response.status != 201:
                    issues.append(f"Expected 201, got {response.status}")
                
                if not location_header or location_header in ["null", ""]:
                    issues.append(f"Location header is null/empty: {location_header}")
                
                if "case" not in data:
                    issues.append("Response missing 'case' field")
                elif "case_id" not in data["case"]:
                    issues.append("Response case missing 'case_id' field")
                else:
                    self.case_id = data["case"]["case_id"]
                
                if issues:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "issues": issues,
                        "status_code": response.status,
                        "location_header": location_header,
                        "response_keys": list(data.keys()) if isinstance(data, dict) else "not_dict"
                    }
                    print(f"   ❌ Case creation issues: {issues}")
                else:
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "case_id": self.case_id,
                        "location_header": location_header,
                        "status_code": response.status
                    }
                    print(f"   ✅ Case created: {self.case_id}")
                    print(f"   ✅ Location header: {location_header}")
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Case creation test error: {e}")
            
        results["tests"]["create_case"] = test_result
    
    async def _test_submit_query(self, results: Dict[str, Any]):
        """Test query submission (observe 201/202 path)"""
        print("\n3️⃣  Testing query submission...")
        
        if not self.case_id:
            results["tests"]["submit_query"] = {
                "status": "SKIP", 
                "details": {"reason": "No case_id from previous test"}
            }
            return
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            # Test sync query first (simple query)
            payload = {"query": "Simple test query"}
            
            async with self.session.post(
                f"{self.base_url}/api/v1/cases/{self.case_id}/queries",
                json=payload,
                headers={"X-User-Id": self.user_id}
            ) as response:
                
                data = await response.json()
                location_header = response.headers.get("Location")
                
                issues = []
                
                if response.status == 201:
                    # Sync response validation
                    if not location_header or location_header in ["null", ""]:
                        issues.append(f"201 response missing Location header: {location_header}")
                    
                    required_fields = ["content", "response_type", "view_state"]
                    for field in required_fields:
                        if field not in data:
                            issues.append(f"201 response missing {field} field")
                    
                    print(f"   ✅ Sync query (201): Location={location_header}")
                    
                elif response.status == 202:
                    # Async response validation
                    retry_after = response.headers.get("Retry-After")
                    
                    if not location_header or location_header in ["null", ""]:
                        issues.append(f"202 response missing Location header: {location_header}")
                    
                    if not retry_after:
                        issues.append("202 response missing Retry-After header")
                    
                    required_fields = ["job_id", "case_id", "query", "status"]
                    for field in required_fields:
                        if field not in data:
                            issues.append(f"202 response missing {field} field")
                    
                    print(f"   ✅ Async query (202): Location={location_header}, Retry-After={retry_after}")
                else:
                    issues.append(f"Expected 201 or 202, got {response.status}")
                
                if issues:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "issues": issues,
                        "status_code": response.status,
                        "headers": dict(response.headers),
                        "response_keys": list(data.keys()) if isinstance(data, dict) else "not_dict"
                    }
                    print(f"   ❌ Query submission issues: {issues}")
                else:
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "status_code": response.status,
                        "location_header": location_header,
                        "response_type": "sync" if response.status == 201 else "async"
                    }
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Query submission test error: {e}")
            
        results["tests"]["submit_query"] = test_result
    
    async def _test_list_session_cases(self, results: Dict[str, Any]):
        """Test session cases listing (immediate visibility)"""
        print("\n4️⃣  Testing session cases listing...")
        
        if not self.session_id:
            results["tests"]["list_session_cases"] = {
                "status": "SKIP", 
                "details": {"reason": "No session_id from previous test"}
            }
            return
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            async with self.session.get(
                f"{self.base_url}/api/v1/sessions/{self.session_id}/cases",
                headers={"X-User-Id": self.user_id}
            ) as response:
                
                data = await response.json()
                total_count_header = response.headers.get("X-Total-Count")
                
                issues = []
                
                if response.status != 200:
                    issues.append(f"Expected 200, got {response.status}")
                
                if not total_count_header:
                    issues.append("Missing X-Total-Count header")
                
                if "cases" not in data:
                    issues.append("Response missing 'cases' field")
                elif not isinstance(data["cases"], list):
                    issues.append(f"'cases' field is not array: {type(data['cases'])}")
                else:
                    cases_list = data["cases"]
                    if self.case_id:
                        # Check if our created case is immediately visible
                        case_ids = [case.get("case_id") for case in cases_list]
                        if self.case_id not in case_ids:
                            issues.append(f"Created case {self.case_id} not immediately visible in session cases")
                
                if issues:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "issues": issues,
                        "status_code": response.status,
                        "total_count_header": total_count_header,
                        "cases_count": len(data.get("cases", [])),
                        "case_ids": [c.get("case_id") for c in data.get("cases", [])]
                    }
                    print(f"   ❌ Session cases listing issues: {issues}")
                else:
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "status_code": response.status,
                        "total_count": total_count_header,
                        "cases_count": len(data["cases"]),
                        "immediate_visibility": self.case_id in [c.get("case_id") for c in data["cases"]] if self.case_id else None
                    }
                    print(f"   ✅ Session cases: {len(data['cases'])} cases, X-Total-Count={total_count_header}")
                    if self.case_id:
                        print(f"   ✅ Created case immediately visible: {self.case_id}")
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Session cases test error: {e}")
            
        results["tests"]["list_session_cases"] = test_result
    
    async def _test_list_cases(self, results: Dict[str, Any]):
        """Test cases listing (array response with headers)"""
        print("\n5️⃣  Testing global cases listing...")
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            async with self.session.get(
                f"{self.base_url}/api/v1/cases?limit=50&offset=0",
                headers={"X-User-Id": self.user_id}
            ) as response:
                
                data = await response.json()
                total_count_header = response.headers.get("X-Total-Count")
                
                issues = []
                
                if response.status != 200:
                    issues.append(f"Expected 200, got {response.status}")
                
                if not isinstance(data, list):
                    issues.append(f"Response is not array: {type(data)} - Frontend expects direct array parsing")
                
                if not total_count_header:
                    issues.append("Missing X-Total-Count header")
                
                if issues:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "issues": issues,
                        "status_code": response.status,
                        "response_type": type(data).__name__,
                        "total_count_header": total_count_header,
                        "is_array": isinstance(data, list)
                    }
                    print(f"   ❌ Cases listing issues: {issues}")
                else:
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "status_code": response.status,
                        "total_count": total_count_header,
                        "cases_count": len(data),
                        "is_array": True
                    }
                    print(f"   ✅ Cases listing: {len(data)} cases, array response ✅, X-Total-Count={total_count_header}")
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Cases listing test error: {e}")
            
        results["tests"]["list_cases"] = test_result
    
    async def _test_pre_auth_probe(self, results: Dict[str, Any]):
        """Test pre-auth behavior (401 not 500)"""
        print("\n6️⃣  Testing pre-auth probe...")
        
        test_result = {"status": "UNKNOWN", "details": {}}
        
        try:
            # Test without authentication headers (simulates pre-auth state)
            async with self.session.get(
                f"{self.base_url}/api/v1/cases/nonexistent-case-id"
                # No X-User-Id header to simulate pre-auth
            ) as response:
                
                data = await response.json() if response.content_type == "application/json" else {"error": "not_json"}
                
                issues = []
                
                if response.status == 500:
                    issues.append("Pre-auth probe returned 500 - should be 401/403 for service unavailable")
                elif response.status not in [401, 403, 404]:
                    issues.append(f"Expected 401/403/404 for pre-auth, got {response.status}")
                
                if response.status == 500:
                    test_result["status"] = "FAIL"
                    test_result["details"] = {
                        "issues": issues,
                        "status_code": response.status,
                        "critical_violation": "500_for_pre_auth"
                    }
                    print(f"   ❌ CRITICAL: Pre-auth probe returned 500 instead of 401/403")
                else:
                    test_result["status"] = "PASS"
                    test_result["details"] = {
                        "status_code": response.status,
                        "correct_behavior": "no_500_for_pre_auth"
                    }
                    print(f"   ✅ Pre-auth probe: {response.status} (not 500) ✅")
                    
        except Exception as e:
            test_result["status"] = "ERROR"
            test_result["details"] = {"error": str(e)}
            print(f"   ❌ Pre-auth probe test error: {e}")
            
        results["tests"]["pre_auth_probe"] = test_result

    def print_results_summary(self, results: Dict[str, Any]):
        """Print detailed test results summary"""
        
        print("\n" + "=" * 60)
        print("🎯 FRONTEND VERIFICATION SMOKE TEST RESULTS")
        print("=" * 60)
        
        overall_status = results["overall_status"]
        status_icon = "✅" if overall_status == "PASS" else ("❌" if overall_status == "FAIL" else "⚠️")
        
        print(f"\n{status_icon} Overall Status: {overall_status}")
        
        if overall_status == "PASS":
            print("🚀 All contract compliance tests passed")
            print("✅ Frontend can use strict parsing per spec")
            print("✅ No client workarounds needed")
        elif overall_status == "FAIL":
            print("🚫 Contract violations detected")
            print("⚠️  Frontend workarounds may be required")
            failed_tests = results.get("failed_tests", [])
            print(f"❌ Failed tests: {', '.join(failed_tests)}")
        
        print(f"\n📊 Test Summary:")
        for test_name, test_result in results["tests"].items():
            status_icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️", "ERROR": "🔥"}.get(test_result["status"], "❓")
            print(f"  {status_icon} {test_name}: {test_result['status']}")
            
            if test_result["status"] in ["FAIL", "ERROR"]:
                details = test_result.get("details", {})
                if "issues" in details:
                    for issue in details["issues"]:
                        print(f"     • {issue}")
                elif "error" in details:
                    print(f"     • Error: {details['error']}")
        
        print(f"\n🕐 Test completed at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(results['timestamp']))}")


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="FaultMaven Frontend Verification Smoke Test")
    parser.add_argument("--backend-url", default="http://localhost:8000", 
                       help="Backend URL (default: http://localhost:8000)")
    parser.add_argument("--output-json", help="Save results to JSON file")
    
    args = parser.parse_args()
    
    tester = FrontendVerificationTest(base_url=args.backend_url)
    results = await tester.run_smoke_test()
    
    tester.print_results_summary(results)
    
    if args.output_json:
        with open(args.output_json, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n📄 Results saved to: {args.output_json}")
    
    # Exit with appropriate code for CI
    exit_code = 0 if results["overall_status"] == "PASS" else 1
    return exit_code


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))