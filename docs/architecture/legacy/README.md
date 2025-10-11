# Legacy Architecture Documents

Historical architecture documents that have been superseded by newer designs. These are preserved for reference and to understand the evolution of the system.

## Superseded Architectures

### Doctor-Patient Prompting Architecture v1.0

**[Doctor-Patient Prompting v1.0](./DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md)**

- **Original Design**: Evidence-based prompting with 6-phase investigation model
- **Superseded By**: [Investigation Phases and OODA Integration Framework v2.1](../investigation-phases-and-ooda-integration.md)
- **Status**: Code artifacts still exist in `faultmaven/services/agentic/doctor_patient/sub_agents/`
- **Value**: Historical context for current phase-based investigation approach

**Key Changes**:
- 6 phases (1-6) → 7 phases (0-6, zero-indexed)
- Single-loop → Dual-loop (Investigation Phases + OODA Steps)
- Added Engagement Modes (Consultant vs Lead Investigator)

---

### Sub-Agent Architecture v1.0

**[Sub-Agent Architecture v1.0](./SUB_AGENT_ARCHITECTURE.md)**

- **Original Design**: Multi-agent coordination with specialized sub-agents
- **Superseded By**: [Agentic Framework Design Specification](../agentic-framework-design-specification.md)
- **Status**: Patterns incorporated into 7-component agentic framework
- **Value**: Reference for agent coordination patterns and multi-agent workflows

**Key Changes**:
- Multiple specialized agents → Unified workflow engine with tool broker
- Fixed agent roles → Dynamic capability discovery
- Agent-to-agent communication → Centralized state manager

---

### System Architecture v1.0

**[System Architecture v1.0](./SYSTEM_ARCHITECTURE.md)**

- **Original Design**: Initial system architecture document
- **Superseded By**: [Architecture Overview v2.0](../architecture-overview.md)
- **Status**: Replaced by more detailed, code-aligned architecture
- **Value**: Historical context for architecture evolution

**Key Changes**:
- Functional grouping → Code-aligned organization (10 sections)
- Missing details → Comprehensive coverage (40+ linked documents)
- Generic descriptions → Specific module mappings

---

## Why Preserve Legacy Docs?

1. **Institutional Knowledge**: Understanding "why we changed"
2. **Design Rationale**: Context for current architecture decisions
3. **Migration Reference**: Helps with understanding the transition
4. **Pattern Library**: Valuable patterns that may be reused

---

## Related Documentation

- [Architecture Evolution](../architecture-evolution.md) - Complete evolution history
- [Agentic Framework Migration Guide](../agentic-framework-migration-guide.md) - Migration from legacy to current framework
- [Architecture Overview v2.0](../architecture-overview.md) - Current architecture

---

**Last Updated**: 2025-10-11  
**Purpose**: Historical reference and institutional knowledge preservation

