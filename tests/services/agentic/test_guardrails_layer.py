"""
Unit tests for GuardrailsPolicyLayer - multi-layer security validation and PII protection.

This module tests the guardrails system that enforces security boundaries, content policies,
compliance monitoring, and PII protection across all agent interactions.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio

from faultmaven.services.agentic.guardrails_layer import GuardrailsPolicyLayer
from faultmaven.models.agentic import (
    SecurityBoundary, ContentPolicy, ComplianceResult, PIIDetectionResult,
    ValidationRequest, ValidationResult, SecurityLevel, PolicyViolation
)


class TestGuardrailsPolicyLayer:
    """Test suite for Guardrails & Policy Layer."""
    
    @pytest.fixture
    def mock_presidio_client(self):
        """Mock Presidio client for PII detection."""
        mock = AsyncMock()
        mock.analyze.return_value = [
            {'entity_type': 'EMAIL', 'start': 10, 'end': 25, 'score': 0.95},
            {'entity_type': 'PHONE', 'start': 30, 'end': 42, 'score': 0.88}
        ]
        mock.anonymize.return_value = "User email [EMAIL] and phone [PHONE] detected"
        return mock

    @pytest.fixture
    def mock_custom_validators(self):
        """Mock custom validation functions."""
        def validate_business_rules(content):
            if 'confidential' in content.lower():
                return PolicyViolation(
                    rule='confidential_content',
                    severity='high',
                    message='Confidential content detected'
                )
            return None
        
        def validate_compliance(content):
            if len(content) > 10000:
                return PolicyViolation(
                    rule='content_length',
                    severity='medium',
                    message='Content exceeds maximum length'
                )
            return None
            
        return [validate_business_rules, validate_compliance]

    @pytest.fixture
    def guardrails_layer(self, mock_presidio_client, mock_custom_validators):
        """Create guardrails layer with mocked dependencies."""
        return GuardrailsPolicyLayer(
            presidio_client=mock_presidio_client,
            custom_validators=mock_custom_validators
        )

    @pytest.mark.asyncio
    async def test_init_guardrails_layer(self, guardrails_layer):
        """Test guardrails layer initialization."""
        assert guardrails_layer.presidio_client is not None
        assert len(guardrails_layer.custom_validators) > 0
        assert hasattr(guardrails_layer, 'security_policies')
        assert hasattr(guardrails_layer, 'compliance_rules')

    @pytest.mark.asyncio
    async def test_validate_request_clean_content(self, guardrails_layer):
        """Test validation of clean content without violations."""
        content = "Please help me troubleshoot a system performance issue"
        
        result = await guardrails_layer.validate_request(content)
        
        assert isinstance(result, SecurityBoundary)
        assert result.is_safe == True
        assert result.security_level == 'safe'
        assert result.pii_detected == False

    @pytest.mark.asyncio
    async def test_validate_request_with_pii(self, guardrails_layer, mock_presidio_client):
        """Test validation of content containing PII."""
        content = "My email is user@example.com and phone is +1-555-0123"
        
        result = await guardrails_layer.validate_request(content)
        
        assert result.pii_detected == True
        assert '[EMAIL]' in result.redacted_content
        assert '[PHONE]' in result.redacted_content
        
        # Verify Presidio was called
        mock_presidio_client.analyze.assert_called_once()
        mock_presidio_client.anonymize.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_request_policy_violation(self, guardrails_layer):
        """Test validation with policy violations."""
        content = "This is confidential information that should not be processed"
        
        result = await guardrails_layer.validate_request(content)
        
        assert result.is_safe == False
        assert result.security_level in ['moderate', 'dangerous']
        assert len(result.violations) > 0
        assert any('confidential' in v.rule for v in result.violations)

    @pytest.mark.asyncio
    async def test_validate_response_clean(self, guardrails_layer):
        """Test response validation for clean content."""
        response_content = "Here are some troubleshooting steps for your system issue"
        
        result = await guardrails_layer.validate_response(response_content)
        
        assert result.is_safe == True
        assert result.security_level == 'safe'

    @pytest.mark.asyncio
    async def test_validate_response_with_sensitive_data(self, guardrails_layer):
        """Test response validation with accidentally included sensitive data."""
        response_content = "The issue is in config file with password=secret123"
        
        result = await guardrails_layer.validate_response(response_content)
        
        # Should detect and redact sensitive information
        assert 'secret123' not in result.redacted_content
        assert result.pii_detected or len(result.violations) > 0

    @pytest.mark.asyncio
    async def test_pii_detection_comprehensive(self, guardrails_layer, mock_presidio_client):
        """Test comprehensive PII detection across multiple types."""
        content = """
        User details:
        Email: john.doe@company.com
        Phone: +1-555-0123  
        SSN: 123-45-6789
        Credit Card: 4532-1234-5678-9012
        """
        
        # Enhance mock to detect more PII types
        mock_presidio_client.analyze.return_value = [
            {'entity_type': 'EMAIL', 'start': 25, 'end': 45, 'score': 0.95},
            {'entity_type': 'PHONE_NUMBER', 'start': 55, 'end': 67, 'score': 0.90},
            {'entity_type': 'US_SSN', 'start': 75, 'end': 86, 'score': 0.98},
            {'entity_type': 'CREDIT_CARD', 'start': 100, 'end': 119, 'score': 0.92}
        ]
        
        result = await guardrails_layer.validate_request(content)
        
        assert result.pii_detected == True
        assert result.security_level in ['moderate', 'dangerous']

    @pytest.mark.asyncio
    async def test_content_policy_enforcement(self, guardrails_layer):
        """Test content policy enforcement."""
        # Test blocked content types
        harmful_content = "How to hack into systems and steal data"
        
        result = await guardrails_layer.validate_request(harmful_content)
        
        # Should be blocked by content policies
        assert result.is_safe == False
        assert any('harmful' in v.rule or 'security' in v.rule for v in result.violations)

    @pytest.mark.asyncio
    async def test_compliance_monitoring(self, guardrails_layer):
        """Test compliance rule monitoring."""
        # Test content length compliance
        long_content = "x" * 15000  # Exceeds limit
        
        result = await guardrails_layer.validate_request(long_content)
        
        # Should violate length compliance
        assert any('length' in v.rule for v in result.violations)

    @pytest.mark.asyncio
    async def test_security_boundary_levels(self, guardrails_layer):
        """Test different security boundary levels."""
        test_cases = [
            ("Simple troubleshooting question", 'safe'),
            ("Email me at user@example.com", 'moderate'),
            ("Confidential password: secret123", 'dangerous')
        ]
        
        for content, expected_level in test_cases:
            result = await guardrails_layer.validate_request(content)
            assert result.security_level in ['safe', 'moderate', 'dangerous']

    @pytest.mark.asyncio
    async def test_custom_validator_integration(self, guardrails_layer):
        """Test integration with custom validators."""
        content = "This contains confidential business information"
        
        result = await guardrails_layer.validate_request(content)
        
        # Custom validator should catch 'confidential' keyword
        assert any('confidential' in v.rule for v in result.violations)

    @pytest.mark.asyncio
    async def test_whitelisting_bypass(self, guardrails_layer):
        """Test whitelisting mechanism for bypassing certain rules."""
        content = "confidential debugging information for authorized user"
        
        # Test with whitelist token
        result = await guardrails_layer.validate_request(
            content, 
            whitelist_tokens=['authorized_debug']
        )
        
        # Should allow with proper authorization
        assert result.is_safe == True or result.security_level == 'safe'

    @pytest.mark.asyncio
    async def test_contextual_validation(self, guardrails_layer):
        """Test contextual validation based on session context."""
        content = "database password is needed for configuration"
        
        # Different contexts should yield different validation results
        admin_context = {'user_role': 'admin', 'session_type': 'configuration'}
        user_context = {'user_role': 'user', 'session_type': 'troubleshooting'}
        
        admin_result = await guardrails_layer.validate_request(content, context=admin_context)
        user_result = await guardrails_layer.validate_request(content, context=user_context)
        
        # Admin context might be more permissive
        assert admin_result.security_level <= user_result.security_level

    @pytest.mark.asyncio
    async def test_error_handling_presidio_failure(self, guardrails_layer, mock_presidio_client):
        """Test error handling when Presidio service fails."""
        mock_presidio_client.analyze.side_effect = Exception("Presidio service unavailable")
        
        content = "test content with potential PII"
        result = await guardrails_layer.validate_request(content)
        
        # Should fallback gracefully
        assert result is not None
        assert result.security_level in ['moderate', 'dangerous']  # Conservative fallback

    @pytest.mark.asyncio
    async def test_batch_validation(self, guardrails_layer):
        """Test batch validation of multiple content items."""
        content_items = [
            "Clean troubleshooting query",
            "Query with email user@example.com",
            "Confidential system information"
        ]
        
        results = await guardrails_layer.validate_batch(content_items)
        
        assert len(results) == len(content_items)
        assert results[0].is_safe == True
        assert results[1].pii_detected == True
        assert results[2].is_safe == False

    @pytest.mark.asyncio
    async def test_real_time_policy_updates(self, guardrails_layer):
        """Test real-time policy updates."""
        # Add new policy rule
        new_rule = lambda content: PolicyViolation(
            rule='test_rule',
            severity='medium',
            message='Test violation'
        ) if 'test_trigger' in content else None
        
        guardrails_layer.add_custom_validator(new_rule)
        
        content = "This contains test_trigger keyword"
        result = await guardrails_layer.validate_request(content)
        
        # New rule should be applied
        assert any('test_rule' in v.rule for v in result.violations)

    @pytest.mark.asyncio
    async def test_audit_logging(self, guardrails_layer):
        """Test audit logging of security validations."""
        content = "Test content for audit logging"
        
        with patch.object(guardrails_layer, '_log_validation') as mock_log:
            await guardrails_layer.validate_request(content)
            
            # Verify audit logging
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_performance_optimization(self, guardrails_layer):
        """Test performance optimization for repeated validations."""
        content = "Repeated validation test content"
        
        # First validation
        start_time = asyncio.get_event_loop().time()
        result1 = await guardrails_layer.validate_request(content)
        first_duration = asyncio.get_event_loop().time() - start_time
        
        # Second validation (should use cache)
        start_time = asyncio.get_event_loop().time()
        result2 = await guardrails_layer.validate_request(content)
        second_duration = asyncio.get_event_loop().time() - start_time
        
        # Results should be identical
        assert result1.is_safe == result2.is_safe
        assert result1.security_level == result2.security_level
        
        # Second validation should be faster (cached)
        assert second_duration <= first_duration

    def test_policy_configuration(self, guardrails_layer):
        """Test policy configuration management."""
        # Test policy retrieval
        policies = guardrails_layer.get_active_policies()
        assert isinstance(policies, dict)
        assert len(policies) > 0
        
        # Test policy update
        new_policy = {
            'name': 'test_policy',
            'rules': ['no_profanity', 'no_personal_info'],
            'enforcement_level': 'strict'
        }
        
        guardrails_layer.update_policy('content_safety', new_policy)
        
        updated_policies = guardrails_layer.get_active_policies()
        assert 'content_safety' in updated_policies

    def test_violation_severity_mapping(self, guardrails_layer):
        """Test violation severity mapping and handling."""
        violations = [
            PolicyViolation(rule='minor_issue', severity='low', message='Minor'),
            PolicyViolation(rule='major_issue', severity='high', message='Major'),
            PolicyViolation(rule='critical_issue', severity='critical', message='Critical')
        ]
        
        # Test severity-based security level determination
        security_level = guardrails_layer._determine_security_level(violations)
        
        # Should reflect highest severity
        assert security_level == 'dangerous'

    @pytest.mark.asyncio
    async def test_sanitization_quality(self, guardrails_layer):
        """Test quality of content sanitization."""
        content = "User john.doe@company.com has phone +1-555-0123 and lives at 123 Main St"
        
        result = await guardrails_layer.validate_request(content)
        
        # Verify comprehensive sanitization
        sanitized = result.redacted_content
        assert '@' not in sanitized or '[EMAIL]' in sanitized
        assert '+1-555' not in sanitized or '[PHONE]' in sanitized
        
        # Original structure should be preserved
        assert 'User' in sanitized
        assert 'has' in sanitized