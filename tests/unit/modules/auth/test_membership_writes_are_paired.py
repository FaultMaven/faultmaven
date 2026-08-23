"""No production code writes an org membership without revoking tokens (#874, #1042).

The pairing lives in
:class:`~faultmaven.modules.auth.domain.services.organization_membership_service.OrganizationMembershipService`,
but nothing stops the next caller from reaching past it to the repository and
reintroducing the bug — a member whose outstanding tokens keep carrying the
membership or the role that was just taken away, because both are only ever
checked at login.

So this scans the shipped package for calls to the two writing methods and holds
them to short allowlists. A new call site fails here and its author has to
decide, deliberately, whether it belongs on the paired path. That is the whole
intent: the failure is the conversation.

The two scans are not equally precise, and the difference is worth knowing:

* ``.remove_member(`` is ambiguous — the service method and the repository method
  share the name, and AST cannot know the receiver's type. So its allowlist
  records, per file, *how that file stays paired*, and a behavioural test pins
  the ones whose answer is "it calls the service".
* ``.update_member_role(`` is not. The paired operation is deliberately named
  ``set_member_role``, so every hit here is a raw repository write with no
  ambiguity about the receiver.

**Neither scan can see the Cloud repo.** ``faultmaven-cloud``'s admin console
drives the same repository from a separate distribution (ADR-010 D7), so the
composed module carries its own obligation to call the service rather than the
repository. That is stated in the service's module docstring and cannot be
enforced from here.
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
ALLOWED_REMOVAL_CALLERS = {
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

#: Files allowed to call ``.update_member_role(``, the *repository* write. Unlike
#: the removal list this one is unambiguous — the paired operation is named
#: ``set_member_role``, so a hit here is always a raw repository write.
#:
#: A role change that skips the service leaves a demoted admin holding elevated
#: claims until their refresh token expires (#1042), which is the same bug #874
#: fixed for removal and is invisible in the admin console: the stored role reads
#: as demoted while the API still honours the old one.
ALLOWED_ROLE_WRITE_CALLERS = {
    # The paired operation itself.
    "modules/auth/domain/services/organization_membership_service.py",
    # The sessionless wrapper delegating to the concrete repository. It is the
    # transport the paired operation writes through, not a caller with a policy
    # of its own. (The interface declaration and the concrete implementation
    # only *define* the method, so they are not call sites.)
    "infrastructure/persistence/sessionless_organization_repository.py",
}


def _call_sites(method: str) -> set[str]:
    """Package-relative paths of every file containing a ``.<method>(`` call."""
    found: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                found.add(path.relative_to(PACKAGE_ROOT).as_posix())
                break
    return found


def test_no_unpaired_membership_removal_call_sites():
    unexpected = _call_sites("remove_member") - ALLOWED_REMOVAL_CALLERS
    assert not unexpected, (
        "New `.remove_member(` call site(s) outside the paired removal path: "
        f"{sorted(unexpected)}. Removing an organization membership must go "
        "through OrganizationMembershipService.remove_member, which bumps the "
        "user's revocation watermark in the same operation (#874) — membership "
        "is verified at login only, so a bare repository delete leaves every "
        "outstanding token valid until it expires. If this call site is genuinely "
        "unrelated (team membership, a test double), add it to "
        "ALLOWED_REMOVAL_CALLERS with the reason."
    )


def test_no_unpaired_member_role_write_call_sites():
    unexpected = _call_sites("update_member_role") - ALLOWED_ROLE_WRITE_CALLERS
    assert not unexpected, (
        "New `.update_member_role(` call site(s) outside the paired role-change "
        f"path: {sorted(unexpected)}. Changing a member's org role must go "
        "through OrganizationMembershipService.set_member_role, which bumps the "
        "user's revocation watermark in the same operation (#1042) — the role is "
        "minted into the `roles` claim at login and never re-read, so a bare "
        "repository update leaves a demoted admin holding elevated claims until "
        "their refresh token expires. If this call site is genuinely a transport "
        "rather than a policy decision, add it to ALLOWED_ROLE_WRITE_CALLERS with "
        "the reason."
    )


@pytest.mark.parametrize(
    ("allowlist", "method"),
    [
        (ALLOWED_REMOVAL_CALLERS, "remove_member"),
        (ALLOWED_ROLE_WRITE_CALLERS, "update_member_role"),
    ],
    ids=["removal", "role-write"],
)
def test_allowlist_has_no_stale_entries(allowlist, method):
    """A file that no longer calls it should leave the allowlist, or the list
    stops describing the code and starts hiding it."""
    stale = allowlist - _call_sites(method)
    assert (
        not stale
    ), f"allowlist names files with no `.{method}(` call: {sorted(stale)}"
