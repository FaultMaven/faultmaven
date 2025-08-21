"""Policy/Safety Service - Phase A Implementation

This module implements the IPolicySafetyService interface from the microservice
architecture blueprint, providing action classification, risk assessment, 
structured confirmations, and RBAC enforcement for system safety.

Key Features:
- Action classification (command_execution, data_modification, network_change, permission_change)
- Risk level assessment with RBAC mapping
- Structured confirmation requests with rationale, risks, and rollback procedures
- Policy decision audit trail
- Compliance checking and validation
- Multi-party approval workflows for critical actions

Implementation Notes:
- Comprehensive action pattern recognition
- Risk assessment based on action type, target, and context
- Structured confirmation payloads with monitoring guidance
- Thread-safe policy evaluation
- SLO compliance (p95 < 80ms, 99.9% availability)
"""

import asyncio
import logging
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from enum import Enum
import uuid
from threading import RLock

from faultmaven.services.microservice_interfaces.core_services import IPolicySafetyService
from faultmaven.models.microservice_contracts.core_contracts import (
    PolicyEvaluation, ActionType, RiskLevel
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException


class PolicySafetyService(IPolicySafetyService):
    """
    Implementation of IPolicySafetyService interface
    
    Provides comprehensive policy evaluation and safety enforcement with:
    - Action pattern recognition and classification
    - Risk assessment with context awareness
    - RBAC integration and permission checking
    - Structured confirmation workflows
    - Audit trail and compliance tracking
    """

    # Action classification patterns
    ACTION_PATTERNS = {
        ActionType.COMMAND_EXECUTION: [
            r'(run|execute|launch|start|invoke|call)\s+\w+',
            r'sudo\s+\w+',
            r'systemctl\s+(start|stop|restart|reload)',
            r'service\s+\w+\s+(start|stop|restart)',
            r'docker\s+(run|start|stop|restart|exec)',
            r'kubectl\s+\w+',
            r'bash|sh|python|perl|ruby',
            r'curl\s+-X\s+(POST|PUT|DELETE)',
            r'(install|upgrade|update)\s+package',
            r'crontab\s+-[el]',
        ],
        ActionType.DATA_MODIFICATION: [
            r'(delete|remove|rm)\s+\w+',
            r'(update|modify|change|edit)\s+\w+',
            r'(insert|add|create)\s+\w+',
            r'(backup|restore)\s+\w+',
            r'(copy|move|mv|cp)\s+\w+',
            r'(truncate|drop|alter)\s+(table|database)',
            r'UPDATE\s+\w+\s+SET',
            r'DELETE\s+FROM\s+\w+',
            r'INSERT\s+INTO\s+\w+',
            r'DROP\s+(TABLE|DATABASE|INDEX)',
        ],
        ActionType.NETWORK_CHANGE: [
            r'(firewall|iptables|ufw)\s+\w+',
            r'(route|ip\s+route)\s+\w+',
            r'(dns|resolv\.conf)\s+\w+',
            r'(port|socket)\s+(open|close|bind)',
            r'(proxy|gateway|nat)\s+\w+',
            r'(vlan|subnet|network)\s+\w+',
            r'(ssl|tls|certificate)\s+\w+',
            r'(load\s+balancer|lb)\s+\w+',
        ],
        ActionType.PERMISSION_CHANGE: [
            r'(chmod|chown|chgrp)\s+\w+',
            r'(user|group)\s+(add|delete|modify)',
            r'(grant|revoke)\s+\w+',
            r'(password|passwd)\s+\w+',
            r'(role|permission)\s+(assign|remove)',
            r'(access|acl)\s+(grant|deny)',
            r'(sudo|sudoers)\s+\w+',
            r'(key|certificate)\s+(generate|install|revoke)',
        ],
        ActionType.SERVICE_RESTART: [
            r'(restart|reload|stop|start)\s+(service|daemon)',
            r'systemctl\s+(restart|reload)',
            r'service\s+\w+\s+restart',
            r'(reboot|shutdown|halt)',
            r'(restart|reload)\s+(web|app|db)\s+server',
            r'(bounce|cycle)\s+\w+',
        ],
        ActionType.CONFIGURATION_CHANGE: [
            r'(config|configuration)\s+(change|update|modify)',
            r'(settings|parameters)\s+(adjust|set|modify)',
            r'(environment|env)\s+(variable|var)\s+\w+',
            r'(property|flag)\s+(enable|disable|set)',
            r'(registry|profile)\s+(update|modify)',
        ]
    }

    # Risk level mappings based on action type and context
    RISK_MATRIX = {
        ActionType.COMMAND_EXECUTION: {
            'default': RiskLevel.MEDIUM,
            'production': RiskLevel.HIGH,
            'privileged': RiskLevel.CRITICAL,
            'system': RiskLevel.HIGH
        },
        ActionType.DATA_MODIFICATION: {
            'default': RiskLevel.MEDIUM,
            'production': RiskLevel.CRITICAL,
            'backup': RiskLevel.LOW,
            'user_data': RiskLevel.HIGH
        },
        ActionType.NETWORK_CHANGE: {
            'default': RiskLevel.MEDIUM,
            'production': RiskLevel.CRITICAL,
            'firewall': RiskLevel.CRITICAL,
            'routing': RiskLevel.HIGH
        },
        ActionType.PERMISSION_CHANGE: {
            'default': RiskLevel.HIGH,
            'production': RiskLevel.CRITICAL,
            'admin': RiskLevel.CRITICAL,
            'user': RiskLevel.MEDIUM
        },
        ActionType.SERVICE_RESTART: {
            'default': RiskLevel.MEDIUM,
            'production': RiskLevel.HIGH,
            'critical_service': RiskLevel.CRITICAL,
            'database': RiskLevel.HIGH
        },
        ActionType.CONFIGURATION_CHANGE: {
            'default': RiskLevel.LOW,
            'production': RiskLevel.MEDIUM,
            'security': RiskLevel.HIGH,
            'system': RiskLevel.MEDIUM
        }
    }

    # Role requirements for risk levels
    ROLE_REQUIREMENTS = {
        RiskLevel.LOW: None,  # No special role required
        RiskLevel.MEDIUM: "user",
        RiskLevel.HIGH: "admin", 
        RiskLevel.CRITICAL: "senior_admin"
    }

    # Confirmation timeouts by risk level (minutes)
    CONFIRMATION_TIMEOUTS = {
        RiskLevel.LOW: 60,
        RiskLevel.MEDIUM: 30,
        RiskLevel.HIGH: 15,
        RiskLevel.CRITICAL: 10
    }

    def __init__(
        self,
        enforcement_mode: str = "strict",
        enable_compliance_checking: bool = True,
        auto_approve_low_risk: bool = False,
        audit_retention_days: int = 90
    ):
        """
        Initialize the Policy/Safety Service
        
        Args:
            enforcement_mode: Policy enforcement mode ('strict', 'warn', 'off')
            enable_compliance_checking: Whether to enable compliance validation
            auto_approve_low_risk: Whether to auto-approve low risk actions
            audit_retention_days: Days to retain audit records
        """
        self._logger = logging.getLogger(self.__class__.__name__)
        self._enforcement_mode = enforcement_mode.lower()
        self._enable_compliance = enable_compliance_checking
        self._auto_approve_low_risk = auto_approve_low_risk
        self._audit_retention = timedelta(days=audit_retention_days)
        
        # Thread safety for policy operations
        self._policy_lock = RLock()
        
        # Policy state and caches
        self._compiled_patterns = self._compile_action_patterns()
        self._policy_cache = {}  # Cache for repeated evaluations
        self._audit_log = []     # In-memory audit log (would be persistent in production)
        self._pending_confirmations = {}  # Active confirmation requests
        
        # Performance metrics
        self._metrics = {
            'evaluations_performed': 0,
            'policies_denied': 0,
            'confirmations_required': 0,
            'confirmations_approved': 0,
            'confirmations_denied': 0,
            'avg_evaluation_time_ms': 0.0,
            'compliance_violations': 0
        }
        
        # Initialize compliance rules if enabled
        if self._enable_compliance:
            self._compliance_rules = self._initialize_compliance_rules()
        else:
            self._compliance_rules = {}
        
        self._logger.info(f"✅ Policy service initialized with enforcement: {enforcement_mode}")

    def _compile_action_patterns(self) -> Dict[ActionType, List]:
        """Compile regex patterns for efficient matching"""
        compiled_patterns = {}
        for action_type, patterns in self.ACTION_PATTERNS.items():
            compiled_patterns[action_type] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]
        return compiled_patterns

    def _initialize_compliance_rules(self) -> Dict[str, Dict[str, Any]]:
        """Initialize compliance rules (simplified for demo)"""
        return {
            "data_protection": {
                "description": "Data protection and privacy compliance",
                "rules": [
                    "Personal data modifications require explicit consent",
                    "Data deletion must be logged and auditable",
                    "Backup verification required before data changes"
                ],
                "applicable_actions": [ActionType.DATA_MODIFICATION]
            },
            "security_controls": {
                "description": "Security and access control compliance",
                "rules": [
                    "Permission changes require multi-party approval",
                    "Administrative access changes must be time-limited",
                    "Security configuration changes require review"
                ],
                "applicable_actions": [ActionType.PERMISSION_CHANGE, ActionType.CONFIGURATION_CHANGE]
            },
            "operational_safety": {
                "description": "Operational safety and stability",
                "rules": [
                    "Production changes require maintenance window",
                    "Service restarts require monitoring preparation",
                    "Network changes require rollback plan"
                ],
                "applicable_actions": [ActionType.SERVICE_RESTART, ActionType.NETWORK_CHANGE]
            }
        }

    @trace("policy_service_evaluate_action")
    async def evaluate_action(
        self, 
        action: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> PolicyEvaluation:
        """
        Evaluate action against safety policies and generate risk assessment
        
        Args:
            action: Action details including type, target, parameters
            context: Execution context including user role, environment, urgency
            
        Returns:
            PolicyEvaluation with decision, risk level, required confirmations,
            and structured approval workflow
        """
        start_time = datetime.utcnow()
        
        try:
            # Validate inputs
            self._validate_evaluation_inputs(action, context)
            
            # Extract action information
            action_description = action.get('description', action.get('command', ''))
            action_target = action.get('target', {})
            action_params = action.get('parameters', {})
            
            # Classify action type
            action_type = await self._classify_action_type(action_description, action_params)
            
            # Assess risk level
            risk_level = await self._assess_risk_level(action_type, action_target, context)
            
            # Identify risk factors
            risk_factors = await self._identify_risk_factors(action, context, action_type)
            
            # Assess potential impacts
            potential_impacts = await self._assess_potential_impacts(action_type, action_target, context)
            
            # Make policy decision
            decision = await self._make_policy_decision(risk_level, context)
            
            # Determine if confirmation is required
            requires_confirmation = await self._requires_confirmation(risk_level, decision, context)
            
            # Get required role
            required_role = self._get_required_role(risk_level)
            
            # Generate confirmation payload if needed
            confirmation_payload = None
            if requires_confirmation:
                confirmation_payload = await self._create_confirmation_payload(
                    action, risk_level, risk_factors, potential_impacts, context
                )
            
            # Generate rationale
            rationale = self._generate_rationale(decision, risk_level, risk_factors, context)
            
            # Generate rollback procedure
            rollback_procedure = await self._generate_rollback_procedure(action_type, action_target, action_params)
            
            # Generate monitoring steps
            monitoring_steps = await self._generate_monitoring_steps(action_type, action_target, context)
            
            # Run compliance checks
            compliance_checks = {}
            if self._enable_compliance:
                compliance_checks = await self._run_compliance_checks(action_type, action, context)
            
            # Update metrics
            processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
            self._update_evaluation_metrics(processing_time, decision)
            
            # Create policy evaluation
            evaluation = PolicyEvaluation(
                action_type=action_type,
                action_description=action_description,
                risk_level=risk_level,
                risk_factors=risk_factors,
                potential_impacts=potential_impacts,
                decision=decision,
                requires_confirmation=requires_confirmation,
                required_role=required_role,
                confirmation_payload=confirmation_payload,
                rationale=rationale,
                rollback_procedure=rollback_procedure,
                monitoring_steps=monitoring_steps,
                compliance_checks=compliance_checks
            )
            
            # Log evaluation for audit
            await self._log_policy_evaluation(evaluation, context)
            
            self._logger.debug(f"Policy evaluation: {decision} (risk: {risk_level.value}, type: {action_type.value})")
            return evaluation
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Policy evaluation failed: {e}")
            raise ServiceException(f"Policy evaluation failed: {str(e)}") from e

    async def _classify_action_type(self, description: str, parameters: Dict[str, Any]) -> ActionType:
        """Classify action type based on description and parameters"""
        if not description:
            return ActionType.CONFIGURATION_CHANGE  # Default fallback
        
        # Check each action type pattern
        for action_type, compiled_patterns in self._compiled_patterns.items():
            for pattern in compiled_patterns:
                if pattern.search(description):
                    return action_type
        
        # Check parameters for additional classification clues
        if parameters:
            param_text = ' '.join(str(v) for v in parameters.values())
            for action_type, compiled_patterns in self._compiled_patterns.items():
                for pattern in compiled_patterns:
                    if pattern.search(param_text):
                        return action_type
        
        # Default to configuration change if no specific pattern matches
        return ActionType.CONFIGURATION_CHANGE

    async def _assess_risk_level(
        self, 
        action_type: ActionType, 
        target: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> RiskLevel:
        """Assess risk level based on action type, target, and context"""
        
        # Get base risk for action type
        risk_mapping = self.RISK_MATRIX.get(action_type, {})
        default_risk = risk_mapping.get('default', RiskLevel.LOW)
        
        # Check for context-specific risk escalation
        environment = context.get('environment', '').lower()
        user_role = context.get('user_role', '').lower()
        urgency = context.get('urgency', 'normal').lower()
        
        # Production environment increases risk
        if environment in ['production', 'prod', 'live']:
            production_risk = risk_mapping.get('production', default_risk)
            if self._risk_level_value(production_risk) > self._risk_level_value(default_risk):
                default_risk = production_risk
        
        # Privileged operations increase risk
        if 'admin' in user_role or 'root' in user_role or 'sudo' in str(target):
            privileged_risk = risk_mapping.get('privileged', default_risk)
            if self._risk_level_value(privileged_risk) > self._risk_level_value(default_risk):
                default_risk = privileged_risk
        
        # Check target-specific risk factors
        target_service = target.get('service', '').lower()
        target_type = target.get('type', '').lower()
        
        # Critical services
        if target_service in ['database', 'db', 'mysql', 'postgresql', 'mongodb']:
            db_risk = risk_mapping.get('database', default_risk)
            if self._risk_level_value(db_risk) > self._risk_level_value(default_risk):
                default_risk = db_risk
        
        # System-level targets
        if target_type in ['system', 'kernel', 'os'] or target_service in ['systemd', 'init']:
            system_risk = risk_mapping.get('system', default_risk)
            if self._risk_level_value(system_risk) > self._risk_level_value(default_risk):
                default_risk = system_risk
        
        # Escalate for urgent requests (might indicate incident response)
        if urgency == 'critical' and self._risk_level_value(default_risk) < self._risk_level_value(RiskLevel.HIGH):
            default_risk = RiskLevel.HIGH
        
        return default_risk

    def _risk_level_value(self, risk_level: RiskLevel) -> int:
        """Get numeric value for risk level comparison"""
        risk_values = {
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4
        }
        return risk_values.get(risk_level, 1)

    async def _identify_risk_factors(
        self, 
        action: Dict[str, Any], 
        context: Dict[str, Any], 
        action_type: ActionType
    ) -> List[str]:
        """Identify specific risk factors for the action"""
        risk_factors = []
        
        # Environment-based risks
        environment = context.get('environment', '').lower()
        if environment in ['production', 'prod', 'live']:
            risk_factors.append("Production environment")
        
        # User/role-based risks
        user_role = context.get('user_role', '').lower()
        if 'temp' in user_role or 'guest' in user_role:
            risk_factors.append("Temporary or guest user")
        
        # Time-based risks
        current_hour = datetime.utcnow().hour
        if current_hour < 6 or current_hour > 22:  # Off-hours
            risk_factors.append("Off-hours execution")
        
        # Action-specific risks
        if action_type == ActionType.DATA_MODIFICATION:
            if 'delete' in action.get('description', '').lower():
                risk_factors.append("Data deletion operation")
            if 'production' in str(action.get('target', {})).lower():
                risk_factors.append("Production data modification")
        
        elif action_type == ActionType.PERMISSION_CHANGE:
            risk_factors.append("Access control modification")
            if 'admin' in str(action).lower():
                risk_factors.append("Administrative privilege change")
        
        elif action_type == ActionType.NETWORK_CHANGE:
            if 'firewall' in str(action).lower():
                risk_factors.append("Firewall configuration change")
            risk_factors.append("Network infrastructure modification")
        
        elif action_type == ActionType.SERVICE_RESTART:
            target_service = action.get('target', {}).get('service', '')
            if target_service.lower() in ['database', 'web', 'api', 'auth']:
                risk_factors.append("Critical service restart")
        
        # Urgency-based risks
        urgency = context.get('urgency', 'normal').lower()
        if urgency in ['urgent', 'critical', 'emergency']:
            risk_factors.append("High urgency request")
        
        # Compliance-based risks
        if self._enable_compliance:
            for rule_name, rule_config in self._compliance_rules.items():
                if action_type in rule_config.get('applicable_actions', []):
                    risk_factors.append(f"Subject to {rule_name} compliance")
        
        return risk_factors

    async def _assess_potential_impacts(
        self, 
        action_type: ActionType, 
        target: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> List[str]:
        """Assess potential negative impacts of the action"""
        impacts = []
        
        # Generic impacts by action type
        impact_mapping = {
            ActionType.COMMAND_EXECUTION: [
                "System state changes",
                "Resource consumption",
                "Process creation/termination"
            ],
            ActionType.DATA_MODIFICATION: [
                "Data loss or corruption",
                "Data consistency issues",
                "Backup validity concerns"
            ],
            ActionType.NETWORK_CHANGE: [
                "Service connectivity disruption",
                "Security exposure",
                "Traffic routing issues"
            ],
            ActionType.PERMISSION_CHANGE: [
                "Security vulnerabilities",
                "Access control bypass",
                "Privilege escalation"
            ],
            ActionType.SERVICE_RESTART: [
                "Service downtime",
                "Connection drops",
                "Data loss during restart"
            ],
            ActionType.CONFIGURATION_CHANGE: [
                "Service behavior changes",
                "Performance impact",
                "Configuration drift"
            ]
        }
        
        impacts.extend(impact_mapping.get(action_type, ["Unknown impact"]))
        
        # Environment-specific impacts
        environment = context.get('environment', '').lower()
        if environment in ['production', 'prod', 'live']:
            impacts.extend([
                "Customer service disruption",
                "Revenue impact",
                "SLA violation"
            ])
        
        # Target-specific impacts
        target_service = target.get('service', '').lower()
        if target_service in ['database', 'db']:
            impacts.extend([
                "Data availability issues",
                "Transaction rollbacks",
                "Replication lag"
            ])
        elif target_service in ['auth', 'authentication']:
            impacts.extend([
                "Authentication service disruption",
                "User login failures",
                "Session invalidation"
            ])
        elif target_service in ['api', 'web']:
            impacts.extend([
                "API service disruption", 
                "Client application failures",
                "Integration breakage"
            ])
        
        return impacts

    async def _make_policy_decision(self, risk_level: RiskLevel, context: Dict[str, Any]) -> str:
        """Make the core policy decision"""
        if self._enforcement_mode == "off":
            return "allow"
        
        # Auto-approve low risk if configured
        if self._auto_approve_low_risk and risk_level == RiskLevel.LOW:
            return "allow"
        
        # Check user role permissions
        user_role = context.get('user_role', '').lower()
        required_role = self._get_required_role(risk_level)
        
        if required_role and not self._user_has_role(user_role, required_role):
            return "deny"
        
        # Risk-based decisions
        if risk_level == RiskLevel.LOW:
            return "allow"
        elif risk_level in [RiskLevel.MEDIUM, RiskLevel.HIGH]:
            return "confirm"  # Requires confirmation
        elif risk_level == RiskLevel.CRITICAL:
            if self._enforcement_mode == "strict":
                return "confirm"  # Still allow with confirmation in strict mode
            else:
                return "deny"
        
        return "deny"  # Default deny

    def _user_has_role(self, user_role: str, required_role: str) -> bool:
        """Check if user has required role (simplified RBAC)"""
        role_hierarchy = {
            'guest': 0,
            'user': 1,
            'admin': 2, 
            'senior_admin': 3,
            'super_admin': 4
        }
        
        user_level = role_hierarchy.get(user_role, 0)
        required_level = role_hierarchy.get(required_role, 0)
        
        return user_level >= required_level

    async def _requires_confirmation(
        self, 
        risk_level: RiskLevel, 
        decision: str, 
        context: Dict[str, Any]
    ) -> bool:
        """Determine if confirmation is required"""
        if decision == "deny":
            return False  # No confirmation for denied actions
        
        if decision == "confirm":
            return True
        
        # Additional confirmation requirements
        if risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
            return True
        
        # Context-based confirmation requirements
        environment = context.get('environment', '').lower()
        if environment in ['production', 'prod', 'live'] and risk_level == RiskLevel.MEDIUM:
            return True
        
        return False

    def _get_required_role(self, risk_level: RiskLevel) -> Optional[str]:
        """Get required role for risk level"""
        return self.ROLE_REQUIREMENTS.get(risk_level)

    async def _create_confirmation_payload(
        self, 
        action: Dict[str, Any], 
        risk_level: RiskLevel, 
        risk_factors: List[str], 
        potential_impacts: List[str], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create structured confirmation request payload"""
        confirmation_id = str(uuid.uuid4())
        timeout_minutes = self.CONFIRMATION_TIMEOUTS.get(risk_level, 15)
        
        payload = {
            "confirmation_id": confirmation_id,
            "action_summary": action.get('description', 'Unknown action'),
            "risk_level": risk_level.value,
            "risk_factors": risk_factors,
            "potential_impacts": potential_impacts,
            "required_role": self._get_required_role(risk_level),
            "timeout_minutes": timeout_minutes,
            "expires_at": (datetime.utcnow() + timedelta(minutes=timeout_minutes)).isoformat(),
            "approval_requirements": self._get_approval_requirements(risk_level),
            "context": {
                "environment": context.get('environment', 'unknown'),
                "user": context.get('user_id', 'unknown'),
                "urgency": context.get('urgency', 'normal'),
                "requested_at": datetime.utcnow().isoformat()
            }
        }
        
        # Store confirmation request
        with self._policy_lock:
            self._pending_confirmations[confirmation_id] = {
                "payload": payload,
                "action": action,
                "context": context,
                "created_at": datetime.utcnow(),
                "status": "pending"
            }
        
        return payload

    def _get_approval_requirements(self, risk_level: RiskLevel) -> Dict[str, Any]:
        """Get approval requirements for risk level"""
        requirements = {
            RiskLevel.LOW: {"approvers": 0, "multi_party": False},
            RiskLevel.MEDIUM: {"approvers": 1, "multi_party": False},
            RiskLevel.HIGH: {"approvers": 1, "multi_party": True},
            RiskLevel.CRITICAL: {"approvers": 2, "multi_party": True}
        }
        
        return requirements.get(risk_level, {"approvers": 1, "multi_party": False})

    def _generate_rationale(
        self, 
        decision: str, 
        risk_level: RiskLevel, 
        risk_factors: List[str], 
        context: Dict[str, Any]
    ) -> str:
        """Generate human-readable rationale for the policy decision"""
        
        if decision == "allow":
            if risk_level == RiskLevel.LOW:
                return f"Action approved: Low risk operation with minimal potential impact."
            else:
                return f"Action approved: Risk factors acceptable for current context."
        
        elif decision == "deny":
            reasons = []
            if risk_level == RiskLevel.CRITICAL:
                reasons.append("Critical risk level exceeds policy threshold")
            
            user_role = context.get('user_role', '')
            required_role = self._get_required_role(risk_level)
            if required_role and not self._user_has_role(user_role, required_role):
                reasons.append(f"User role '{user_role}' insufficient (requires '{required_role}')")
            
            if self._enforcement_mode == "strict":
                reasons.append("Strict enforcement mode active")
            
            rationale = f"Action denied: {'; '.join(reasons)}."
            if risk_factors:
                rationale += f" Risk factors: {', '.join(risk_factors[:3])}."
            
            return rationale
        
        elif decision == "confirm":
            rationale = f"Action requires confirmation due to {risk_level.value} risk level."
            if risk_factors:
                rationale += f" Key concerns: {', '.join(risk_factors[:2])}."
            return rationale
        
        return f"Policy decision: {decision}"

    async def _generate_rollback_procedure(
        self, 
        action_type: ActionType, 
        target: Dict[str, Any], 
        parameters: Dict[str, Any]
    ) -> Optional[str]:
        """Generate rollback procedure for the action"""
        
        rollback_templates = {
            ActionType.COMMAND_EXECUTION: [
                "1. Stop the executed process using appropriate signals",
                "2. Revert any system state changes made by the command",
                "3. Restore previous configuration if modified",
                "4. Verify system functionality after rollback"
            ],
            ActionType.DATA_MODIFICATION: [
                "1. Restore data from most recent backup",
                "2. Verify data integrity after restoration",
                "3. Update dependent systems with restored data",
                "4. Test application functionality with restored data"
            ],
            ActionType.NETWORK_CHANGE: [
                "1. Revert network configuration to previous state",
                "2. Restart networking services if necessary",
                "3. Verify connectivity to all dependent services",
                "4. Update monitoring and alerting rules"
            ],
            ActionType.PERMISSION_CHANGE: [
                "1. Restore previous permissions from backup",
                "2. Verify access control is working correctly",
                "3. Invalidate any sessions using changed permissions",
                "4. Audit log review for security implications"
            ],
            ActionType.SERVICE_RESTART: [
                "1. Monitor service startup and health checks",
                "2. If issues arise, stop service and investigate",
                "3. Restore previous service version if needed",
                "4. Verify dependent services are functioning"
            ],
            ActionType.CONFIGURATION_CHANGE: [
                "1. Restore previous configuration from backup",
                "2. Restart affected services with old configuration",
                "3. Verify system behavior matches expectations",
                "4. Document rollback actions for future reference"
            ]
        }
        
        template_steps = rollback_templates.get(action_type, [
            "1. Identify changes made by the action",
            "2. Develop plan to reverse each change",
            "3. Execute rollback plan systematically", 
            "4. Verify system returns to previous state"
        ])
        
        # Customize based on target and parameters
        target_service = target.get('service', '')
        if target_service:
            template_steps.append(f"5. Monitor {target_service} service health post-rollback")
        
        return "\n".join(template_steps)

    async def _generate_monitoring_steps(
        self, 
        action_type: ActionType, 
        target: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Optional[List[str]]:
        """Generate monitoring steps to verify action success"""
        
        monitoring_templates = {
            ActionType.COMMAND_EXECUTION: [
                "Monitor process execution logs",
                "Check system resource utilization",
                "Verify expected output/results",
                "Monitor for error messages or exceptions"
            ],
            ActionType.DATA_MODIFICATION: [
                "Verify data integrity with checksums",
                "Monitor database performance metrics",
                "Check application functionality",
                "Validate data consistency across systems"
            ],
            ActionType.NETWORK_CHANGE: [
                "Test connectivity to affected services",
                "Monitor network traffic patterns",
                "Check firewall logs for blocked connections",
                "Verify DNS resolution is working"
            ],
            ActionType.PERMISSION_CHANGE: [
                "Test access with affected user accounts",
                "Monitor authentication logs",
                "Verify role-based access is working",
                "Check for unauthorized access attempts"
            ],
            ActionType.SERVICE_RESTART: [
                "Monitor service health checks",
                "Check service logs for errors",
                "Verify dependent services are connecting",
                "Monitor response times and performance"
            ],
            ActionType.CONFIGURATION_CHANGE: [
                "Verify configuration is applied correctly",
                "Monitor application behavior changes",
                "Check for configuration-related errors",
                "Test affected functionality end-to-end"
            ]
        }
        
        steps = monitoring_templates.get(action_type, [
            "Monitor system logs for related messages",
            "Check service health and availability",
            "Verify expected functionality is working"
        ])
        
        # Add environment-specific monitoring
        environment = context.get('environment', '').lower()
        if environment in ['production', 'prod', 'live']:
            steps.extend([
                "Monitor customer-facing metrics",
                "Check SLA compliance dashboards",
                "Verify no alerts are triggered"
            ])
        
        return steps

    async def _run_compliance_checks(
        self, 
        action_type: ActionType, 
        action: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Dict[str, bool]:
        """Run compliance validation checks"""
        checks = {}
        
        if not self._enable_compliance:
            return checks
        
        for rule_name, rule_config in self._compliance_rules.items():
            if action_type in rule_config.get('applicable_actions', []):
                # Simplified compliance check (would be more sophisticated in production)
                passed = True
                
                # Example compliance logic
                if rule_name == "data_protection" and action_type == ActionType.DATA_MODIFICATION:
                    # Check for user consent, backup verification, etc.
                    if not context.get('user_consent', False):
                        passed = False
                    if not context.get('backup_verified', False):
                        passed = False
                
                elif rule_name == "security_controls" and action_type == ActionType.PERMISSION_CHANGE:
                    # Check for multi-party approval, time limits, etc.
                    if context.get('urgency') == 'critical':
                        passed = True  # Emergency override
                    elif not context.get('multi_party_approved', False):
                        passed = False
                
                checks[rule_name] = passed
                
                if not passed:
                    self._metrics['compliance_violations'] += 1
        
        return checks

    def _update_evaluation_metrics(self, processing_time_ms: float, decision: str):
        """Update performance and decision metrics"""
        self._metrics['evaluations_performed'] += 1
        
        # Update average processing time
        count = self._metrics['evaluations_performed']
        current_avg = self._metrics['avg_evaluation_time_ms']
        self._metrics['avg_evaluation_time_ms'] = (current_avg * (count - 1) + processing_time_ms) / count
        
        # Update decision counts
        if decision == "deny":
            self._metrics['policies_denied'] += 1
        elif decision == "confirm":
            self._metrics['confirmations_required'] += 1

    async def _log_policy_evaluation(self, evaluation: PolicyEvaluation, context: Dict[str, Any]):
        """Log policy evaluation for audit trail"""
        audit_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "action_id": evaluation.action_id,
            "action_type": evaluation.action_type.value,
            "risk_level": evaluation.risk_level.value,
            "decision": evaluation.decision,
            "user": context.get('user_id', 'unknown'),
            "environment": context.get('environment', 'unknown'),
            "requires_confirmation": evaluation.requires_confirmation,
            "compliance_checks": evaluation.compliance_checks
        }
        
        with self._policy_lock:
            self._audit_log.append(audit_entry)
            
            # Cleanup old audit entries
            cutoff_time = datetime.utcnow() - self._audit_retention
            self._audit_log = [
                entry for entry in self._audit_log 
                if datetime.fromisoformat(entry['timestamp']) > cutoff_time
            ]

    def _validate_evaluation_inputs(self, action: Dict[str, Any], context: Dict[str, Any]):
        """Validate policy evaluation inputs"""
        if not action:
            raise ValidationException("Action cannot be empty")
        
        if not isinstance(action, dict):
            raise ValidationException("Action must be a dictionary")
        
        if not context:
            raise ValidationException("Context cannot be empty")
        
        # Check for required action fields
        if not action.get('description') and not action.get('command'):
            raise ValidationException("Action must have description or command")

    @trace("policy_service_create_confirmation")
    async def create_confirmation(
        self, 
        action: Dict[str, Any], 
        risk_assessment: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create structured confirmation request for risky actions"""
        try:
            risk_level = RiskLevel(risk_assessment.get('risk_level', 'medium'))
            risk_factors = risk_assessment.get('risk_factors', [])
            potential_impacts = risk_assessment.get('potential_impacts', [])
            
            # Create confirmation payload
            confirmation_payload = await self._create_confirmation_payload(
                action, risk_level, risk_factors, potential_impacts, {}
            )
            
            self._logger.info(f"Created confirmation request {confirmation_payload['confirmation_id']}")
            return confirmation_payload
            
        except Exception as e:
            self._logger.error(f"Failed to create confirmation: {e}")
            raise ServiceException(f"Confirmation creation failed: {str(e)}") from e

    @trace("policy_service_record_decision")
    async def record_decision(
        self, 
        action_id: str, 
        approved: bool, 
        approver: str, 
        context: Dict[str, Any]
    ) -> bool:
        """Record policy decision for audit trail"""
        try:
            if not action_id or not action_id.strip():
                raise ValidationException("Action ID cannot be empty")
            
            if not approver or not approver.strip():
                raise ValidationException("Approver cannot be empty")
            
            # Find pending confirmation
            confirmation = None
            with self._policy_lock:
                if action_id in self._pending_confirmations:
                    confirmation = self._pending_confirmations[action_id]
                    confirmation['status'] = 'approved' if approved else 'denied'
                    confirmation['approver'] = approver
                    confirmation['decision_time'] = datetime.utcnow()
                    confirmation['decision_context'] = context
            
            if not confirmation:
                self._logger.warning(f"No pending confirmation found for action {action_id}")
                return False
            
            # Update metrics
            if approved:
                self._metrics['confirmations_approved'] += 1
            else:
                self._metrics['confirmations_denied'] += 1
            
            # Create audit entry
            audit_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action_id": action_id,
                "decision": "approved" if approved else "denied",
                "approver": approver,
                "context": context,
                "original_request": confirmation.get('payload', {})
            }
            
            with self._policy_lock:
                self._audit_log.append(audit_entry)
            
            self._logger.info(f"Recorded decision for action {action_id}: {audit_entry['decision']}")
            return True
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Failed to record decision: {e}")
            return False

    async def health_check(self) -> Dict[str, Any]:
        """Get service health status"""
        try:
            # Calculate metrics
            total_evaluations = self._metrics['evaluations_performed']
            denial_rate = (self._metrics['policies_denied'] / max(total_evaluations, 1)) * 100
            avg_latency = self._metrics['avg_evaluation_time_ms']
            
            # Determine status
            status = "healthy"
            errors = []
            
            if avg_latency > 80:  # SLO: p95 < 80ms
                status = "degraded"
                errors.append(f"High average latency: {avg_latency:.2f}ms")
            
            if denial_rate > 50:  # High denial rate might indicate misconfiguration
                status = "degraded"
                errors.append(f"High policy denial rate: {denial_rate:.1f}%")
            
            pending_confirmations = len(self._pending_confirmations)
            if pending_confirmations > 100:  # Large backlog of confirmations
                status = "degraded"
                errors.append(f"Large confirmation backlog: {pending_confirmations}")
            
            health_info = {
                "service": "policy_service",
                "status": status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "enforcement_mode": self._enforcement_mode,
                "compliance_enabled": self._enable_compliance,
                "metrics": {
                    "total_evaluations": total_evaluations,
                    "policies_denied": self._metrics['policies_denied'],
                    "confirmations_required": self._metrics['confirmations_required'],
                    "confirmations_approved": self._metrics['confirmations_approved'],
                    "confirmations_denied": self._metrics['confirmations_denied'],
                    "avg_latency_ms": avg_latency,
                    "denial_rate_percent": denial_rate,
                    "compliance_violations": self._metrics['compliance_violations']
                },
                "state": {
                    "pending_confirmations": pending_confirmations,
                    "audit_log_entries": len(self._audit_log),
                    "compiled_patterns": len(self._compiled_patterns)
                },
                "errors": errors
            }
            
            return health_info
            
        except Exception as e:
            return {
                "service": "policy_service",
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

    async def ready_check(self) -> bool:
        """Check if service is ready to handle requests"""
        try:
            # Check if essential components are initialized
            if not self._compiled_patterns:
                return False
            
            if self._enforcement_mode not in ["strict", "warn", "off"]:
                return False
            
            return True
            
        except Exception:
            return False

    # Additional utility methods for monitoring and administration

    async def get_pending_confirmations(self) -> List[Dict[str, Any]]:
        """Get list of pending confirmation requests"""
        with self._policy_lock:
            pending = []
            for conf_id, conf_data in self._pending_confirmations.items():
                if conf_data['status'] == 'pending':
                    # Check if expired
                    timeout_minutes = conf_data['payload'].get('timeout_minutes', 15)
                    if datetime.utcnow() - conf_data['created_at'] > timedelta(minutes=timeout_minutes):
                        conf_data['status'] = 'expired'
                    else:
                        pending.append({
                            "confirmation_id": conf_id,
                            "created_at": conf_data['created_at'].isoformat(),
                            "payload": conf_data['payload']
                        })
            return pending

    async def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent audit log entries"""
        with self._policy_lock:
            return self._audit_log[-limit:] if self._audit_log else []

    async def get_policy_statistics(self) -> Dict[str, Any]:
        """Get comprehensive policy statistics"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "enforcement_mode": self._enforcement_mode,
            "metrics": self._metrics.copy(),
            "configuration": {
                "compliance_enabled": self._enable_compliance,
                "auto_approve_low_risk": self._auto_approve_low_risk,
                "audit_retention_days": self._audit_retention.days
            },
            "action_types": [action_type.value for action_type in ActionType],
            "risk_levels": [risk_level.value for risk_level in RiskLevel],
            "compliance_rules": list(self._compliance_rules.keys()) if self._enable_compliance else []
        }