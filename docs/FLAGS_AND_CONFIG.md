# Flags and Configuration (Modular Monolith)

## Feature flags (recommended defaults)
- gateway: true
- router: true
- confidence_scoring: true
- loop_guard: true
- clarifier_skill: true
- retrieval_skill: true
- diagnoser_skill: true
- validator_skill: true
- policy_enforcement: strict

## Extraction/microservice flags (keep disabled)
- microservice_routes_enabled: false
- kafka_enabled: false
- external_event_bus: false

## Env var equivalents (example)
- FAULTMAVEN_GATEWAY=1
- FAULTMAVEN_ROUTER=1
- FAULTMAVEN_CONFIDENCE=1
- FAULTMAVEN_LOOP_GUARD=1
- FAULTMAVEN_SKILL_CLARIFIER=1
- FAULTMAVEN_SKILL_RETRIEVAL=1
- FAULTMAVEN_SKILL_DIAGNOSER=1
- FAULTMAVEN_SKILL_VALIDATOR=1
- FAULTMAVEN_POLICY_ENFORCEMENT=strict
- FAULTMAVEN_MICROSERVICES_ENABLED=0
- FAULTMAVEN_KAFKA_ENABLED=0
- FAULTMAVEN_EVENTBUS=memory
- FAULTMAVEN_DEBUG=0

## Notes
- Flags can be centralized in a simple config reader that checks env first, then file defaults.
- Any experimental feature should default to off unless covered by tests and SLOs.


