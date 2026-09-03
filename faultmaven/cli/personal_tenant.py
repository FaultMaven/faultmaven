"""Retire or re-anchor a personal tenant (#1045, ADR-016 D5 / D8).

``fm-provision-sso-org`` brings a company tenant into existence out of band.
Personal tenants are the other shape: with
``SSO_JIT_PERSONAL_TENANT_ENABLED`` on, an SSO identity carrying no IdP
organization provisions its own tenant on first sign-in. Nothing retired one,
and that gap has teeth — soft-deleting the organization by hand fails every
later login of that subject closed (``reason=personal_org_unavailable``) with no
way back, the enterprise row does not cascade with the organization, and a user
re-anchored to a company organization leaves their personal tenant dormant for
good. This command is the operator's half of that lifecycle.

Two operations
--------------
**retire** takes a personal tenant out of service and decides, with
``--next-login``, what the subject's next org-less sign-in gets:

* ``refuse`` (default) — the account stays anchored to the retired enterprise
  and the login is refused with ``reason=personal_tenant_retired``.
* ``fresh-tenant`` — the retirement releases the anchor and the next org-less
  login provisions a brand-new personal tenant.

The anchor cannot simply be cleared: ``users.enterprise_id`` is NOT NULL
(migration 006). So the choice is *recorded* on the retired enterprise, in
``enterprises.settings``, bound to the subject by the same derived key the
provisioning path uses — and the login re-derives that key from its own
identity before honouring it, which is what stops a marker releasing anybody it
was not written for.

**re-anchor** moves an account off its personal enterprise onto a named,
operator-provisioned company organization — the operator's version of what a
mapped login now does by itself, for the cases where no mapped login is coming
(the account has to be moved before the IdP knows about it, or the personal
binding is already gone).

What is NOT touched
-------------------
**Cases, evidence and knowledge items survive a retirement**, and so does the
organization row that owns them: the organization is soft-deleted and renamed,
never removed. What data a retired tenant keeps, and for how long, is ADR-014's
subject and deliberately not this command's. Neither is
``organization_members``: the membership row of a soft-deleted organization
grants nothing (every login binds and verifies the organization first) and
removing it would be a second, separate decision.

Ordering, and why an interrupted run is finishable
--------------------------------------------------
The steps and their reasons live in one place —
:mod:`faultmaven.infrastructure.persistence.tenant_retirement` — because they
are the ordering constraint, not narration about it. In short: the organization
is soft-deleted **first**, so no login can enter a tenant being taken apart; the
subject binding goes before the IdP calls, so no login can re-create the IdP
organization the next step deletes; and the enterprise marker is written
**last**, so an anchor is never released while the derived slug it would collide
with is still occupied. Every step is idempotent and every step is discoverable
from the command's own arguments, so re-running finishes an interrupted run
rather than starting a different one.

**Run it with the owner DSN.** Like ``fm-provision-sso-org``, and for the same
reason: this resolves an organization by a *derived slug* rather than by id, and
``organizations`` is RLS-tenanted (migration 018). A preflight verifies the
connected role really is RLS-exempt and refuses before any write. The pod's own
``DATABASE_URL`` is the application role by design, so an unqualified
``kubectl exec`` would otherwise run under exactly the role this command
forbids.

Usage (``fm-personal-tenant``, installed with the package)
----------------------------------------------------------
::

    fm-personal-tenant retire --subject user_01H...
    fm-personal-tenant retire --subject user_01H... --next-login fresh-tenant --apply
    fm-personal-tenant retire --organization-id 8f1c... --apply
    fm-personal-tenant re-anchor --subject user_01H... \\
        --organization-id <company org id> --apply

In a Kubernetes deployment, run it in the API pod with the owner DSN passed
explicitly::

    kubectl exec -it deploy/faultmaven-api -- \\
        env DATABASE_URL="$OWNER_DSN" \\
        fm-personal-tenant retire --subject user_01H... --apply

Dry run is the **default**: with no ``--apply`` the command reads, reports every
side-effect it would apply, and writes nothing on either side. That inverts the
``--yes`` convention its siblings use, deliberately — those default to acting
and refuse without confirmation, which leaves "neither flag" as a state an
operator can reach; here the safe reading is the one you get by leaving a flag
off, and ``--dry-run`` is still accepted so the sibling muscle memory works.

Exit codes
----------
| 0 | done, or a dry run that reported what it would do |
| 1 | refused: nothing matched, or a guard tripped — nothing was written |
| 2 | argparse usage error (a bad flag), reserved by argparse — nothing written |
| 3 | nothing to do: already fully retired / already re-anchored |
| 4 | incomplete: some steps landed and a later one failed — re-run to finish |
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.infrastructure.persistence import tenant_retirement
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.rls_role_guard import (
    assert_provisioning_db_role_bypasses_rls,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import PROVIDER
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from faultmaven.modules.auth.contracts import (
    PERSONAL_TENANT_RETIREMENT_KEY,
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    personal_key_of_slug,
    personal_org_slug,
    personal_tenant_key,
    retired_slug,
    retired_slug_pattern,
)
from faultmaven.modules.auth.exceptions import SSOProvisioningError

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = "Retire or re-anchor a personal tenant provisioned by an SSO login."

#: Nothing matched, or a guard refused. Nothing was written on either side.
EXIT_REFUSED = 1

#: The end state already holds. Distinct from 0 so an operator scripting a sweep
#: can tell "this run did the work" from "somebody already had" — and so a
#: second run of a completed retirement is visibly a no-op rather than a claim.
EXIT_NOTHING_TO_DO = 3

#: Some steps landed and a later one did not. The one outcome that leaves work
#: outstanding, so it gets its own code; re-running the same command finishes
#: it. Deliberately not 2, which argparse owns.
EXIT_INCOMPLETE = 4

#: What ``--next-login`` accepts, and the policy each records.
_NEXT_LOGIN_POLICIES = {
    "refuse": RETIREMENT_POLICY_REFUSE,
    "fresh-tenant": RETIREMENT_POLICY_FRESH_TENANT,
}


class _Refused(Exception):
    """A guard refused before anything was written."""


@dataclass
class Step:
    """One side-effect, its operator-facing line, and how to apply it.

    A retirement is reported as the list of steps it would apply and then
    executed from that same list, so what a dry run prints and what an
    ``--apply`` run does cannot drift into two descriptions of one procedure.
    ``run`` answers True when the call changed something; a step that finds its
    work already done says so rather than claiming a write.
    """

    name: str
    description: str
    run: Callable[[], Awaitable[bool]]


def _print_header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


async def _preflight_role() -> None:
    """Refuse before any write if RLS scopes the connected role."""
    try:
        role = await assert_provisioning_db_role_bypasses_rls()
    except DeploymentCoherenceError as exc:
        raise _Refused(str(exc)) from exc
    if role:
        print(f"\nDatabase role: {role} (RLS-exempt — retirement allowed)")


def _resolve_retirement_provider(injected: Any | None) -> Any:
    """The IdP teardown port, or a refusal naming what is missing.

    Refuses rather than skipping the IdP half. A retirement that left the WorkOS
    organization standing would keep the derived ``external_id`` claimed, so the
    subject could never be provisioned a second tenant — and the command would
    have reported success for a step it did not run.
    """
    if injected is not None:
        return injected

    from faultmaven.config.settings import get_settings
    from faultmaven.container.providers.services import create_sso_identity_provider
    from faultmaven.modules.auth.contracts import ISSOTenantRetirementProvider

    provider = create_sso_identity_provider(get_settings())
    if provider is None:
        raise _Refused(
            "SSO is not configured in this environment, so the IdP half of a "
            "retirement cannot run.\n"
            "   Retiring only the FaultMaven half would leave the provider's "
            "organization standing, and with it the derived external id — the "
            "subject could then never be given a second personal tenant.\n"
            "   Run this where AUTH_MODE=oauth and the WorkOS settings are "
            "present (the API pod's own environment)."
        )
    if not isinstance(provider, ISSOTenantRetirementProvider):
        raise _Refused(
            f"The configured SSO provider ({type(provider).__name__}) does not "
            "implement personal-tenant teardown, so this command cannot remove "
            "the provider-side organization."
        )
    return provider


async def _read_state(*, organization_id: str | None, slug: str | None):
    async with get_db_session() as session:
        return await tenant_retirement.read_state(
            session,
            provider=PROVIDER,
            marker_key=PERSONAL_TENANT_RETIREMENT_KEY,
            organization_id=organization_id,
            slug=slug,
            retired_slug_pattern=retired_slug_pattern(slug) if slug else None,
        )


def _derived_key_for(state, *, subject: str | None) -> str:
    """The subject's derived key, established from evidence, never assumed.

    Two independent sources have to agree before anything is retired: the slug
    the tenant actually carries, and — when the operator named a subject — the
    key re-derived from that subject. A tenant whose slug was not derived at all
    is not a personal tenant, and retiring it would soft-delete a customer's
    organization and stamp it with a marker naming somebody who does not own it.
    """
    from_slug = personal_key_of_slug(state.organization_slug)
    if from_slug is None:
        raise _Refused(
            f"Organization {state.organization_id} (slug "
            f"'{state.organization_slug}') is not a personal tenant: its slug "
            "was not derived from an IdP subject.\n"
            "   This command retires personal tenants only. A company tenant is "
            "provisioned by fm-provision-sso-org and retired by a different "
            "procedure."
        )
    if subject is not None:
        expected = personal_tenant_key(PROVIDER, subject)
        if expected != from_slug:
            raise _Refused(
                f"Organization {state.organization_id} does not belong to "
                f"subject '{subject}': its slug derives from a different "
                "subject. Nothing was written.\n"
                "   Check --subject and --organization-id — naming both is a "
                "cross-check, and this is it refusing."
            )
    return from_slug


def _describe(state, *, key: str, policy: str) -> None:
    print(f"\nOrganization: {state.organization_id}  ({state.organization_slug})")
    print(f"Enterprise:   {state.enterprise_id}  ({state.enterprise_slug})")
    if state.binding is not None:
        print(
            f"Binding:      {state.binding.provider}:"
            f"{state.binding.provider_user_id} → this tenant "
            f"(idp org {state.binding.provider_org_id}, membership "
            f"{'confirmed' if state.binding.membership_confirmed else 'unconfirmed'})"
        )
    else:
        print("Binding:      none (already retired, or never written)")
    print(f"Derived key:  {key}")
    print(f"Next org-less login for this subject: {policy}")


def _retirement_steps(state, *, key: str, policy: str, idp) -> list[Step]:
    """The pending side-effects, in the order they have to happen.

    Only the steps whose work is outstanding: a step that finds its state
    already reached is left out entirely, which is what makes a second run
    report "nothing to do" instead of a list of no-ops.
    """
    external_id = personal_org_slug(key)
    steps: list[Step] = []

    if not state.organization_retired:

        async def _soft_delete() -> bool:
            async with get_db_session() as session:
                return await tenant_retirement.soft_delete_organization(
                    session, organization_id=state.organization_id
                )

        steps.append(
            Step(
                "organization_soft_deleted",
                f"soft-delete organization {state.organization_id} "
                "(fences every login out of the tenant)",
                _soft_delete,
            )
        )

    if state.mapping_provider_org_id is not None:

        async def _drop_mapping() -> bool:
            async with get_db_session() as session:
                return (
                    await tenant_retirement.delete_mapping(
                        session,
                        provider=PROVIDER,
                        organization_id=state.organization_id,
                    )
                    is not None
                )

        steps.append(
            Step(
                "mapping_deleted",
                f"delete the sso_org_mappings row for "
                f"{PROVIDER}:{state.mapping_provider_org_id}",
                _drop_mapping,
            )
        )

    if state.binding is not None:

        async def _drop_binding() -> bool:
            async with get_db_session() as session:
                return (
                    await tenant_retirement.delete_binding(
                        session, organization_id=state.organization_id
                    )
                    is not None
                )

        steps.append(
            Step(
                "binding_deleted",
                "delete the sso_personal_orgs row binding "
                f"{state.binding.provider_user_id} to this tenant",
                _drop_binding,
            )
        )

    post: list[Step] = []

    async def _retire_idp() -> bool:
        outcome = await asyncio.to_thread(
            lambda: idp.retire_personal_organization(external_id=external_id)
        )
        if not outcome.organization_found:
            print(f"  · idp organization external_id={external_id} was already gone")
            return False
        print(f"  · idp memberships removed: {outcome.memberships_deleted}")
        return outcome.organization_deleted

    idp_step = Step(
        "idp_organization_deleted",
        f"delete the IdP organization carrying external_id={external_id} "
        "(and its memberships first)",
        _retire_idp,
    )

    organization_retired_slug = retired_slug(external_id, state.organization_id)
    if state.organization_slug != organization_retired_slug:

        async def _rename_org() -> bool:
            async with get_db_session() as session:
                return await tenant_retirement.rename_organization(
                    session,
                    organization_id=state.organization_id,
                    slug=organization_retired_slug,
                )

        post.append(
            Step(
                "organization_renamed",
                f"rename the organization slug to {organization_retired_slug} "
                "(frees the derived slug for a later tenant)",
                _rename_org,
            )
        )

    marker = tenant_retirement.build_marker(
        provider=PROVIDER,
        key=key,
        policy=policy,
        organization_id=state.organization_id,
    )
    enterprise_retired_slug = retired_slug(external_id, state.enterprise_id)
    existing = state.retirement_marker or {}
    if not (
        state.enterprise_retired
        and state.enterprise_slug == enterprise_retired_slug
        and existing.get("key") == key
        and existing.get("policy") == policy
    ):

        async def _retire_enterprise() -> bool:
            async with get_db_session() as session:
                return await tenant_retirement.retire_enterprise(
                    session,
                    enterprise_id=state.enterprise_id,
                    slug=enterprise_retired_slug,
                    marker_key=PERSONAL_TENANT_RETIREMENT_KEY,
                    marker=marker,
                )

        post.append(
            Step(
                "enterprise_retired",
                f"soft-delete and rename the enterprise to "
                f"{enterprise_retired_slug}, recording next-login policy "
                f"'{policy}'",
                _retire_enterprise,
            )
        )

    if not steps and not post:
        # Everything this command writes is already in place, and the marker —
        # written LAST, after the provider call — says so. That ordering is what
        # makes the marker a completion record rather than an intention, and it
        # is why a finished retirement can report "nothing to do" without asking
        # the provider again.
        return []
    return steps + [idp_step] + post


async def _apply(steps: list[Step]) -> int:
    """Run the steps in order, one line per side-effect. Returns the exit code.

    A step that raises stops the run: everything after it depends on it having
    happened, and continuing would produce a report that is wrong about the
    state. Whether that is a refusal or an incomplete run turns on whether
    anything had already been applied — which is the difference between "re-run
    when the cause is fixed" and "re-run, and until you do the deployment is
    half-retired".
    """
    applied = 0
    for step in steps:
        try:
            changed = await step.run()
        except SSOProvisioningError as exc:
            print(f"\n❌ {step.name}: {exc}")
            print(
                "   The identity provider could not be asked, or refused. "
                "Nothing after this step ran."
            )
            return EXIT_INCOMPLETE if applied else EXIT_REFUSED
        except Exception as exc:  # noqa: BLE001 — an operator needs the reason
            print(f"\n❌ {step.name}: {type(exc).__name__}: {exc}")
            return EXIT_INCOMPLETE if applied else EXIT_REFUSED
        if changed:
            applied += 1
            print(f"  ✅ {step.description}")
        else:
            print(f"  · {step.description} — already done")
    return 0


async def _retire_idp_residue_only(
    *, subject: str | None, organization_id: str | None, idp, apply: bool
) -> int:
    """No FaultMaven tenant matched — deal with any provider-side leftover.

    This is not a hypothetical shape. Provisioning creates the IdP organization
    *before* the database transaction, so an attempt that minted one and then
    failed to commit leaves an organization carrying the derived external id and
    no tenant anywhere. The login path heals that by adopting it on the next
    sign-in; an operator retiring the subject instead needs it gone, or the
    external id stays claimed forever.

    Reachable only when the subject is known: the derived external id is what
    addresses the provider, and an ``--organization-id`` that matched nothing
    supplies no subject to derive it from.
    """
    where = f"subject '{subject}'" if subject else f"organization {organization_id}"
    print(f"\nNo FaultMaven personal tenant matches {where}.")
    print(
        "   A tenant retired earlier still has its organization row, under a "
        "retired slug, and would have been found — so this is an absent tenant "
        "rather than a finished one."
    )
    if subject is None:
        print("   Nothing was written.")
        return EXIT_NOTHING_TO_DO

    external_id = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    if not apply:
        print(
            f"\nWould apply:\n  · delete any IdP organization carrying "
            f"external_id={external_id} — a first sign-in that minted one and "
            "then failed to commit leaves exactly that residue"
        )
        print("\nDry run — nothing was written. Re-run with --apply.")
        return 0

    try:
        outcome = await asyncio.to_thread(
            lambda: idp.retire_personal_organization(external_id=external_id)
        )
    except SSOProvisioningError as exc:
        print(f"\n❌ {exc}")
        print("   Nothing was written on either side.")
        return EXIT_REFUSED
    if not outcome.organization_found:
        print(
            f"   No IdP organization carries external_id={external_id} either. "
            "Nothing to do."
        )
        return EXIT_NOTHING_TO_DO
    print(
        f"\n  ✅ removed the orphaned IdP organization (external_id={external_id}, "
        f"memberships removed: {outcome.memberships_deleted})"
    )
    return 0


async def retire(
    *,
    subject: str | None,
    organization_id: str | None,
    next_login: str,
    apply: bool,
    idp: Any | None = None,
) -> int:
    """Retire one personal tenant. Returns the process exit code."""
    _print_header("Retire Personal Tenant")
    policy = _NEXT_LOGIN_POLICIES[next_login]

    try:
        await _preflight_role()
        provider = _resolve_retirement_provider(idp)

        slug = (
            personal_org_slug(personal_tenant_key(PROVIDER, subject))
            if subject
            else None
        )
        state = await _read_state(organization_id=organization_id, slug=slug)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    if state is None:
        return await _retire_idp_residue_only(
            subject=subject, organization_id=organization_id, idp=provider, apply=apply
        )

    try:
        key = _derived_key_for(state, subject=subject)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    _describe(state, key=key, policy=next_login)

    try:
        steps = _retirement_steps(state, key=key, policy=policy, idp=provider)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    if not steps:
        # Every write is already in place AND the marker — written last, after
        # the provider call — says the retirement completed. Reporting "done"
        # here would claim work this run did not do.
        print(
            "\nAlready retired, with the same next-login policy — nothing to do. "
            "Nothing was written."
        )
        return EXIT_NOTHING_TO_DO

    if not apply:
        print("\nWould apply:")
        for step in steps:
            print(f"  · {step.description}")
        print(
            "\nDry run — nothing was written, on either side. Re-run with "
            "--apply to retire this tenant."
        )
        print(
            "   Cases, evidence and knowledge items are NOT removed by this "
            "command; the organization is soft-deleted, not deleted."
        )
        return 0

    print("\nApplying:")
    code = await _apply(steps)
    if code != 0:
        return code

    print("\n✅ Retired.")
    if policy == RETIREMENT_POLICY_FRESH_TENANT:
        print(
            "   The subject's next org-less sign-in provisions a NEW personal "
            "tenant. Their retired one keeps its cases and is no longer "
            "reachable by any login."
        )
    else:
        print(
            "   The subject's next org-less sign-in is refused with "
            "reason=personal_tenant_retired. Re-run with "
            "--next-login fresh-tenant to let them start over."
        )
    return 0


# --------------------------------------------------------------------------- #
# re-anchor
# --------------------------------------------------------------------------- #


async def _load_user(subject: str):
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )

    users = SessionlessUserRepository()
    user = await users.get_by_sso(PROVIDER, subject)
    if user is None:
        raise _Refused(
            f"No account is linked to {PROVIDER} subject '{subject}'.\n"
            "   Nothing was written. Check the subject — it is the IdP's own "
            "opaque handle (user_01H…), never an email."
        )
    if getattr(user, "deleted_at", None) is not None or not getattr(
        user, "is_active", True
    ):
        raise _Refused(
            f"Account {user.user_id} is deactivated or deleted, so moving its "
            "anchor would grant a company organization a member that cannot "
            "sign in. Nothing was written."
        )
    return users, user


async def _load_company_organization(organization_id: str):
    from faultmaven.config.tenant_context import set_current_org_id
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )

    if organization_id == STANDALONE_ORG_ID:
        raise _Refused(
            "That id is the Standalone sentinel, which identifies the "
            "deployment rather than a tenant (fm#850). Nothing was written."
        )
    set_current_org_id(organization_id)
    orgs = SessionlessOrganizationRepository()
    organization = await orgs.get_organization(organization_id)
    if (
        organization is None
        or getattr(organization, "deleted_at", None) is not None
        or not getattr(organization, "is_active", True)
    ):
        raise _Refused(
            f"No usable organization '{organization_id}' (it is an id, not a "
            "slug; a soft-deleted or inactive one does not resolve). Nothing "
            "was written."
        )

    async with get_db_session() as session:
        state = await tenant_retirement.read_state(
            session,
            provider=PROVIDER,
            marker_key=PERSONAL_TENANT_RETIREMENT_KEY,
            organization_id=organization_id,
        )
    if state is None or state.mapping_provider_org_id is None:
        raise _Refused(
            f"Organization {organization_id} has no {PROVIDER} mapping, so no "
            "login can land in it and an account anchored there would be "
            "stranded.\n"
            "   Map it first with fm-provision-sso-org. Nothing was written."
        )
    if state.binding is not None or personal_key_of_slug(state.organization_slug):
        raise _Refused(
            f"Organization {organization_id} is itself a personal tenant. "
            "Moving an account into somebody's personal tenant is never the "
            "right operation. Nothing was written."
        )
    return orgs, organization


async def reanchor(
    *,
    subject: str,
    organization_id: str,
    apply: bool,
) -> int:
    """Move one account off its personal enterprise. Returns the exit code."""
    _print_header("Re-anchor Personal Account")

    try:
        await _preflight_role()
        users, user = await _load_user(subject)
        orgs, organization = await _load_company_organization(organization_id)

        personal_slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
        async with get_db_session() as session:
            personal = await tenant_retirement.read_state(
                session,
                provider=PROVIDER,
                marker_key=PERSONAL_TENANT_RETIREMENT_KEY,
                slug=personal_slug,
                retired_slug_pattern=retired_slug_pattern(personal_slug),
            )
        personal_enterprise = personal.enterprise_id if personal else None
        current = getattr(user, "enterprise_id", None)
        already_moved = current == organization.enterprise_id

        if not already_moved and current != personal_enterprise:
            # The narrowing the login path makes, made here too: the account's
            # current enterprise must be the one ITS OWN personal tenant owns,
            # established from that tenant's derived slug rather than from a
            # name. A company-to-company move is a different act with different
            # consequences and is refused here.
            raise _Refused(
                f"Account {user.user_id} is anchored to enterprise {current}, "
                "which is not the enterprise of its own personal tenant "
                f"({personal_enterprise or 'none found'}).\n"
                "   This command moves an account OFF its personal tenant "
                "only. Moving one between company enterprises is a manual "
                "migration. Nothing was written."
            )
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    print(f"\nAccount:      {user.username} <{user.email}> ({user.user_id})")
    print(f"Anchored to:  {current}")
    print(
        f"Moving to:    {organization.organization_id} ({organization.name}) "
        f"in enterprise {organization.enterprise_id}"
    )

    role_id = SYSTEM_ROLE_IDS[Role.MEMBER]
    existing_role = await orgs.get_member_role(organization_id, user.user_id)
    has_binding = personal is not None and personal.binding is not None

    steps: list[Step] = []
    if not already_moved:

        async def _move() -> bool:
            user.enterprise_id = organization.enterprise_id
            await users.update(user)
            return True

        steps.append(
            Step(
                "anchor_moved",
                f"anchor {user.user_id} to enterprise " f"{organization.enterprise_id}",
                _move,
            )
        )

    if existing_role is None:

        async def _grant() -> bool:
            await orgs.add_member(organization_id, user.user_id, role_id)
            return True

        steps.append(
            Step(
                "membership_granted",
                f"grant {user.user_id} the member role in " f"{organization_id}",
                _grant,
            )
        )

    if has_binding:

        async def _retire_binding() -> bool:
            async with get_db_session() as session:
                return (
                    await tenant_retirement.delete_binding(
                        session, organization_id=personal.organization_id
                    )
                    is not None
                )

        steps.append(
            Step(
                "binding_retired",
                "delete the sso_personal_orgs row, so a later org-less login "
                "does not resolve back into the personal tenant",
                _retire_binding,
            )
        )

    if not steps:
        print("\nAlready re-anchored — nothing to do. Nothing was written.")
        return EXIT_NOTHING_TO_DO

    if not apply:
        print("\nWould apply:")
        for step in steps:
            print(f"  · {step.description}")
        print("\nDry run — nothing was written. Re-run with --apply.")
        return 0

    print("\nApplying:")
    code = await _apply(steps)
    if code != 0:
        return code

    print("\n✅ Re-anchored.")
    if personal is not None:
        print(
            "   The personal tenant is now dormant: its cases stay where they "
            "are and nobody can enter it. Retire it when you are ready:\n"
            f"     fm-personal-tenant retire --organization-id "
            f"{personal.organization_id} --apply"
        )
    return 0


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def _add_apply_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (without it the command reports and writes nothing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default: report what would change, write nothing",
    )


def main() -> None:
    """Console entrypoint (``fm-personal-tenant``)."""
    parser = argparse.ArgumentParser(
        prog="fm-personal-tenant",
        description=_SUMMARY,
        epilog=(
            "Dry run is the default; pass --apply to write. Cases, evidence and "
            "knowledge items are never removed — the organization is "
            "soft-deleted. See docs/operations/sso-org-provisioning.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="operation", required=True)

    retire_parser = sub.add_parser(
        "retire",
        help="Take a personal tenant out of service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    retire_parser.add_argument(
        "--subject",
        default=None,
        help="The IdP subject (user_01H…) whose personal tenant to retire",
    )
    retire_parser.add_argument(
        "--organization-id",
        default=None,
        help="The personal organization's id (an id, not a slug)",
    )
    retire_parser.add_argument(
        "--next-login",
        choices=sorted(_NEXT_LOGIN_POLICIES),
        default="refuse",
        help=(
            "What the subject's next org-less sign-in gets: 'refuse' (default) "
            "or 'fresh-tenant' — a brand-new personal tenant"
        ),
    )
    _add_apply_flags(retire_parser)

    reanchor_parser = sub.add_parser(
        "re-anchor",
        help="Move an account off its personal enterprise onto a company one",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    reanchor_parser.add_argument(
        "--subject", required=True, help="The IdP subject (user_01H…) to move"
    )
    reanchor_parser.add_argument(
        "--organization-id",
        required=True,
        help="The mapped company organization to move them to (an id, not a slug)",
    )
    _add_apply_flags(reanchor_parser)

    args = parser.parse_args()

    # --dry-run with --apply is a usage error, not a preference. The two
    # invocations differ by one flag, so an operator editing the previous
    # command can end up with both — and silently taking either branch reads as
    # the other.
    if args.dry_run and args.apply:
        parser.error(
            "--dry-run and --apply are mutually exclusive: --apply writes, and "
            "leaving it off is already a dry run."
        )

    if args.operation == "retire":
        if bool(args.subject) == bool(args.organization_id) and not (
            args.subject and args.organization_id
        ):
            retire_parser.error(
                "pass --subject, --organization-id, or both (naming both is a "
                "cross-check: the command refuses if they disagree)."
            )
        sys.exit(
            asyncio.run(
                retire(
                    subject=args.subject,
                    organization_id=args.organization_id,
                    next_login=args.next_login,
                    apply=args.apply,
                )
            )
        )

    sys.exit(
        asyncio.run(
            reanchor(
                subject=args.subject,
                organization_id=args.organization_id,
                apply=args.apply,
            )
        )
    )


if __name__ == "__main__":
    main()
