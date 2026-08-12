"""No production code removes an org membership without revoking tokens (#874).

The pairing lives in
:class:`~faultmaven.modules.auth.domain.services.organization_membership_service.OrganizationMembershipService`,
but nothing stops the next caller from reaching past it to
``IOrganizationRepository.remove_member`` and reintroducing the bug — a removed
member whose outstanding tokens keep working until they expire, because
membership is only ever checked at login.

So this scans the shipped package for ``.remove_member(`` calls and holds them to
a short allowlist. A new call site fails here and its author has to decide,
deliberately, whether it belongs on the paired path. That is the whole intent:
the failure is the conversation.

**It cannot see the Cloud repo.** ``faultmaven-cloud``'s admin console drives the
same repository from a separate distribution (ADR-010 D7), so the composed module
carries its own obligation to call the service rather than the repository. That
is stated in the service's module docstring and cannot be enforced from here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(__file__).resolve().parents[4] / "faultmaven"

#: Files allowed to call ``.remove_member(``, and why. Anything else is a new
#: unpaired removal until proven otherwise.
#:
#: The scan is file-level: it matches the method *name*, not the receiver's type,
#: which AST cannot know. So it answers "is a new file removing memberships?" and
#: each entry below records how that file stays paired. Where the answer is "it
#: calls the service", a behavioural test pins that separately — the allowlist is
#: a prompt for review, not the proof.
ALLOWED_CALLERS = {
    # The paired operation itself — the one place an org membership may be deleted.
    "modules/auth/domain/services/organization_membership_service.py",
    # The operator command, which calls the *service*, not the repository. Pinned
    # by tests/unit/cli/test_remove_org_member_cli.py::test_removal_goes_through
    # _the_paired_service.
    "cli/remove_org_member.py",
    # The sessionless wrapper delegating to the concrete repository. It is the
    # transport for the call above, not a caller with a policy of its own.
    "infrastructure/persistence/sessionless_organization_repository.py",
    # Team membership, not organization membership: leaving a team narrows KB
    # read scope, it does not end tenancy, and no token claim carries it.
    "infrastructure/persistence/sessionless_team_repository.py",
}


def _call_sites() -> set[str]:
    """Package-relative paths of every file containing a ``.remove_member(`` call."""
    found: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "remove_member"
            ):
                found.add(path.relative_to(PACKAGE_ROOT).as_posix())
                break
    return found


def test_no_unpaired_membership_removal_call_sites():
    unexpected = _call_sites() - ALLOWED_CALLERS
    assert not unexpected, (
        "New `.remove_member(` call site(s) outside the paired removal path: "
        f"{sorted(unexpected)}. Removing an organization membership must go "
        "through OrganizationMembershipService.remove_member, which bumps the "
        "user's revocation watermark in the same operation (#874) — membership "
        "is verified at login only, so a bare repository delete leaves every "
        "outstanding token valid until it expires. If this call site is genuinely "
        "unrelated (team membership, a test double), add it to ALLOWED_CALLERS "
        "with the reason."
    )


def test_allowlist_has_no_stale_entries():
    """A file that no longer calls it should leave the allowlist, or the list
    stops describing the code and starts hiding it."""
    stale = ALLOWED_CALLERS - _call_sites()
    assert not stale, f"ALLOWED_CALLERS names files with no such call: {sorted(stale)}"
