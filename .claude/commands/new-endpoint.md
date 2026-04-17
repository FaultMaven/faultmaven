---
description: Scaffold a new API endpoint in the given module, applying the correct Vertical Module or Domain Service pattern.
---

# /new-endpoint

Scaffold a new API endpoint in a FaultMaven module, applying the correct Vertical Module or Domain Service pattern. Produces a working stub. Business logic is left to the user.

## Argument

`$ARGUMENTS` — the module name. Valid modules: `agent`, `auth`, `case`, `evidence`, `knowledge`, `preprocessing`, `report`.

If the argument is missing or not one of the valid modules, reject the invocation with a helpful error listing the valid set. Do not guess or improvise.

## Procedure

### 1. Load the architecture skill

Read `.claude/skills/architecture.md` and follow its procedure. Do not rely on auto-loading.

### 2. Look up the module in the manifest

Read `.claude/manifest.json`. Find `modules.<name>`:
- `type` — `vertical` or `domain_service`
- `path` — module root directory

If the module is not in the manifest, stop and tell the user — do not scaffold into a module that is not declared.

### 3. Read the existing module structure

Before creating anything, read what is already in the module:
- The existing `api/routes.py` (imports, router prefix, existing endpoints — match their style)
- The existing `domain/services/` (service class naming, method signatures, async/sync conventions)
- For `vertical` modules: the existing `contracts.py` (DTO style, interface naming)
- The tests directory for this module (`tests/unit/modules/<name>/` or similar) to match test structure

The scaffold must match the patterns actually used in *this* module, not a generic template.

### 4. Prompt the user for endpoint details

Ask for:
- **HTTP method** (GET / POST / PUT / PATCH / DELETE)
- **Path** (e.g., `/bulk-update`, appended to the module's existing router prefix)
- **Short description** of what the endpoint does (one sentence; becomes the docstring)

Do not guess these. Wait for the answer before continuing.

### 5. Scaffold the files

**For both module types:**
- Add a route stub in `api/routes.py` with:
  - Correct HTTP method decorator
  - Pydantic request/response models (stubs)
  - Auth dependency matching the module's existing pattern
  - Call to the service method stub
  - Docstring from step 4
- Add a service method stub in `domain/services/` (pick the most relevant existing service class; if none fits, create a minimal new service file following the module's naming pattern)
- Add a test stub in `tests/unit/modules/<name>/` covering at minimum: happy path + unauthorized + validation error

**Additionally for `vertical` modules:**
- Scaffold Pydantic request/response schemas in the module's schemas location (match existing style — some modules keep these in `domain/models/`, others in `api/schemas.py`)
- If the endpoint implies a new repository interface, add the interface signature to `contracts.py` (a stub `...` body is fine — implementation is the user's job)

**`domain_service` modules must NOT scaffold:**
- A `contracts.py` file
- An `infrastructure/` directory
- Any persistence code. If the endpoint needs data, the scaffold must import from `faultmaven.modules.case.contracts` or `faultmaven.modules.auth.contracts`.

### 6. Verify import compliance

Before declaring done:
- Check every new import in the scaffolded files. Domain Services must import shared models from Case or Auth contracts, not from other modules' internals (e.g., not from `faultmaven.modules.case.infrastructure.*`).
- Run `lint-imports` from the repo root and report the result. If it fails on the scaffolded files, fix it before finishing.

### 7. Report

Output a summary:
- Files created (with paths)
- Files modified (with paths and what was added)
- `lint-imports` result

## Completion Criteria

Done when: (a) all scaffold files exist with the conventions for the target module type, (b) `lint-imports` passes, and (c) the user has been shown a clear list of next steps to fill in the business logic.

## Out of Scope

- Implementing the business logic — explicitly left to the user
- Writing production-quality tests — stubs only
- Adding the endpoint to external API documentation — a separate step
