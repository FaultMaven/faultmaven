---
name: architecture
description: Triggers when adding a new endpoint, creating a service, refactoring module code, moving code between modules, adding a new module, or making schema changes that add or move tables. Do NOT trigger on documentation-only tasks, configuration edits, or migrations that only adjust columns/indexes on an existing table.
---

# Skill: architecture

**What this skill does:** Makes sure you read the authoritative architecture design docs *before* making structural changes, so code conforms to FaultMaven's Vertical Module vs Domain Service conventions.

**What this skill does NOT do:** Restate the architecture. The design docs are the source of truth and change over time — a stale copy here would cause the very inconsistency this skill is meant to prevent.

---

## Authoritative Documents

Read these before acting. The first is one of the four canonical source-of-truth documents declared in `docs/architecture/README.md`:

1. **`docs/architecture/core-architecture/architectural-design-principles.md`** — Canonical. The 12 design principles (deployment-agnostic, vertical modules, composition root, interfaces, observability).
2. **`docs/architecture/core-architecture/README.md`** — Index and reading order for the core-architecture section.
3. **`docs/architecture/core-architecture/module-organization-design.md`** — Module boundaries and how modules compose.
4. **`docs/architecture/core-architecture/vertical-vs-layer-structuring-explained.md`** — The "why" behind Vertical Modules vs Domain Services.
5. **`docs/architecture/core-architecture/infrastructure-layer-guide.md`** — What belongs in `faultmaven/infrastructure/` vs inside a module.
6. **`CLAUDE.md` §Architecture** — Module inventory, cross-module import rules, module-type table. Read for facts; this skill does not restate them.

If any referenced document does not exist at the path above, **stop and tell the user** — do not fabricate content to fill the gap.

---

## Procedure

1. **Read `docs/architecture/README.md`** to confirm canonical documents and navigation.
2. **Read `docs/architecture/core-architecture/README.md`** for reading order within the core-architecture section.
3. **Read the design docs relevant to the change** (principles doc is mandatory; others per the section README's guidance).
4. **Read `CLAUDE.md` §Architecture** for module inventory and import rules.
5. **Read the target module's existing structure** — directory layout, `contracts.py` (if present), existing `api/routes.py` and `domain/services/` — so the change matches this specific module's conventions, not a generic template.
6. **Identify module type** by consulting `.claude/manifest.json` (`modules.<name>.type`). `vertical` modules have `contracts.py` + `infrastructure/`; `domain_service` modules have `api/` + `domain/` only and import shared models from Case or Auth contracts.
7. **Apply changes** conforming to the documented conventions for that module type.
8. **Verify** by running `lint-imports` — `.importlinter` enforces module boundaries mechanically. A passing `lint-imports` is necessary but not sufficient; this skill enforces the non-mechanical conventions.

If the design docs and the existing code appear to contradict each other, **stop and ask the user which side is authoritative** before proceeding. Do not silently pick one side. Drift in either direction is what `/design-check` exists to report.

---

## Scope Boundaries

**This skill governs:**
- Code structure and module organization
- Where a new endpoint, service, repository, or shared adapter belongs
- Whether a new module should be a Vertical Module or a Domain Service
- Cross-module import compliance

**This skill does NOT govern:**
- Investigation orchestration / agent behavior / prompts — see `investigation-framework`
- Retrieval, search, reranking, KB query construction — see `rag-architecture`
- Ingestion, classification, chunking of evidence — see `ingestion-pipeline`
- Product positioning and messaging — see `brand-messaging`
- Test authoring — see the `test-engineer` agent
- Things already enforced by tooling (`ruff`, `black`, `isort`, `mypy`, `import-linter`) — let the tool catch them
