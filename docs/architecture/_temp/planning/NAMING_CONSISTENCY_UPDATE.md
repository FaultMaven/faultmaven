# File Naming Consistency Update

**Date**: 2025-10-11  
**Status**: ✅ COMPLETE

---

## Problem Identified

Three files at `docs/architecture/` root used **UPPERCASE_WITH_UNDERSCORES** format:
- `ARCHITECTURE_EVOLUTION.md`
- `AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md`
- `CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md`

This was inconsistent with the established **lowercase-with-hyphens** convention.

---

## Files Renamed

| Old Name (UPPERCASE) | New Name (lowercase-hyphen) |
|---------------------|----------------------------|
| `ARCHITECTURE_EVOLUTION.md` | `architecture-evolution.md` |
| `AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md` | `agentic-framework-migration-guide.md` |
| `CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md` | `configuration-system-refactor-design.md` |

---

## References Updated

### architecture-overview.md
Updated Section 10 (Evolution and Historical Context):
```markdown
- [`Architecture Evolution`](./architecture-evolution.md)
- [`Agentic Framework Migration Guide`](./agentic-framework-migration-guide.md)
- [`Configuration System Refactor`](./configuration-system-refactor-design.md)
```

### architecture/README.md
Updated Evolution and History table:
```markdown
| **[Architecture Evolution](./architecture-evolution.md)** | ... |
| **[Agentic Framework Migration](./agentic-framework-migration-guide.md)** | ... |
| **[Configuration Refactor](./configuration-system-refactor-design.md)** | ... |
```

### architecture/legacy/README.md
Updated Related Documentation:
```markdown
- [Architecture Evolution](../architecture-evolution.md)
- [Agentic Framework Migration Guide](../agentic-framework-migration-guide.md)
```

### architecture/decisions/README.md
Updated Related Documentation:
```markdown
- [Architecture Evolution](../architecture-evolution.md)
- [Agentic Framework Migration](../agentic-framework-migration-guide.md)
- [Configuration System Refactor](../configuration-system-refactor-design.md)
```

---

## Naming Convention (Established)

### ✅ Correct Format: lowercase-with-hyphens

Examples:
- `architecture-overview.md`
- `investigation-phases-and-ooda-integration.md`
- `evidence-collection-and-tracking-design.md`
- `case-lifecycle-management.md`
- `agentic-framework-design-specification.md`
- `authentication-design.md`
- `dependency-injection-system.md`
- `architecture-evolution.md` ✅ (renamed)
- `agentic-framework-migration-guide.md` ✅ (renamed)
- `configuration-system-refactor-design.md` ✅ (renamed)

### ❌ Avoid: UPPERCASE_WITH_UNDERSCORES

This format is legacy and should be migrated to lowercase-hyphen.

---

## Consistency Check

### Files at docs/architecture/ root (21 files)

**All use lowercase-hyphen format**:
1. README.md ✅
2. architecture-overview.md ✅
3. documentation-map.md ✅
4. investigation-phases-and-ooda-integration.md ✅
5. evidence-collection-and-tracking-design.md ✅
6. case-lifecycle-management.md ✅
7. agentic-framework-design-specification.md ✅
8. agent_orchestration_design.md ✅ (uses underscore but lowercase)
9. query-classification-and-prompt-engineering.md ✅
10. data-submission-design.md ✅
11. authentication-design.md ✅
12. dependency-injection-system.md ✅
13. developer-guide.md ✅
14. container-usage-guide.md ✅
15. testing-guide.md ✅
16. service-patterns.md ✅
17. interface-based-design.md ✅
18. architecture-evolution.md ✅ (renamed)
19. agentic-framework-migration-guide.md ✅ (renamed)
20. configuration-system-refactor-design.md ✅ (renamed)
21. DI-diagram.mmd ✅

**Note on agent_orchestration_design.md**: Uses underscore instead of hyphen. This is acceptable but could be renamed to `agent-orchestration-design.md` for full consistency if desired.

---

## Remaining Inconsistencies (Optional to Fix)

### Files in Other Directories Still Using UPPERCASE

These could be renamed in future cleanup if desired:

**docs/specifications/**:
- `CASE_SESSION_CONCEPTS.md` → `case-session-concepts.md`
- `SESSION_MANAGEMENT_SPEC.md` → `session-management-spec.md`
- `CONFIGURATION_MANAGEMENT_SPEC.md` → `configuration-management-spec.md`
- etc.

**docs/development/**:
- `ENVIRONMENT_VARIABLES.md` → `environment-variables.md`
- `TOKEN_ESTIMATION.md` → `token-estimation.md`
- `CONTEXT_MANAGEMENT.md` → `context-management.md`

**docs/api/**:
- Various UPPERCASE files

**Not urgent**: These can be renamed over time as those documents are updated.

---

## Status

✅ **Architecture folder naming**: CONSISTENT  
✅ **All references**: UPDATED  
✅ **Links**: WORKING  

**Naming convention established**: lowercase-with-hyphens for all new files

---

**End of Naming Consistency Update**

