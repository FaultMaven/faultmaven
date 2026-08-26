"""Phase 0 enforces nothing: no endpoint uses a permission/role dependency (#1163).

``AuthenticatedUser.permissions`` is populated from ``claims.get("permissions",
[])`` and **neither token generator mints that key**, so it is always ``[]``.
Every dependency built on it therefore denies *every* caller — a platform admin
included. The first ``Depends(require_permission(...))`` on a live route is not
a permission check; it is an outage, and one that reads as "the RBAC wiring
doesn't work" rather than as "nothing fills the claim".

``require_role`` / ``require_any_role`` are a subtler case: the ``roles`` claim
*is* minted, so those would "work" — they would start enforcing an authority
model (``admin`` / ``member`` / ``viewer`` on the org axis) whose fitness is
exactly what Phase 1's shadow logging exists to measure, before anyone has
looked at the data. Phase 0 holds all five to the same rule.

So this scans the shipped package and holds the enforcement surface to an
**empty** allowlist. Lifting it is Phase 2's job, and doing so should mean
deleting a line here on purpose (see
``docs/architecture/security/org-permission-enforcement.md``).

Test ↔ guard mapping — what breaks each test:

* ``test_no_module_references_a_permission_dependency`` — add
  ``Depends(require_permission("cases:read"))`` (or a bare reference to any of
  the five names) anywhere under ``faultmaven/``. This is the load-bearing one:
  it sees *uses*, including a reference that is stored rather than called.
* ``test_only_the_re_export_imports_a_permission_dependency`` — import one of
  the five into any module other than the middleware re-export. Catches the
  step before a use, and catches a use this scan's Name/Attribute matching
  could miss (e.g. a decorator built by ``functools.partial``).
* ``test_the_dependencies_still_exist`` — the two scans above pass vacuously if
  the names are renamed or deleted. This pins that the surface being held empty
  is still there to hold.

**This cannot see ``faultmaven-cloud``.** The composed Cloud module (ADR-010 D7)
mounts its own routes over this core, and carries the same obligation
separately — the same limit ``test_membership_writes_are_paired`` records.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(__file__).resolve().parents[4] / "faultmaven"

#: The dependency factories that gate on ``AuthenticatedUser.permissions`` (the
#: first three) or on the ``roles`` claim (the last two).
PERMISSION_DEPENDENCIES = frozenset(
    {
        "require_permission",
        "require_any_permission",
        "require_all_permissions",
        "require_role",
        "require_any_role",
    }
)

#: Files allowed to *reference* one. Empty, and that is the point of the test.
ALLOWED_USERS: frozenset[str] = frozenset()

#: Files allowed to *import* one. Only the middleware package's re-export, which
#: publishes the names without using them.
ALLOWED_IMPORTERS = frozenset({"api/middleware/__init__.py"})

#: Where the five are defined. A ``def require_permission(...)`` is not a use of
#: it, and the docstring examples inside are strings, invisible to AST.
DEFINITION_SITE = "api/middleware/auth.py"


def _iter_modules():
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _reference_sites() -> set[str]:
    """Files that name one of the dependencies in an expression.

    Matches a bare ``ast.Name`` load (``Depends(require_permission("x"))``,
    ``dep = require_role``) and an attribute access (``middleware.require_role``),
    so a use is caught whether or not it is called on the spot.
    """
    found: set[str] = set()
    for rel, tree in _iter_modules():
        if rel == DEFINITION_SITE:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in PERMISSION_DEPENDENCIES:
                found.add(rel)
                break
            if isinstance(node, ast.Attribute) and node.attr in PERMISSION_DEPENDENCIES:
                found.add(rel)
                break
    return found


def _import_sites() -> set[str]:
    found: set[str] = set()
    for rel, tree in _iter_modules():
        if rel == DEFINITION_SITE:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                alias.name in PERMISSION_DEPENDENCIES for alias in node.names
            ):
                found.add(rel)
                break
    return found


def test_no_module_references_a_permission_dependency():
    unexpected = _reference_sites() - ALLOWED_USERS
    assert not unexpected, (
        "Permission/role dependency used in: "
        f"{sorted(unexpected)}. Phase 0 of the org-permission rollout enforces "
        "nothing on purpose: no token generator mints a `permissions` claim, so "
        "`AuthenticatedUser.permissions` is always empty and "
        "`require_permission` denies every caller including a platform admin. "
        "`require_role` would enforce an authority model Phase 1 has not yet "
        "measured. See docs/architecture/security/org-permission-enforcement.md "
        "— enforcement lands in Phase 2, after Phase 1 populates the field and "
        "shadow-logs what would have been denied."
    )


def test_only_the_re_export_imports_a_permission_dependency():
    unexpected = _import_sites() - ALLOWED_IMPORTERS
    assert not unexpected, (
        "Permission/role dependency imported into: "
        f"{sorted(unexpected)}. Importing one is the step before enforcing with "
        "it; see the message above for why Phase 0 holds this surface empty."
    )


def test_the_re_export_allowlist_has_no_stale_entries():
    """Otherwise the allowlist stops describing the code and starts hiding it."""
    stale = ALLOWED_IMPORTERS - _import_sites()
    assert not stale, f"allowlist names files that import nothing: {sorted(stale)}"


def test_the_dependencies_still_exist():
    """The scans above are vacuous if the names moved; pin that they have not."""
    from faultmaven.api.middleware import auth as auth_middleware

    missing = [
        name for name in PERMISSION_DEPENDENCIES if not hasattr(auth_middleware, name)
    ]
    assert not missing, (
        f"{sorted(missing)} no longer defined in {DEFINITION_SITE} — the "
        "emptiness the other tests assert would be emptiness about nothing. "
        "Update PERMISSION_DEPENDENCIES to whatever replaced them."
    )
