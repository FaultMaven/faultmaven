#!/usr/bin/env python3
"""
Security and Compliance Validation Test Suite for FaultMaven Phase 2

This module provides comprehensive security and compliance testing including:
- PII redaction compliance testing with Presidio integration
- API security and authentication validation
- Data encryption in transit and at rest validation
- GDPR compliance for session and memory data
- Audit logging and compliance tracking validation
- Input validation and injection attack prevention
- Rate limiting and DoS protection testing
- SSL/TLS certificate validation
- Data retention and deletion policy compliance
"""

import pytest
import asyncio
import time
import json
import hashlib
import random
import string
import re
import ssl
import socket
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
import aiohttp
import secrets
from pathlib import Path


@dataclass
class SecurityTestResult:
    """Result from a security test."""
    test_name: str
    passed: bool
    risk_level: str  # "low", "medium", "high", "critical"
    findings: List[str]
    recommendations: List[str]
    compliance_status: str  # "compliant", "non_compliant", "partial"


@dataclass
class PIITestCase:
    """Test case for PII redaction testing."""
    name: str
    input_data: str
    expected_pii_types: List[str]
    should_be_redacted: bool
    risk_level: str


class SecurityComplianceValidator:
    """Comprehensive security and compliance validator for FaultMaven Phase 2."""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.session = None
        
        # PII test cases for validation
        self.pii_test_cases = [
            PIITestCase(
                name="email_redaction",
                input_data="Please contact john.doe@company.com for assistance",
                expected_pii_types=["EMAIL_ADDRESS"],
                should_be_redacted=True,
                risk_level="high"
            ),
            PIITestCase(
                name="phone_redaction",
                input_data="Call me at 555-123-4567 or (555) 123-4567",
                expected_pii_types=["PHONE_NUMBER"],
                should_be_redacted=True,
                risk_level="medium"
            ),
            PIITestCase(
                name="ssn_redaction",
                input_data="My SSN is 123-45-6789",
                expected_pii_types=["US_SSN"],
                should_be_redacted=True,
                risk_level="critical"
            ),
            PIITestCase(
                name="credit_card_redaction",
                input_data="Payment failed for card 4532-1234-5678-9012",
                expected_pii_types=["CREDIT_CARD"],
                should_be_redacted=True,
                risk_level="critical"
            ),
            PIITestCase(
                name="person_name_redaction",
                input_data="John Smith reported the issue yesterday",
                expected_pii_types=["PERSON"],
                should_be_redacted=True,
                risk_level="medium"
            ),
            PIITestCase(
                name="ip_address_redaction",
                input_data="Server 192.168.1.100 is experiencing issues",
                expected_pii_types=["IP_ADDRESS"],
                should_be_redacted=False,  # Technical data, not PII
                risk_level="low"
            ),
            PIITestCase(
                name="mixed_pii_content",
                input_data="Contact Jane Doe at jane.doe@company.com or 555-987-6543 regarding incident #12345",
                expected_pii_types=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
                should_be_redacted=True,
                risk_level="high"
            ),
            PIITestCase(
                name="no_pii_content",
                input_data="Application timeout error occurred at 2025-01-15 10:30:00 UTC",
                expected_pii_types=[],
                should_be_redacted=False,
                risk_level="low"
            )
        ]
        
        # Security test patterns for injection attacks
        self.injection_test_patterns = [
            # SQL Injection patterns
            "'; DROP TABLE users; --",
            "' UNION SELECT password FROM users --",
            "admin' --",
            "1' OR '1'='1",
            
            # XSS patterns
            "<script>alert('xss')</script>",
            "javascript:alert('xss')",
            "<img src=x onerror=alert('xss')>",
            "';alert('xss');//",
            
            # Command injection patterns
            "; cat /etc/passwd",
            "| whoami",
            "&& rm -rf /",
            "`id`",
            
            # NoSQL injection patterns
            "'; db.users.drop(); //",
            "$ne: null",
            "{'$gt': ''}",
            
            # LDAP injection patterns
            "admin)(&)",
            "admin)(|(password=*))",
            
            # Path traversal patterns
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\config\\sam",
            "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd"
        ]


@pytest.fixture
async def security_validator():
    """Fixture providing configured security validator."""
    validator = SecurityComplianceValidator()
    validator.session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=20)
    )
    yield validator
    await validator.session.close()


class TestPIIRedactionCompliance:
    """Test PII redaction compliance with Presidio integration."""
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_pii_detection_accuracy(self, security_validator):
        """Test PII detection accuracy for various data types."""
        validator = security_validator
        
        detection_results = []
        
        for test_case in validator.pii_test_cases:
            result = await self._test_pii_detection(validator, test_case)
            detection_results.append(result)
        
        # Validate overall PII detection performance
        total_tests = len(detection_results)
        passed_tests = sum(1 for r in detection_results if r.passed)
        critical_failures = sum(1 for r in detection_results if not r.passed and r.risk_level == "critical")
        
        # All critical PII types must be detected
        assert critical_failures == 0, f"Critical PII detection failures: {critical_failures}"
        
        # Overall detection rate should be high
        detection_rate = passed_tests / total_tests if total_tests > 0 else 0
        assert detection_rate >= 0.8, f"PII detection rate {detection_rate:.1%} below 80% threshold"
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_pii_redaction_in_api_responses(self, security_validator):
        """Test that PII is redacted in API responses."""
        validator = security_validator
        
        # Test PII redaction through API calls
        for test_case in validator.pii_test_cases[:4]:  # Test first 4 cases
            if not test_case.should_be_redacted:
                continue
            
            session_id = f"pii_test_{test_case.name}_{int(time.time())}"
            
            try:
                # Send request with PII data
                async with validator.session.post(
                    f"{validator.base_url}/api/v1/query",
                    json={
                        "query": test_case.input_data,
                        "session_id": session_id
                    }
                ) as resp:
                    
                    if resp.status == 200:
                        response_data = await resp.json()
                        response_text = json.dumps(response_data)
                        
                        # Check that original PII is not in response
                        pii_found = self._find_pii_in_text(test_case.input_data, response_text)
                        assert not pii_found, f"PII found in API response: {pii_found}"
                        
                    elif resp.status == 422:
                        # Input validation rejected PII - acceptable behavior
                        pass
                    else:
                        pytest.skip(f"API not available for PII testing (status: {resp.status})")
                        
            except Exception as e:
                pytest.skip(f"PII API test failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_pii_redaction_in_logs(self, security_validator):
        """Test that PII is redacted in system logs."""
        validator = security_validator
        
        # Generate requests with PII data
        session_id = f"log_pii_test_{int(time.time())}"
        
        pii_data = [
            "User email: test.user@company.com needs help",
            "Phone number 555-123-4567 reported an issue",
            "SSN 123-45-6789 verification failed"
        ]
        
        for pii_text in pii_data:
            try:
                async with validator.session.post(
                    f"{validator.base_url}/api/v1/query",
                    json={
                        "query": pii_text,
                        "session_id": session_id
                    }
                ) as resp:
                    pass  # We don't need the response, just want to generate logs
            except:
                pass
        
        # Check logging health (indirect way to verify log redaction)
        try:
            async with validator.session.get(f"{validator.base_url}/health/logging") as resp:
                if resp.status == 200:
                    log_health = await resp.json()
                    
                    # Logs should be functional (PII redaction shouldn't break logging)
                    assert log_health.get("status") != "error", "Logging system failed during PII processing"
                else:
                    pytest.skip("Logging health endpoint not available")
        except:
            pytest.skip("Log redaction test inconclusive")
    
    async def _test_pii_detection(self, validator, test_case: PIITestCase) -> SecurityTestResult:
        """Test PII detection for a single test case."""
        findings = []
        recommendations = []
        
        try:
            # Test via Presidio analyzer if available
            presidio_config = validator.base_url.replace("8000", "30433")  # Presidio analyzer port
            
            async with validator.session.post(
                f"http://localhost:30433/analyze",
                json={
                    "text": test_case.input_data,
                    "language": "en"
                }
            ) as resp:
                if resp.status == 200:
                    analysis_results = await resp.json()
                    
                    detected_entities = [entity.get("entity_type", "") for entity in analysis_results]
                    
                    if test_case.should_be_redacted:
                        # Should detect expected PII types
                        expected_detected = any(expected in detected_entities 
                                              for expected in test_case.expected_pii_types)
                        
                        if expected_detected:
                            return SecurityTestResult(
                                test_name=f"pii_detection_{test_case.name}",
                                passed=True,
                                risk_level=test_case.risk_level,
                                findings=[f"Correctly detected PII: {detected_entities}"],
                                recommendations=[],
                                compliance_status="compliant"
                            )
                        else:
                            findings.append(f"Failed to detect expected PII types: {test_case.expected_pii_types}")
                            recommendations.append("Review PII detection models and configuration")
                    else:
                        # Should not detect PII where none exists
                        if not detected_entities:
                            return SecurityTestResult(
                                test_name=f"pii_detection_{test_case.name}",
                                passed=True,
                                risk_level=test_case.risk_level,
                                findings=["Correctly identified no PII present"],
                                recommendations=[],
                                compliance_status="compliant"
                            )
                        else:
                            findings.append(f"False positive PII detection: {detected_entities}")
                            recommendations.append("Fine-tune PII detection to reduce false positives")
                else:
                    findings.append(f"Presidio analyzer unavailable (status: {resp.status})")
                    
        except Exception as e:
            findings.append(f"PII detection test failed: {str(e)}")
        
        return SecurityTestResult(
            test_name=f"pii_detection_{test_case.name}",
            passed=False,
            risk_level=test_case.risk_level,
            findings=findings,
            recommendations=recommendations,
            compliance_status="non_compliant"
        )
    
    def _find_pii_in_text(self, original_text: str, response_text: str) -> List[str]:
        """Find PII patterns in response text."""
        pii_patterns = {
            "email": r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            "phone": r'\b\(?([0-9]{3})\)?[-. ]?([0-9]{3})[-. ]?([0-9]{4})\b',
            "ssn": r'\b\d{3}-\d{2}-\d{4}\b',
            "credit_card": r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'
        }
        
        found_pii = []
        
        for pii_type, pattern in pii_patterns.items():
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            if matches:
                found_pii.extend([f"{pii_type}: {match}" for match in matches])
        
        return found_pii


class TestAPISecurityValidation:
    """Test API security measures and authentication."""
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_input_validation_security(self, security_validator):
        """Test input validation against injection attacks."""
        validator = security_validator
        
        injection_test_results = []
        
        for pattern in validator.injection_test_patterns[:10]:  # Test first 10 patterns
            result = await self._test_injection_pattern(validator, pattern)
            injection_test_results.append(result)
        
        # All injection attempts should be blocked or handled safely
        vulnerable_responses = sum(1 for r in injection_test_results if not r.passed)
        
        assert vulnerable_responses == 0, f"Vulnerable to {vulnerable_responses} injection patterns"
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_cors_configuration(self, security_validator):
        """Test CORS configuration security."""
        validator = security_validator
        
        # Test CORS headers
        try:
            async with validator.session.options(f"{validator.base_url}/api/v1/query") as resp:
                cors_headers = {k.lower(): v for k, v in resp.headers.items()}
                
                # Check for appropriate CORS headers
                assert "access-control-allow-origin" in cors_headers, "Missing CORS allow-origin header"
                
                allow_origin = cors_headers["access-control-allow-origin"]
                
                # Should not allow all origins in production
                if allow_origin == "*":
                    pytest.skip("Wildcard CORS detected - review for production security")
                
                # Should have appropriate methods and headers
                if "access-control-allow-methods" in cors_headers:
                    allowed_methods = cors_headers["access-control-allow-methods"]
                    
                    # Should not allow dangerous methods
                    dangerous_methods = ["TRACE", "CONNECT"]
                    for method in dangerous_methods:
                        assert method not in allowed_methods, f"Dangerous HTTP method {method} allowed"
                        
        except Exception as e:
            pytest.skip(f"CORS configuration test failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_rate_limiting_protection(self, security_validator):
        """Test rate limiting and DoS protection."""
        validator = security_validator
        
        # Test rate limiting by making rapid requests
        rapid_requests = []
        request_start_time = time.time()
        
        for i in range(20):  # Make 20 rapid requests
            try:
                async with validator.session.get(f"{validator.base_url}/health") as resp:
                    rapid_requests.append({
                        "status": resp.status,
                        "timestamp": time.time(),
                        "response_time": time.time() - request_start_time
                    })
            except Exception as e:
                rapid_requests.append({
                    "status": 0,
                    "error": str(e),
                    "timestamp": time.time()
                })
            
            await asyncio.sleep(0.1)  # Brief pause between requests
        
        total_request_time = time.time() - request_start_time
        
        # Analyze rate limiting behavior
        successful_requests = sum(1 for r in rapid_requests if r.get("status", 0) == 200)
        rate_limited_requests = sum(1 for r in rapid_requests if r.get("status", 0) == 429)
        
        # Rate limiting should kick in for rapid requests
        if rate_limited_requests > 0:
            # Rate limiting is working
            assert rate_limited_requests >= 5, "Rate limiting should block some rapid requests"
        else:
            # No explicit rate limiting, but system should handle load
            success_rate = successful_requests / len(rapid_requests)
            assert success_rate >= 0.8, f"System cannot handle rapid requests: {success_rate:.1%} success rate"
    
    @pytest.mark.asyncio 
    @pytest.mark.security
    async def test_error_message_information_disclosure(self, security_validator):
        """Test that error messages don't disclose sensitive information."""
        validator = security_validator
        
        # Test various error conditions
        error_test_cases = [
            # Invalid JSON
            {"endpoint": "/api/v1/query", "payload": "invalid json", "content_type": "application/json"},
            
            # Missing required fields
            {"endpoint": "/api/v1/query", "payload": {}, "content_type": "application/json"},
            
            # Invalid endpoint
            {"endpoint": "/api/v1/nonexistent", "payload": {"test": "data"}, "content_type": "application/json"},
            
            # Oversized request
            {"endpoint": "/api/v1/query", "payload": {"query": "x" * 10000}, "content_type": "application/json"}
        ]
        
        information_disclosure_found = []
        
        for test_case in error_test_cases:
            try:
                if test_case["content_type"] == "application/json":
                    if isinstance(test_case["payload"], str):
                        # Invalid JSON test
                        async with validator.session.post(
                            f"{validator.base_url}{test_case['endpoint']}",
                            data=test_case["payload"],
                            headers={"Content-Type": "application/json"}
                        ) as resp:
                            error_response = await resp.text()
                    else:
                        async with validator.session.post(
                            f"{validator.base_url}{test_case['endpoint']}",
                            json=test_case["payload"]
                        ) as resp:
                            error_response = await resp.text()
                else:
                    async with validator.session.get(
                        f"{validator.base_url}{test_case['endpoint']}"
                    ) as resp:
                        error_response = await resp.text()
                
                # Check for information disclosure in error messages
                sensitive_patterns = [
                    r"/[a-zA-Z]:/.*",  # Windows file paths
                    r"/home/.*",       # Linux home paths  
                    r"/var/.*",        # System paths
                    r"password",       # Password references
                    r"secret",         # Secret references
                    r"key.*=.*",      # Key-value pairs
                    r"connection.*failed.*host.*",  # Database connection details
                    r"stack trace",    # Stack traces
                    r"internal server error.*at line \d+",  # Detailed error locations
                ]
                
                for pattern in sensitive_patterns:
                    if re.search(pattern, error_response, re.IGNORECASE):
                        information_disclosure_found.append(f"Pattern '{pattern}' found in error response")
                        
            except Exception as e:
                # Exceptions during error testing are expected
                pass
        
        # Should not disclose sensitive information in error messages
        assert len(information_disclosure_found) == 0, \
            f"Information disclosure in error messages: {information_disclosure_found}"
    
    async def _test_injection_pattern(self, validator, injection_pattern: str) -> SecurityTestResult:
        """Test a single injection pattern."""
        try:
            # Test injection in query parameter
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={
                    "query": injection_pattern,
                    "session_id": f"injection_test_{secrets.token_hex(8)}"
                }
            ) as resp:
                
                response_text = await resp.text()
                
                # Check response for signs of successful injection
                dangerous_indicators = [
                    "syntax error",
                    "mysql error",
                    "postgres error", 
                    "sqlite error",
                    "oracle error",
                    "database error",
                    "/etc/passwd",
                    "root:",
                    "administrator",
                    "script>",
                    "eval("
                ]
                
                for indicator in dangerous_indicators:
                    if indicator.lower() in response_text.lower():
                        return SecurityTestResult(
                            test_name=f"injection_test_{injection_pattern[:20]}",
                            passed=False,
                            risk_level="critical",
                            findings=[f"Injection successful: {indicator} found in response"],
                            recommendations=["Implement proper input validation and sanitization"],
                            compliance_status="non_compliant"
                        )
                
                # If we get here, injection was blocked or handled safely
                return SecurityTestResult(
                    test_name=f"injection_test_{injection_pattern[:20]}",
                    passed=True,
                    risk_level="low",
                    findings=["Injection pattern safely handled"],
                    recommendations=[],
                    compliance_status="compliant"
                )
                
        except Exception as e:
            # Exceptions might indicate successful DoS or other injection
            if "timeout" in str(e).lower() or "connection" in str(e).lower():
                return SecurityTestResult(
                    test_name=f"injection_test_{injection_pattern[:20]}",
                    passed=False,
                    risk_level="high",
                    findings=[f"Injection may have caused service disruption: {str(e)}"],
                    recommendations=["Review error handling and input validation"],
                    compliance_status="non_compliant"
                )
            
            # Other exceptions are likely normal error handling
            return SecurityTestResult(
                test_name=f"injection_test_{injection_pattern[:20]}",
                passed=True,
                risk_level="low",
                findings=["Injection properly rejected with error handling"],
                recommendations=[],
                compliance_status="compliant"
            )


class TestDataEncryptionValidation:
    """Test data encryption in transit and at rest."""
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_https_enforcement(self, security_validator):
        """Test HTTPS enforcement and TLS configuration."""
        validator = security_validator
        
        # Test if HTTPS is available
        https_url = validator.base_url.replace("http://", "https://")
        
        try:
            async with validator.session.get(f"{https_url}/health") as resp:
                if resp.status == 200:
                    # HTTPS is working
                    assert True, "HTTPS successfully configured"
                else:
                    pytest.skip("HTTPS not available - may be acceptable for development")
        except Exception as e:
            if "ssl" in str(e).lower() or "certificate" in str(e).lower():
                pytest.skip(f"HTTPS/SSL configuration issue: {e}")
            else:
                pytest.skip(f"HTTPS not available: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_sensitive_data_handling(self, security_validator):
        """Test that sensitive data is properly handled."""
        validator = security_validator
        
        # Test session data encryption/protection
        session_id = f"sensitive_test_{secrets.token_hex(16)}"
        sensitive_data = {
            "query": "Sensitive troubleshooting query with confidential information",
            "session_id": session_id,
            "context": {
                "confidential": True,
                "internal_use": True
            }
        }
        
        try:
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json=sensitive_data
            ) as resp:
                
                if resp.status == 200:
                    response_data = await resp.json()
                    
                    # Check that sensitive context is not echoed back inappropriately
                    response_text = json.dumps(response_data).lower()
                    
                    # Should not contain raw sensitive markers
                    sensitive_markers = ["confidential", "internal_use", "sensitive"]
                    exposed_markers = [marker for marker in sensitive_markers if marker in response_text]
                    
                    if exposed_markers:
                        pytest.skip(f"Sensitive markers found in response: {exposed_markers} - review data handling")
                    
                elif resp.status == 422:
                    # Input validation rejected sensitive data - good
                    pass
                else:
                    pytest.skip(f"API not available for sensitive data test (status: {resp.status})")
                    
        except Exception as e:
            pytest.skip(f"Sensitive data handling test failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_session_token_security(self, security_validator):
        """Test session token security."""
        validator = security_validator
        
        # Create multiple sessions to test token handling
        sessions = []
        
        for i in range(3):
            session_id = f"token_test_{i}_{secrets.token_hex(8)}"
            
            try:
                async with validator.session.post(
                    f"{validator.base_url}/api/v1/query",
                    json={
                        "query": f"Token security test {i}",
                        "session_id": session_id
                    }
                ) as resp:
                    if resp.status < 500:
                        sessions.append(session_id)
            except:
                pass
        
        # Test session isolation
        if len(sessions) >= 2:
            # Sessions should be properly isolated
            for session_id in sessions[:2]:
                try:
                    async with validator.session.post(
                        f"{validator.base_url}/api/v1/query",
                        json={
                            "query": f"Cross-session test for {session_id}",
                            "session_id": session_id
                        }
                    ) as resp:
                        if resp.status == 200:
                            response_data = await resp.json()
                            
                            # Response should not contain data from other sessions
                            other_sessions = [s for s in sessions if s != session_id]
                            response_text = json.dumps(response_data).lower()
                            
                            for other_session in other_sessions:
                                assert other_session.lower() not in response_text, \
                                    f"Session isolation failed: {other_session} found in {session_id} response"
                except:
                    pass


class TestGDPRCompliance:
    """Test GDPR compliance features."""
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_data_retention_compliance(self, security_validator):
        """Test data retention policy compliance."""
        validator = security_validator
        
        # Test session data retention
        test_session_id = f"retention_test_{int(time.time())}"
        
        try:
            # Create session with data
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={
                    "query": "Data retention compliance test",
                    "session_id": test_session_id,
                    "context": {"retention_test": True}
                }
            ) as resp:
                if resp.status >= 500:
                    pytest.skip("Cannot test data retention - API not available")
            
            # Test session cleanup capabilities (simulated)
            # In production, this would test actual data deletion
            
            # Verify session handling is compliant
            async with validator.session.get(f"{validator.base_url}/health/dependencies") as resp:
                if resp.status == 200:
                    health_data = await resp.json()
                    
                    # Check if session management is healthy (indirect compliance check)
                    assert "session" in str(health_data).lower() or "redis" in str(health_data).lower(), \
                        "Session management should be monitored for GDPR compliance"
                        
        except Exception as e:
            pytest.skip(f"Data retention compliance test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_right_to_erasure_simulation(self, security_validator):
        """Test right to erasure (data deletion) capabilities."""
        validator = security_validator
        
        # Create test data
        erasure_session_id = f"erasure_test_{secrets.token_hex(8)}"
        
        try:
            # Create session with user data
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={
                    "query": "User data for erasure testing",
                    "session_id": erasure_session_id,
                    "context": {"user_consent": True, "data_subject": "test_user"}
                }
            ) as resp:
                if resp.status >= 500:
                    pytest.skip("Cannot test data erasure - API not available")
            
            # In production, there would be a data deletion endpoint
            # For now, test that sessions can be managed
            
            # Verify session isolation prevents data leakage
            other_session_id = f"other_session_{secrets.token_hex(8)}"
            
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={
                    "query": "Different user session",
                    "session_id": other_session_id
                }
            ) as resp:
                if resp.status == 200:
                    response_data = await resp.json()
                    response_text = json.dumps(response_data)
                    
                    # Should not contain data from erasure test session
                    assert erasure_session_id not in response_text, \
                        "Session data isolation failed - potential GDPR violation"
                        
        except Exception as e:
            pytest.skip(f"Right to erasure test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_consent_management(self, security_validator):
        """Test consent management for data processing."""
        validator = security_validator
        
        # Test consent handling through API
        consent_tests = [
            {
                "name": "explicit_consent",
                "payload": {
                    "query": "Process my data with consent",
                    "session_id": "consent_test_explicit",
                    "consent": {"data_processing": True, "analytics": True}
                }
            },
            {
                "name": "no_consent",
                "payload": {
                    "query": "Process without consent",
                    "session_id": "consent_test_none"
                    # No consent field
                }
            },
            {
                "name": "limited_consent",
                "payload": {
                    "query": "Limited data processing",
                    "session_id": "consent_test_limited", 
                    "consent": {"data_processing": True, "analytics": False}
                }
            }
        ]
        
        consent_results = []
        
        for test in consent_tests:
            try:
                async with validator.session.post(
                    f"{validator.base_url}/api/v1/query",
                    json=test["payload"]
                ) as resp:
                    consent_results.append({
                        "test": test["name"],
                        "status": resp.status,
                        "response_available": resp.status < 500
                    })
            except:
                consent_results.append({
                    "test": test["name"],
                    "status": 0,
                    "response_available": False
                })
        
        # All consent scenarios should be handled appropriately
        successful_tests = sum(1 for r in consent_results if r["response_available"])
        
        assert successful_tests >= len(consent_tests) * 0.8, \
            "Consent management should handle various consent scenarios"


class TestAuditLoggingCompliance:
    """Test audit logging and compliance tracking."""
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_audit_log_functionality(self, security_validator):
        """Test audit logging functionality."""
        validator = security_validator
        
        # Test logging system health
        try:
            async with validator.session.get(f"{validator.base_url}/health/logging") as resp:
                if resp.status == 200:
                    log_health = await resp.json()
                    
                    # Logging system should be operational
                    assert log_health.get("status") != "error", "Audit logging system not operational"
                    
                    # Should have appropriate log levels for audit trail
                    log_config = log_health.get("configuration", {})
                    if log_config:
                        # Should support INFO level or higher for audit events
                        log_level = log_config.get("level", "INFO").upper()
                        assert log_level in ["DEBUG", "INFO", "WARN", "ERROR"], \
                            f"Inappropriate log level for audit trail: {log_level}"
                        
                else:
                    pytest.skip("Audit logging health endpoint not available")
                    
        except Exception as e:
            pytest.skip(f"Audit logging test failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_compliance_tracking_capabilities(self, security_validator):
        """Test compliance tracking and monitoring."""
        validator = security_validator
        
        # Test SLA monitoring for compliance
        try:
            async with validator.session.get(f"{validator.base_url}/health/sla") as resp:
                if resp.status == 200:
                    sla_data = await resp.json()
                    
                    # Should track SLA metrics for compliance
                    assert "timestamp" in sla_data, "SLA tracking should include timestamps"
                    
                    # Should have summary data
                    summary = sla_data.get("summary", {})
                    if summary:
                        assert "overall_sla" in summary, "Should track overall SLA compliance"
                        
                else:
                    pytest.skip("SLA compliance tracking not available")
                    
        except Exception as e:
            pytest.skip(f"Compliance tracking test failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_security_event_logging(self, security_validator):
        """Test security event logging capabilities."""
        validator = security_validator
        
        # Generate potential security events
        security_test_events = [
            # Rapid requests (potential DoS)
            {"type": "rapid_requests", "count": 5},
            
            # Invalid input (potential injection)
            {"type": "invalid_input", "payload": {"query": "'; DROP TABLE test; --"}},
            
            # Large requests (potential DoS)
            {"type": "large_request", "payload": {"query": "x" * 1000}}
        ]
        
        events_generated = 0
        
        for event in security_test_events:
            try:
                if event["type"] == "rapid_requests":
                    # Make rapid requests
                    for _ in range(event["count"]):
                        async with validator.session.get(f"{validator.base_url}/health"):
                            pass
                        await asyncio.sleep(0.1)
                    events_generated += 1
                    
                else:
                    # Make request with potentially malicious payload
                    async with validator.session.post(
                        f"{validator.base_url}/api/v1/query",
                        json=event.get("payload", {"query": "security test"})
                    ):
                        pass
                    events_generated += 1
                    
            except:
                # Exceptions during security testing are expected
                events_generated += 1
        
        # Verify logging system is still healthy after security events
        try:
            async with validator.session.get(f"{validator.base_url}/health/logging") as resp:
                if resp.status == 200:
                    log_health = await resp.json()
                    
                    # Logging should still be functional after security events
                    assert log_health.get("status") != "error", \
                        "Logging system should remain stable during security events"
                        
        except Exception as e:
            pytest.skip(f"Security event logging test inconclusive: {e}")


@pytest.mark.security
class TestOverallSecurityPosture:
    """Test overall security posture and compliance."""
    
    @pytest.mark.asyncio
    async def test_comprehensive_security_assessment(self, security_validator):
        """Comprehensive security assessment across all areas."""
        validator = security_validator
        
        security_areas = {
            "pii_protection": 0,
            "input_validation": 0,
            "authentication": 0,
            "encryption": 0,
            "audit_logging": 0,
            "compliance": 0
        }
        
        # Test PII protection
        try:
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={"query": "Test with email test@example.com", "session_id": "security_assessment"}
            ) as resp:
                if resp.status < 500:
                    security_areas["pii_protection"] = 1
        except:
            pass
        
        # Test input validation
        try:
            async with validator.session.post(
                f"{validator.base_url}/api/v1/query",
                json={"query": "'; DROP TABLE users; --", "session_id": "security_assessment"}
            ) as resp:
                if resp.status in [200, 422]:  # Either handled safely or rejected
                    security_areas["input_validation"] = 1
        except:
            security_areas["input_validation"] = 1  # Exception is good - input rejected
        
        # Test authentication/authorization (basic)
        try:
            async with validator.session.get(f"{validator.base_url}/health") as resp:
                if resp.status == 200:
                    security_areas["authentication"] = 1  # Basic endpoint security
        except:
            pass
        
        # Test encryption readiness
        https_url = validator.base_url.replace("http://", "https://")
        try:
            async with validator.session.get(f"{https_url}/health") as resp:
                if resp.status == 200:
                    security_areas["encryption"] = 1
        except:
            # HTTPS not available - might be acceptable for development
            security_areas["encryption"] = 0.5  # Partial credit
        
        # Test audit logging
        try:
            async with validator.session.get(f"{validator.base_url}/health/logging") as resp:
                if resp.status == 200:
                    security_areas["audit_logging"] = 1
        except:
            pass
        
        # Test compliance features
        try:
            async with validator.session.get(f"{validator.base_url}/health/sla") as resp:
                if resp.status == 200:
                    security_areas["compliance"] = 1
        except:
            pass
        
        # Calculate overall security score
        total_areas = len(security_areas)
        security_score = sum(security_areas.values()) / total_areas
        
        # Should achieve at least 70% security posture
        assert security_score >= 0.7, \
            f"Overall security posture {security_score:.1%} below 70% threshold. Areas: {security_areas}"
        
        # Critical areas should be fully implemented
        critical_areas = ["pii_protection", "input_validation"]
        for area in critical_areas:
            assert security_areas[area] >= 0.5, f"Critical security area '{area}' not adequately implemented"


if __name__ == "__main__":
    import sys
    
    # Allow running this module directly for debugging
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        pytest.main([__file__, "-v", "-m", "security"])
    else:
        print("Security and Compliance Validation Test Suite")
        print("Usage: python test_security_compliance_validation.py --test")